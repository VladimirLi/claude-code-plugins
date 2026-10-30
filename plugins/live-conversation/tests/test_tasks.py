"""Task handoff: bin/live-task -> daemon -> mod -> result spoken by the brain."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)
sys.path.insert(0, os.path.join(PLUGIN, "tests"))

from live import tasks  # noqa: E402
from live.brain import Brain  # noqa: E402
from live.events import FRAME, Clock, EventLog  # noqa: E402
from live.orchestrator import ANNOUNCE_CARRY_NOTE, ANNOUNCE_RETRY_NOTE, Components, Orchestrator  # noqa: E402
from live.turntaking import TurnTaking  # noqa: E402
from test_orchestrator import FakeAudio, FakeBrain, FakeSTT, FakeTTS, Nop  # noqa: E402

SCRIPT = os.path.join(PLUGIN, "bin", "live-task")


class TestRequests(unittest.TestCase):
    def test_numbers_and_states(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(tasks.request(d, "Add a  verbose flag"), 1)
            self.assertEqual(tasks.request(d, "Fix the test"), 2)
            self.assertEqual([r["state"] for r in tasks.status(d)], ["waiting", "waiting"])
            self.assertEqual(tasks.status(d)[0]["text"], "Add a verbose flag")
            with self.assertRaises(ValueError):
                tasks.request(d, "  ")

    def test_script_hands_off_and_lists(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run([sys.executable, SCRIPT, "--run-dir", d, "Rename the helper"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertIn("task 1", out)
            out = subprocess.run([sys.executable, SCRIPT, "--run-dir", d, "--status"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertIn("task 1: waiting: Rename the helper", out)
            self.assertEqual(subprocess.run([sys.executable, SCRIPT, "x"], capture_output=True,
                                            env={**os.environ, "LIVE_RUN_DIR": ""}).returncode, 2)

    def test_command_is_shell_quoted(self):
        cmd = tasks.command("/a b/python", "/a b/bin/live-task", "/tmp/run dir")
        self.assertEqual(cmd, "'/a b/python' '/a b/bin/live-task' --run-dir '/tmp/run dir'")


class TestBridge(unittest.TestCase):
    def test_handoff_event_then_result(self):
        with tempfile.TemporaryDirectory() as d:
            log = EventLog(Clock())
            b = tasks.TaskBridge(d, log)
            n = tasks.request(d, "Add a verbose flag")
            self.assertEqual(b.poll(), [])
            ev = [e for e in log.events if e.event == "task_handoff"]
            self.assertEqual((ev[0].extra["task"], ev[0].extra["text"]), (n, "Add a verbose flag"))
            self.assertEqual(b.poll(), [])                       # handed once
            self.assertEqual(tasks.status(d)[0]["state"], "running")
            p = os.path.join(d, "tasks", f"{n}.result.json")
            with open(p, "w") as f:
                f.write('{"answer": "Done')                     # the mod is still writing
            self.assertEqual(b.poll(), [])
            with open(p, "w") as f:
                json.dump({"task": n, "reason": "answer", "answer": "Added **--verbose**; tests pass."}, f)
            res = b.poll()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["text"], "Add a verbose flag")
            self.assertEqual(b.poll(), [])
            self.assertEqual(tasks.status(d)[0]["state"], "finished")
            prompt = tasks.result_prompt(res[0])
            self.assertTrue(prompt.startswith("[Task 1 finished."))
            self.assertIn("tests pass", prompt)

    def test_result_prompt_aborted_and_long(self):
        p = tasks.result_prompt({"task": 3, "reason": "aborted", "text": "x", "answer": "y " * 5000})
        self.assertIn("stopped it", p)
        self.assertLess(len(p), tasks.MAX_ANSWER + 400)


class TestBrainPrompt(unittest.TestCase):
    def test_handoff_section_is_in_the_system_prompt(self):
        b = Brain({"cwd": "/p", "prompt_extra": tasks.handoff_prompt("live-task --run-dir /r")})
        sp = b.system_prompt()
        self.assertIn("/p", sp)
        self.assertIn('live-task --run-dir /r "the task"', sp)
        self.assertNotIn("Handing work", Brain({"cwd": "/p"}).system_prompt())

    def test_investigations_are_handed_off(self):
        # bar 20261030-110521 turn 5: "see if there might be any other issues ... from the logs" was
        # dug into by the voice brain itself; the prompt must name that as work to hand off
        p = tasks.handoff_prompt("x")
        self.assertIn("issues in the logs", p)
        self.assertNotIn("You can still look things up yourself", p)
        self.assertIn("two tool calls at most", p)


def make():
    log = EventLog(Clock())
    audio = FakeAudio(log)
    c = Components(endpointer=Nop(), stt=FakeSTT("hi"), brain=FakeBrain("It worked.", delay=0.05),
                   tts=FakeTTS(0.01), bargein=Nop(), turntaking=TurnTaking({"fillers": False}))
    o = Orchestrator(audio, log, c, {"orchestrator": {"announce_quiet_ms": 100, "keepalive_after_ms": 0}})
    return o, log, audio


async def ticking(audio):
    while True:
        await asyncio.sleep(0.01)
        audio.clock.advance(FRAME)


class TestAnnounce(unittest.TestCase):
    def test_result_is_spoken_when_free(self):
        async def go():
            o, log, audio = make()
            t = asyncio.ensure_future(ticking(audio))
            await asyncio.wait_for(o.announce("[Task 1 finished.]"), 3)
            t.cancel()
            self.assertEqual(o.c.brain.calls[0][0], "[Task 1 finished.]")
            r = o.replies[-1]
            self.assertTrue(r.announce and r.content_started)
            self.assertEqual(len([e for e in log.events if e.event == "x_announce"]), 1)
        asyncio.run(go())

    def test_waits_for_the_user_and_retries_if_cut_before_audio(self):
        async def go():
            o, log, audio = make()
            o.c.brain.delay = 0.3
            t = asyncio.ensure_future(ticking(audio))
            o.in_user_turn = True                               # the user is talking
            a = o.announce("[Task 2 finished.]")
            await asyncio.sleep(0.3)
            self.assertEqual(o.c.brain.calls, [])
            o.in_user_turn = False
            await asyncio.sleep(0.2)                            # announced, brain still thinking
            self.assertEqual(len(o.c.brain.calls), 1)
            o._on_speech_start()                                # user talks before any audio
            self.assertEqual(o.carry, "")                       # never merged into the user's turn
            await asyncio.sleep(0.2)
            o.in_user_turn = False
            await asyncio.wait_for(a, 3)
            t.cancel()
            self.assertEqual(len(o.c.brain.calls), 2)
            self.assertTrue(o.replies[-1].content_started)
        asyncio.run(go())

    def test_bar_cut_twice_then_spoken_once_not_as_a_repeat(self):
        # bar session 20261030-110521: the task 5 result was cut twice before any audio, then
        # spoken as "The investigation came back again with the same finding".
        async def go():
            o, log, audio = make()
            o.c.brain.delay = 0.3
            t = asyncio.ensure_future(ticking(audio))
            a = o.announce("[Task 5 finished.]")
            for _ in range(2):
                while len(o.c.brain.calls) < 1 + _:
                    await asyncio.sleep(0.02)
                o._on_speech_start()                            # the user talks before any audio
                await asyncio.sleep(0.05)
                o.in_user_turn = False
            await asyncio.wait_for(a, 3)
            t.cancel()
            prompts = [c[0] for c in o.c.brain.calls]
            self.assertEqual(len(prompts), 3)
            self.assertEqual(prompts[0], "[Task 5 finished.]")
            for p in prompts[1:]:
                self.assertTrue(p.startswith(ANNOUNCE_RETRY_NOTE))
                self.assertTrue(p.endswith("[Task 5 finished.]"))
            spoken = [r for r in o.replies if r.announce and r.content_started]
            self.assertEqual(len(spoken), 1)                    # heard once
            self.assertFalse(spoken[0].interrupted)             # and completely
        asyncio.run(go())

    def test_cut_result_rides_on_the_next_reply_and_is_not_said_twice(self):
        # replay of the bar: the cut result was told in the reply to "All right, let's see if this
        # works better", then the retry told it a second time.
        async def go():
            o, log, audio = make()
            o.c.brain.delay = 0.3
            t = asyncio.ensure_future(ticking(audio))
            a = o.announce("[Task 5 finished.]")
            while not o.c.brain.calls:
                await asyncio.sleep(0.01)
            o._on_speech_start()                                # the user talks before any audio
            await asyncio.sleep(0.05)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)                # their turn: "hi"
            await asyncio.wait_for(a, 3)
            t.cancel()
            prompts = [c[0] for c in o.c.brain.calls]
            self.assertEqual(len(prompts), 2)                   # no third, separate announcement
            self.assertTrue(prompts[1].startswith(ANNOUNCE_CARRY_NOTE))
            self.assertIn("[Task 5 finished.]", prompts[1])
            self.assertTrue(prompts[1].endswith("hi"))
            self.assertEqual(o.unheard, [])
            self.assertEqual(len([e for e in log.events if e.event == "x_announce_delivered"]), 1)
        asyncio.run(go())

    def test_result_heard_in_one_reply_is_not_carried_by_the_next(self):
        # The user spoke again within announce_quiet_ms of the carrying reply's end: the next reply
        # carried the same result again, and the brain told it a second time (bar issue 5).
        async def go():
            o, log, audio = make()
            o.ocfg["announce_quiet_ms"] = 800
            o.c.brain.delay = 0.3
            t = asyncio.ensure_future(ticking(audio))
            a = o.announce("[Task 5 finished.]")
            while not o.c.brain.calls:
                await asyncio.sleep(0.01)
            o._on_speech_start()
            await asyncio.sleep(0.05)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)                # "hi" carries the result
            r1 = o.reply
            await o.reply_task
            await asyncio.sleep(0.2)                            # the user talks again 200 ms later
            o._on_speech_start()
            await asyncio.sleep(0.05)
            o.in_user_turn = False
            await o._handle_turn(o.label, o.gen)
            await o.reply_task
            await asyncio.wait_for(a, 5)
            t.cancel()
            prompts = [c[0] for c in o.c.brain.calls]
            self.assertTrue(o._heard(r1))
            self.assertEqual(sum(p.startswith(ANNOUNCE_CARRY_NOTE) for p in prompts), 1)
            self.assertEqual(prompts[-1], "hi")
            self.assertEqual(len([e for e in log.events if e.event == "x_announce_delivered"]), 1)
        asyncio.run(go())

    def test_carrying_reply_cut_early_does_not_count_as_heard(self):
        async def go():
            o, log, audio = make()
            o.c.brain.delay = 0.3
            t = asyncio.ensure_future(ticking(audio))
            a = o.announce("[Task 8 finished.]")
            while not o.c.brain.calls:
                await asyncio.sleep(0.01)
            o._on_speech_start()
            await asyncio.sleep(0.05)
            o.in_user_turn = False
            o.c.brain.reply = "One two three four five six seven eight nine ten."
            await o._handle_turn(o.label, o.gen)
            r = o.reply
            t.cancel()                                          # hold the clock: nothing of it plays
            while not r.content_started:
                await asyncio.sleep(0.005)
            o._barge_in()
            t = asyncio.ensure_future(ticking(audio))
            await asyncio.wait_for(a, 3)
            t.cancel()
            prompts = [c[0] for c in o.c.brain.calls]
            self.assertEqual(len(prompts), 3)
            self.assertTrue(prompts[2].startswith(ANNOUNCE_RETRY_NOTE))
        asyncio.run(go())

    def test_no_retry_once_content_was_heard(self):
        async def go():
            o, log, audio = make()
            o.c.brain.delay = 0.05
            t = asyncio.ensure_future(ticking(audio))
            a = o.announce("[Task 6 finished.]")
            while not (o.reply and o.reply.content_started):
                await asyncio.sleep(0.01)
            o._barge_in()                                       # cut after part of it was heard
            await asyncio.wait_for(a, 3)
            t.cancel()
            self.assertEqual(len(o.c.brain.calls), 1)
        asyncio.run(go())

    def test_retry_when_only_a_keepalive_line_was_heard(self):
        async def go():
            o, log, audio = make()
            o.c.brain.delay = 0.5
            t = asyncio.ensure_future(ticking(audio))
            a = o.announce("[Task 7 finished.]")
            while not o.c.brain.calls:
                await asyncio.sleep(0.01)
            o.reply.audio_started = True                        # "One moment." played, no content
            o._barge_in()
            await asyncio.wait_for(a, 3)
            t.cancel()
            self.assertEqual(len(o.c.brain.calls), 2)
            self.assertTrue(o.c.brain.calls[1][0].startswith(ANNOUNCE_RETRY_NOTE))
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
