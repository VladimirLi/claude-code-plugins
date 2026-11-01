"""Offline tests for every module's interface. Run: .venv/bin/python -m unittest discover -s tests

Each builder may add tests/test_<module>.py; keep this file passing.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

import numpy as np

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)
sys.path.insert(0, os.path.join(PLUGIN, "bench"))

from live.audio_io import FileAudio, Sink  # noqa: E402
from live.bargein import BargeIn  # noqa: E402
from live.brain import Brain  # noqa: E402
from live.endpointing import Endpointer  # noqa: E402
from live.events import FRAME, SR, Clock, EventLog  # noqa: E402
from live.stt import BaseSTT  # noqa: E402
from live.tts import Chunker  # noqa: E402
from live.turntaking import TurnContext, TurnTaking  # noqa: E402

LOUD = (np.sin(np.arange(FRAME) / 5) * 8000).astype(np.int16)
QUIET = np.zeros(FRAME, np.int16)


class TestEndpointer(unittest.TestCase):
    def test_start_then_end(self):
        ep = Endpointer({"silence_ms": 100})
        kinds = [d.kind for i in range(10) for d in ep.feed(LOUD, i * 20)]
        kinds += [d.kind for i in range(10) for d in ep.feed(QUIET, 200 + i * 20)]
        self.assertEqual(kinds, ["speech_start", "turn_end"])


class TestBargeIn(unittest.TestCase):
    def test_fires_once_while_speaking(self):
        bi = BargeIn({"min_ms": 40})
        out = [bi.feed(LOUD, i * 20, True) for i in range(10)]
        self.assertEqual(out.count("barge_in"), 1)
        self.assertIsNone(BargeIn().feed(LOUD, 0, False))


class TestChunker(unittest.TestCase):
    def test_sentences(self):
        c = Chunker()
        got = c.push("Hello there. How are") + c.push(" you? Fine") + c.flush()
        self.assertEqual(got, ["Hello there.", "How are you?", "Fine"])


class TestTurnTaking(unittest.TestCase):
    def test_empty_ignored(self):
        self.assertEqual(TurnTaking().decide(TurnContext("")).kind, "ignore")
        self.assertEqual(TurnTaking().decide(TurnContext("hi")).kind, "respond")


class TestSTTBuffer(unittest.TestCase):
    def test_preroll(self):
        s = BaseSTT({"preroll_ms": 20})
        for _ in range(5):
            s.feed(QUIET)
        s.begin()
        s.feed(LOUD)
        self.assertEqual(len(s.utterance()), 2 * FRAME)


class TestSink(unittest.TestCase):
    def test_schedule_and_flush(self):
        clock = Clock()
        log = EventLog(clock)
        sink = Sink(clock, log)
        self.assertEqual(sink.write(LOUD, turn=1), 0.0)
        self.assertEqual(sink.write(LOUD, turn=1), FRAME * 1000 / SR)
        clock.advance(FRAME)
        sink.flush("x")
        self.assertFalse(sink.is_playing())
        names = [e.event for e in log.events]
        self.assertEqual(names, ["first_reply_audio", "playback_stopped"])


class TestFileAudio(unittest.TestCase):
    def test_single_turn_scenario_runs_and_ends(self):
        log = EventLog(Clock())
        fa = FileAudio(os.path.join(PLUGIN, "bench", "scenarios", "s01_quick_fact"), log, realtime=False)
        fa.NO_REPLY_S = 1.0

        async def go():
            n = 0
            async for _ in fa.frames():
                n += 1
            return n
        n = asyncio.run(go())
        self.assertGreater(n, 0)
        ev = [e.event for e in log.events]
        self.assertIn("user_speech_end", ev)
        self.assertIn("x_no_reply", ev)
        with tempfile.TemporaryDirectory() as d:
            fa.save(d)
            self.assertTrue(os.path.exists(os.path.join(d, "timeline.wav")))


class TestBrainArgv(unittest.TestCase):
    def test_stream_json(self):
        argv = Brain()._argv()
        self.assertIn("stream-json", argv)
        self.assertEqual(argv[:2], ["claude", "-p"])


class TestBlindAnonymize(unittest.TestCase):
    def test_strips_identity(self):
        import blind
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "s"), os.path.join(d, "o")
            os.makedirs(src)
            with open(os.path.join(src, "events.jsonl"), "w") as f:
                f.write(json.dumps({"t_ms": 1.234, "event": "turn_end_detected", "turn": 1, "source": "semantic_vad",
                                    "model": "gpt-realtime"}) + "\n")
                f.write(json.dumps({"t_ms": 2, "event": "x_secret", "turn": 1}) + "\n")
            json.dump({"turns": [{"role": "assistant", "text_generated": "I'm Claude, from /Users/x/y", "voice": "v"}]},
                      open(os.path.join(src, "transcript.json"), "w"))
            open(os.path.join(src, "run.json"), "w").write("{}")
            blind.anonymize(src, dst)
            self.assertEqual(sorted(os.listdir(dst)), ["events.jsonl", "transcript.json"])
            ev = [json.loads(l) for l in open(os.path.join(dst, "events.jsonl"))]
            self.assertEqual(ev, [{"t_ms": 0.0, "event": "turn_end_detected", "turn": 1}])
            t = json.load(open(os.path.join(dst, "transcript.json")))["turns"][0]
            self.assertEqual(t, {"role": "assistant", "text_generated": "I'm [redacted], from [path]"})


if __name__ == "__main__":
    unittest.main()
