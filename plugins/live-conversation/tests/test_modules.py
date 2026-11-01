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

    def _sink(self):
        clock = Clock()
        log = EventLog(clock)
        return clock, log, Sink(clock, log)

    def test_pause_keeps_playing_and_emits_nothing(self):
        clock, log, sink = self._sink()
        sink.write(LOUD, turn=1)
        sink.write(LOUD, turn=1)
        sink.tick(0, FRAME)
        clock.advance(FRAME)
        sink.pause()
        self.assertTrue(sink.is_playing())
        self.assertEqual(sink.queued_end, FRAME)               # the second frame is dropped
        self.assertFalse(sink.tick(FRAME, FRAME).any())
        clock.advance(FRAME)
        sink.tick(2 * FRAME, FRAME)
        names = [e.event for e in log.events]
        self.assertEqual(names, ["first_reply_audio"])         # no playback_stopped, no reply_audio_end
        sink.resume()
        self.assertFalse(sink.is_playing())
        self.assertEqual(sink.write(LOUD, turn=1), 2 * FRAME * 1000 / SR)   # plays from now

    def test_flush_clears_pause(self):
        clock, log, sink = self._sink()
        sink.write(LOUD, turn=1)
        sink.pause()
        sink.flush()
        self.assertFalse(sink.paused)
        self.assertFalse(sink.is_playing())

    def test_tags_report_what_plays(self):
        clock, log, sink = self._sink()
        sink.write(LOUD, turn=1, tag=("r1", 0))
        sink.write(LOUD, turn=1, tag=("r1", 1))
        self.assertEqual(sink.tag_at(10), (("r1", 0), 10))
        self.assertEqual(sink.tag_at(FRAME + 5), (("r1", 1), 5))
        self.assertIsNone(sink.tag_at(2 * FRAME))
        clock.advance(FRAME // 2)
        sink.pause()                                           # a pause cuts the tags it dropped
        self.assertIsNone(sink.tag_at(FRAME + 5))
        self.assertEqual(sink.tag_at(10), (("r1", 0), 10))

    def test_duck_gain_applies_as_it_plays(self):
        clock, log, sink = self._sink()
        x = np.full(2 * FRAME, 10000, dtype=np.int16)
        sink.write(x, turn=1)
        sink.gain_db = -12.0
        out = sink.tick(0, FRAME)
        self.assertAlmostEqual(float(out[0]), 10000 * 10 ** (-12 / 20), delta=1)
        sink.gain_db = 0.0
        self.assertEqual(int(sink.tick(FRAME, FRAME)[0]), 10000)   # unduck is instant
        self.assertAlmostEqual(float(sink.heard(FRAME)[0]), 10000 * 10 ** (-12 / 20), delta=1)

    def test_first_reply_audio_can_be_left_to_the_controller(self):
        clock, log, sink = self._sink()
        sink.log_first_audio = False
        sink.write(LOUD, turn=1)
        self.assertEqual([e.event for e in log.events], [])


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

    def test_echo_mixes_the_reply_into_the_mic(self):
        scen = os.path.join(PLUGIN, "bench", "scenarios", "s01_quick_fact")

        def mic(echo):
            log = EventLog(Clock())
            fa = FileAudio(scen, log, realtime=False, echo_gain_db=echo, echo_delay_ms=20)
            fa.sink.write(np.full(SR, 8000, dtype=np.int16), turn=1)     # 1 s of reply from t=0
            frames = []

            async def go():
                async for f in fa.frames():
                    frames.append(f)
                    if len(frames) * FRAME >= SR:
                        break
            asyncio.run(go())
            return np.concatenate(frames).astype(np.float64)

        dry, wet = mic(None), mic(-20.0)
        delay = int(0.020 * SR)
        diff = wet - dry
        self.assertLess(np.abs(diff[:delay]).max(), 2)                      # nothing before the delay
        self.assertAlmostEqual(float(np.median(diff[delay + FRAME:])), 800, delta=2)   # -20 dB of 8000


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
