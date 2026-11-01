import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import wave

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from live.events import FRAME, SR, Clock, EventLog  # noqa: E402
from live.recorder import Recorder  # noqa: E402

_spec = importlib.util.spec_from_file_location("replay_session", os.path.join(HERE, "bench", "replay_session.py"))
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def write_stereo(path, left, right):
    st = np.empty(2 * len(left), np.int16)
    st[0::2], st[1::2] = left, right
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(st.tobytes())


class ReplayPureTest(unittest.TestCase):
    def test_left_channel_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = np.arange(1000, dtype=np.int16)
            right = -np.arange(1000, dtype=np.int16) - 1
            p = os.path.join(tmp, "a.wav")
            write_stereo(p, left, right)
            np.testing.assert_array_equal(rs.read_left(p), left)

    def test_frames_and_tail(self):
        mic = np.ones(FRAME * 2 + 10, np.int16)
        self.assertEqual(rs.total_frames(len(mic), 0.1), 3 + 5)
        last = rs.mic_frame(mic, FRAME * 2)
        self.assertEqual(int(last[:10].sum()), 10)
        self.assertEqual(int(last[10:].sum()), 0)
        self.assertEqual(int(rs.mic_frame(mic, FRAME * 5).sum()), 0)
        self.assertAlmostEqual(rs.frame_due(100.0, 0), 100.02)
        self.assertAlmostEqual(rs.frame_due(100.0, SR), 101.02)

    def test_schedule_fires_once_in_order(self):
        s, got = rs.Schedule(), []
        s.at(40.0, lambda: got.append("b"))
        s.at(0.0, lambda: got.append("a"))
        s.at(40.0, lambda: got.append("c"))
        self.assertEqual(s.fire(0.0), 1)
        self.assertEqual(s.fire(20.0), 0)
        self.assertEqual(s.fire(40.0), 2)
        self.assertEqual(s.fire(1e9), 0)
        self.assertEqual(got, ["a", "b", "c"])

    def test_recorded_tasks_and_config(self):
        ev = [{"t_ms": 0.0, "event": "task_result", "task": 5, "reason": "answer"},
              {"t_ms": 340.0, "event": "x_announce",
               "text": '[Task 5 finished. The task was: "Read-only investigation, do not change'}]
        t = rs.recorded_tasks(ev)
        self.assertEqual(t, [{"t_ms": 0.0, "task": 5, "reason": "answer",
                              "text": "Read-only investigation, do not change"}])
        rec = {"brain": {"cwd": "/old", "prompt_extra": "X" + rs.HANDOFF_HEAD + " old cmd /real/run"},
               "stt": {"kind": "azure"}}
        cfg = rs.replay_config(rec, {"stt": {"kind": "whisper"}, "bargein": {"a": 1}}, "/new", "CMD --run-dir /tmp/r")
        self.assertEqual(cfg["brain"]["cwd"], "/new")
        self.assertTrue(cfg["brain"]["prompt_extra"].startswith("X" + rs.HANDOFF_HEAD))
        self.assertNotIn("/real/run", cfg["brain"]["prompt_extra"])
        self.assertIn("CMD --run-dir /tmp/r", cfg["brain"]["prompt_extra"])
        self.assertEqual(cfg["stt"], {"kind": "whisper"})
        self.assertEqual(cfg["bargein"], {"a": 1})
        self.assertEqual(rec["brain"]["cwd"], "/old")     # recorded config untouched

    def test_replay_audio_records_mic_and_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            mic = (np.arange(FRAME * 3) % 100).astype(np.int16)
            rec = Recorder(tmp, "/w", {})
            log = EventLog(Clock(), listener=rec.on_event)
            sched, fired = rs.Schedule(), []
            sched.at(40.0, lambda: fired.append(log.clock.ms))
            audio = rs.ReplayAudio(log, mic, recorder=rec, tail_s=0.04, realtime=False, schedule=sched)
            audio.sink.write(np.full(FRAME, 7, np.int16))

            async def go():
                return [f async for f in audio.frames()]
            frames = asyncio.run(go())
            rec.close()
            self.assertEqual(len(frames), 5)
            self.assertTrue(audio.done)
            self.assertEqual(fired, [40.0])
            with wave.open(os.path.join(tmp, "audio.wav")) as w:
                st = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2)
            np.testing.assert_array_equal(st[:FRAME * 3, 0], mic)
            self.assertTrue((st[:FRAME, 1] == 7).all() and (st[FRAME:, 1] == 0).all())
            with open(os.path.join(tmp, "events.jsonl")) as f:
                rows = [json.loads(x) for x in f]
            self.assertIn("session_start", [r["event"] for r in rows])


if __name__ == "__main__":
    unittest.main()
