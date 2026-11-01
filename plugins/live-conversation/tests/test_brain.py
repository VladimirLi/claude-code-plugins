"""brain piece tests: markdown cleaning, stream parsing, interrupt, fastest-process pick.

Uses tests/fake_claude.py instead of the real CLI, so they run offline in about 3 s.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from live.brain import Brain, Interrupted, _Cleaner, brain_env, spoken  # noqa: E402
from live.tts import Chunker  # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")


def run(coro):
    return asyncio.run(coro)


class TestSpoken(unittest.TestCase):
    def test_strips_markdown(self):
        out = spoken("Here:\n- **First** thing\n- `git status`, check\n## Next\n1. one")
        for mark in "*`#":
            self.assertNotIn(mark, out)
        self.assertNotIn("- ", out)
        self.assertIn("First thing.", out)      # a list item ends a sentence

    def test_plain_text_unchanged(self):
        t = "Canberra. It was built as a compromise, in 1913."
        self.assertEqual(spoken(t), t)

    def test_streaming_does_not_hold_plain_text(self):
        c = _Cleaner()
        self.assertEqual(c.push("Canberra"), "Canberra")
        self.assertEqual(c.push(". It's"), ". It's")

    def test_streaming_bullet_split_over_deltas(self):
        c = _Cleaner()
        got = "".join(c.push(d) for d in ["-", " It", "em one\n", "1", ". two"]) + c.flush()
        self.assertEqual(got.strip(), "Item one. two")

    def test_number_at_line_start_kept(self):
        c = _Cleaner()
        got = "".join(c.push(d) for d in ["19", "90 was a year."]) + c.flush()
        self.assertEqual(got, "1990 was a year.")


class TestArgv(unittest.TestCase):
    def test_tools_and_isolation_flags(self):
        argv = Brain({"cwd": "/x"})._argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Bash")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Grep,Glob,Bash")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        for bypass in ("--dangerously-skip-permissions", "--permission-mode", "--allow-dangerously-skip-permissions"):
            self.assertNotIn(bypass, argv)

    def test_flags(self):
        b = Brain({"cwd": "/x/proj"})
        argv = b._argv()
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn('{"disableAllHooks": true}', argv)
        sp = argv[argv.index("--system-prompt") + 1]
        self.assertIn("/x/proj", sp)
        self.assertIn("markdown", sp)


class TestEnv(unittest.TestCase):
    """The brain runs Bash without asking: it must not see the daemon's secrets."""

    def test_scrub(self):
        env = brain_env({"HOME": "/h", "PATH": "/p", "USER": "u", "LANG": "C", "TMPDIR": "/t",
                         "CLAUDE_CONFIG_DIR": "/c", "AZURE_OPENAI_API_KEY": "k", "AZURE_SPEECH_REGION": "r",
                         "GITHUB_TOKEN": "t", "my_secret": "s", "DB_PASSWORD": "p", "AWS_SECRET_ACCESS_KEY": "a",
                         "ANTHROPIC_API_KEY": "claude", "CLAUDE_CODE_OAUTH_TOKEN": "claude"})
        self.assertEqual(sorted(env), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
                                       "HOME", "LANG", "PATH", "TMPDIR", "USER"])

    def test_bedrock_keeps_its_aws_credentials(self):
        env = brain_env({"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_SECRET_ACCESS_KEY": "a", "AZURE_X_KEY": "z"})
        self.assertIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("AZURE_X_KEY", env)

    def test_the_process_sees_no_azure_key(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "env.json")
        saved = {k: os.environ.get(k) for k in ("AZURE_OPENAI_API_KEY", "FAKE_ENV_OUT")}
        os.environ["AZURE_OPENAI_API_KEY"] = "secret"
        os.environ["FAKE_ENV_OUT"] = out
        try:
            async def go():
                b = fake_brain(warm=False)
                await b.start()
                for _ in range(100):
                    if os.path.exists(out):
                        break
                    await asyncio.sleep(0.02)
                await b.close()
            run(go())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        seen = json.load(open(out))
        self.assertNotIn("AZURE_OPENAI_API_KEY", seen)
        self.assertEqual(seen.get("HOME"), os.environ.get("HOME"))
        self.assertEqual(seen.get("PATH"), os.environ.get("PATH"))


def fake_brain(**cfg):
    base = {"claude_bin": FAKE, "pool": 1, "probes": 0, "cwd": HERE}
    base.update(cfg)
    return Brain(base)


class TestStream(unittest.TestCase):
    def test_sentence_before_tool_is_released_at_once(self):
        async def go():
            b = fake_brain()
            await b.start()
            ch, t0, first_sentence, text = Chunker(), time.monotonic(), None, ""
            async for d in b.respond("use a tool please"):
                text += d
                if first_sentence is None and ch.push(d):
                    first_sentence = time.monotonic() - t0
            await b.close()
            return first_sentence, text
        fs, text = run(go())
        self.assertLess(fs, 0.5)                # not after the 0.6 s tool call
        self.assertEqual(" ".join(text.split()), "Let me check. It works.")

    def test_narration_between_tools_dropped(self):
        async def go():
            b = fake_brain()
            await b.start()
            text = "".join([d async for d in b.respond("narrate the work")])
            await b.close()
            return text
        self.assertEqual(" ".join(run(go()).split()), "All pass.")

    def test_answer_after_tool_keeps_spaces(self):
        async def go():
            b = fake_brain()
            await b.start()
            text = "".join([d async for d in b.respond("report on tests")])
            await b.close()
            return text
        self.assertEqual(" ".join(run(go()).split()),
                         "Let me run them. One test is broken. It expected three. The rest pass.")

    def test_narration_kept_after_long_silence(self):
        async def go():
            b = fake_brain(narrate_after_s=0.0)
            await b.start()
            text = "".join([d async for d in b.respond("narrate the work")])
            await b.close()
            return text
        self.assertEqual(" ".join(run(go()).split()), "I'll run pytest now. All pass.")

    def test_markdown_cleaned_in_stream(self):
        async def go():
            b = fake_brain()
            await b.start()
            text = "".join([d async for d in b.respond("markdown")])
            await b.close()
            return text
        text = run(go())
        self.assertNotIn("*", text)
        self.assertNotIn("`", text)
        self.assertIn("one thing", text)

    def test_cancel_then_next_turn(self):
        async def go():
            b = fake_brain()
            await b.start()
            got = []
            async def consume():
                async for d in b.respond("long answer"):
                    got.append(d)
            task = asyncio.create_task(consume())
            await asyncio.sleep(0.2)
            task.cancel()
            await b.cancel()
            t0 = time.monotonic()
            nxt = "".join([d async for d in b.respond("hi", Interrupted("word word"))])
            dt = time.monotonic() - t0
            await b.close()
            return got, nxt, dt
        got, nxt, dt = run(go())
        self.assertTrue(got)
        self.assertLess(len(got), 100)
        self.assertEqual(nxt.strip(), "ready")
        self.assertLess(dt, 1.0)

    def test_abandoned_stream_interrupts_by_itself(self):
        async def go():
            b = fake_brain()
            await b.start()
            gen = b.respond("long answer")
            await gen.__anext__()
            await gen.aclose()                   # caller stops listening, no cancel()
            t0 = time.monotonic()
            nxt = "".join([d async for d in b.respond("hi")])
            dt = time.monotonic() - t0
            await b.close()
            return nxt, dt
        nxt, dt = run(go())
        self.assertEqual(nxt.strip(), "ready")
        self.assertLess(dt, 1.0)                 # did not wait for the 4 s long answer


class TestToolBudget(unittest.TestCase):
    """Bar session 20261030-110521 turn 5: "Can we see if there might be any other issues ... from the
    logs" got "Let me check the logs ..." and then the voice brain dug with its own tools while the
    user heard nothing. Past its tool budget the reply is stopped and the brain hands the work off."""

    BAR = ("Can we see if there might be any other issues? for this plugin, from the logs, one thing that I "
           "would like to... prove is that when the plugin has started and the state switch to listening")

    def test_digging_reply_is_turned_into_a_handoff(self):
        from live import tasks
        async def go():
            b = fake_brain(prompt_extra=tasks.handoff_prompt("live-task --run-dir /r"), tool_budget=2,
                           tool_budget_s=30)
            await b.start()
            t0 = time.monotonic()
            text = "".join([d async for d in b.respond(self.BAR)])
            dt = time.monotonic() - t0
            nudges = b.session.nudges
            await b.close()
            return text, dt, nudges
        text, dt, nudges = run(go())
        self.assertEqual(nudges, 1)
        self.assertEqual(" ".join(text.split()), "Let me check the logs. Handing that to Claude Code. Task one.")
        self.assertLess(dt, 2.0)                 # not the 2.4 s of eight tool calls

    def test_time_budget(self):
        async def go():
            b = fake_brain(tool_budget=10, tool_budget_s=0.5)
            await b.start()
            text = "".join([d async for d in b.respond(self.BAR)])
            n = b.session.nudges
            await b.close()
            return text, n
        text, n = run(go())
        self.assertEqual(n, 1)
        self.assertTrue(text.startswith("Let me check the logs."))

    def test_handoff_call_is_not_counted(self):
        async def go():
            b = fake_brain(tool_budget=1, tool_budget_s=0.1)
            await b.start()
            text = "".join([d async for d in b.respond("handoff please")])
            n = b.session.nudges
            await b.close()
            return text, n
        text, n = run(go())
        self.assertEqual(n, 0)
        self.assertEqual(" ".join(text.split()), "Handing that to Claude Code. Task one.")

    def test_one_slow_tool_call_is_stopped_by_the_time_budget(self):
        # tool_budget_s was only checked when a tool block ended: one 40 s Bash call meant 40 s of silence
        async def go():
            b = fake_brain(tool_budget=10, tool_budget_s=0.5)
            await b.start()
            t0 = time.monotonic()
            text = "".join([d async for d in b.respond("run the slow command")])
            dt = time.monotonic() - t0
            n = b.session.nudges
            await b.close()
            return text, dt, n
        text, dt, n = run(go())
        self.assertEqual(n, 1)
        self.assertLess(dt, 2.0)                 # not the 4 s the tool runs
        self.assertTrue(text.startswith("Let me check."))

    def test_reading_or_grepping_live_task_is_not_a_handoff(self):
        # a Read of bin/live-task or a Grep for "live-task" turned the whole budget off (bug: substring match)
        from live.brain import _Turn
        async def go():
            q = asyncio.Queue()
            t = _Turn(q, tool_budget=2, tool_budget_s=8)
            t._tool_done(json.dumps({"file_path": "/repo/plugins/live-conversation/bin/live-task"}), "Read")
            t._tool_done(json.dumps({"pattern": "live-task", "path": "live"}), "Grep")
            t._tool_done(json.dumps({"command": "grep -rn live-task live"}), "Bash")
            t.finish()
            return t, q
        t, q = run(go())
        self.assertFalse(t.handed_off)
        self.assertTrue(t.over)

    def test_handoff_command_forms(self):
        from live import tasks
        from live.brain import is_handoff
        def bash(c):
            return json.dumps({"command": c})
        cmd = tasks.command("/a b/python", "/a b/bin/live-task", "/tmp/run dir")
        self.assertTrue(is_handoff("Bash", bash(cmd + ' "Fix the test; then commit"')))
        self.assertTrue(is_handoff("Bash", bash("cd /x && python3 bin/live-task --status")))
        self.assertTrue(is_handoff("Bash", bash("/p/bin/live-task --run-dir /r 'x'")))
        self.assertFalse(is_handoff("Bash", bash("cat bin/live-task")))
        self.assertFalse(is_handoff("Bash", bash("ls bin | grep live-task")))
        self.assertFalse(is_handoff("Read", json.dumps({"file_path": "/p/bin/live-task"})))

    def test_budget_off(self):
        async def go():
            b = fake_brain(tool_budget=0)
            await b.start()
            text = "".join([d async for d in b.respond(self.BAR)])
            await b.close()
            return text
        self.assertEqual(" ".join(run(go()).split()), "Let me check the logs. Found nothing.")

    def test_nudge_without_handoff_asks_for_an_answer(self):
        from live import brain
        from live import tasks
        self.assertEqual(fake_brain()._new_session().nudge, brain.NUDGE_ANSWER)
        b = fake_brain(prompt_extra=tasks.handoff_prompt("x"))
        self.assertEqual(b._new_session().nudge, brain.NUDGE_HANDOFF)


class TestPool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        os.environ["FAKE_DELAYS"] = "2000,60,1500,2500"
        os.environ["FAKE_COUNTER"] = os.path.join(self.d.name, "n")

    def tearDown(self):
        os.environ.pop("FAKE_DELAYS")
        os.environ.pop("FAKE_COUNTER")
        self.d.cleanup()

    def test_keeps_fastest_processes(self):
        async def go():
            b = fake_brain(pool=4, probes=2, race=2, fast_ms=10000, pool_batches=1)
            await b.start()
            info = b.startup
            t0 = time.monotonic()
            "".join([x async for x in b.respond("hi")])
            dt = time.monotonic() - t0
            await b.close()
            return info, dt
        info, dt = run(go())
        self.assertEqual(len(info["candidates_ttft_ms"]), 4)
        self.assertEqual(len(info["kept_ttft_ms"]), 2)
        self.assertLess(info["kept_ttft_ms"][0], 1000)
        self.assertLess(dt, 1.0)

    def test_race_takes_first_and_syncs_loser(self):
        async def go():
            b = fake_brain(pool=2, probes=0, race=2, fast_ms=10000, pool_batches=1)
            await b.start()
            slow = [s for s in b.sessions if s is not b.sessions[0]]
            t0 = time.monotonic()
            first = "".join([x async for x in b.respond("hi")])
            dt = time.monotonic() - t0
            loser = [s for s in b.sessions if s is not b.session][0]
            msg = b._message(loser, "next", None)
            await b.close()
            return first, dt, msg, len(slow)
        first, dt, msg, n = run(go())
        self.assertEqual(first.strip(), "ready")
        self.assertLess(dt, 1.0)                # the 60 ms process, not the 2000 ms one
        self.assertIn('the user said "hi" and you answered "ready"', msg)
        self.assertTrue(msg.endswith("next"))


class TestRaceSkipsThinking(unittest.TestCase):
    """2026-10-30 replay: the race winner had started a thinking block at 0.95-1.06 s and its
    first word came 0.45-1.07 s later; the other process was already cancelled."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        os.environ["FAKE_DELAYS"] = "60,300"
        os.environ["FAKE_THINK"] = "1500,0"
        os.environ["FAKE_COUNTER"] = os.path.join(self.d.name, "n")

    def tearDown(self):
        for k in ("FAKE_DELAYS", "FAKE_THINK", "FAKE_COUNTER"):
            os.environ.pop(k)
        self.d.cleanup()

    def test_first_to_answer_wins_not_first_to_think(self):
        async def go():
            b = fake_brain(pool=2, probes=0, race=2, fast_ms=10000, pool_batches=1)
            await b.start()
            t0 = time.monotonic()
            first = None
            async for _ in b.respond("hi"):
                if first is None:
                    first = time.monotonic() - t0
            timing = b.last_timing
            await b.close()
            return first, timing
        first, timing = run(go())
        self.assertLess(first, 1.0)                  # the 300 ms answer, not 60 ms + 1500 ms thinking
        self.assertIn("thinking", timing["first_kind"])


if __name__ == "__main__":
    unittest.main()


class TestInterruptedQuestion(unittest.TestCase):
    def test_question_is_told(self):
        from live.brain import Brain, Interrupted, _Session  # noqa: F401
        b = Brain()
        msg = b._message(object(), "and now?", Interrupted("Git rebase takes", question="what is rebase"))
        self.assertIn('You were answering: "what is rebase"', msg)
        self.assertIn('"Git rebase takes"', msg)
        msg = b._message(object(), "x", Interrupted("", question="q"))
        self.assertIn("before you said anything", msg)
        msg = b._message(object(), "x", Interrupted("heard"))
        self.assertIn('They heard only: "heard"', msg)
