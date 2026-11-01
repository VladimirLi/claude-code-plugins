import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.events import FRAME, SR, Clock, EventLog  # noqa: E402
from live.recorder import Recorder  # noqa: E402
from live.turntaking import _BODY, _INTERJ, compose  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("live_review", os.path.join(HERE, "bin", "live-review"))
_spec = importlib.util.spec_from_loader("live_review", _loader)
review_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(review_mod)


def session(tmp, mic_fn):
    """6 s session: the assistant speaks 1-5 s; a barge-in fires at 3 s. mic_fn(spk, rng) -> mic."""
    rng = np.random.default_rng(1)
    n = 6 * SR
    spk = np.zeros(n, np.int16)
    spk[SR:5 * SR] = (rng.standard_normal(4 * SR) * 3000).astype(np.int16)
    mic = mic_fn(spk, rng)
    clock = Clock()
    rec = Recorder(tmp, "/work", {})
    log = EventLog(clock, listener=rec.on_event)
    for i in range(0, n, FRAME):
        rec.audio(mic[i:i + FRAME], spk[i:i + FRAME])
        clock.advance(FRAME)
    log.emit("turn_end_detected", 1, t_ms=500.0, source="smart_turn")
    log.emit("x_filler", 1, t_ms=600.0, text="Okay, so,")
    log.emit("x_bargein", 1, t_ms=3000.0, decision="barge_in", reason="long", onset_ms=2400.0)
    log.emit("barge_in_detected", 1, t_ms=3000.0)
    return rec, log


class RecorderTest(unittest.TestCase):
    def test_echo_barge_in_is_flagged_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            # no headphones: the mic hears the speaker 80 ms later, quieter
            def echo(spk, rng):
                d = int(0.08 * SR)
                m = np.zeros_like(spk)
                m[d:] = (spk[:-d] * 0.3).astype(np.int16)
                return m
            rec, log = session(tmp, echo)
            rec.close()
            with open(os.path.join(tmp, "events.jsonl")) as f:
                rows = [json.loads(line) for line in f]
            self.assertTrue(all("t_wall" in r for r in rows))
            report = review_mod.review(tmp)
            self.assertIn("probably false: 1", report)
            self.assertIn("echo", report)
            self.assertTrue(os.path.exists(os.path.join(tmp, "clips", "bargein-01.wav")))
            self.assertEqual(review_mod.load_audio(os.path.join(tmp, "audio.wav")).shape, (6 * SR, 2))

    def test_real_interruption_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            def user(spk, rng):
                m = np.zeros_like(spk)
                m[int(2.4 * SR):int(3.4 * SR)] = (rng.standard_normal(SR) * 4000).astype(np.int16)
                return m
            rec, log = session(tmp, user)
            log.emit("final_transcript", 2, t_ms=3600.0, text="Wait, can you use rebase instead?")
            rec.close()
            report = review_mod.review(tmp)
            self.assertIn("probably false: 0", report)
            self.assertIn("Wait, can you use rebase", report)

    def test_feedback_note_gets_a_clip_and_nearby_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec, log = session(tmp, lambda spk, rng: np.zeros_like(spk))
            rec.close()
            with open(os.path.join(tmp, "events.jsonl")) as f:
                rows = [json.loads(line) for line in f]
            with open(os.path.join(tmp, "feedback.jsonl"), "w") as f:
                f.write(json.dumps({"t_wall": rows[-1]["t_wall"], "text": "it stopped by itself"}) + "\n")
            report = review_mod.review(tmp)
            self.assertIn('"it stopped by itself"', report)
            self.assertIn("barge_in_detected", report)
            self.assertTrue(os.path.exists(os.path.join(tmp, "clips", "feedback-01.wav")))


class FillerTest(unittest.TestCase):
    def test_no_praise_fillers(self):
        # First live use: "good question, so" on nearly every turn sounded canned.
        for kind in set(_BODY) | set(_INTERJ):
            for text, *_ in compose(kind):
                self.assertNotRegex(text.lower(), r"question|good point|great point|good one", text)


if __name__ == "__main__":
    unittest.main()
