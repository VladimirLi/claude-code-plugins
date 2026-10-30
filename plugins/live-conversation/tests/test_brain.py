"""brain piece tests: markdown cleaning, stream parsing, interrupt, fastest-process pick.

Uses tests/fake_claude.py instead of the real CLI, so they run offline in about 3 s.
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from live.brain import Brain, Interrupted, _Cleaner, spoken  # noqa: E402
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
    def test_flags(self):
        b = Brain({"cwd": "/x/proj"})
        argv = b._argv()
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn('{"disableAllHooks": true}', argv)
        sp = argv[argv.index("--system-prompt") + 1]
        self.assertIn("/x/proj", sp)
        self.assertIn("markdown", sp)


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
