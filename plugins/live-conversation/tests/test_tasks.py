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
from live.orchestrator import Components, Orchestrator  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
