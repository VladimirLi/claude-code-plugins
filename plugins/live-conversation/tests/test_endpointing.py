"""Tests for the endpointing piece (live/endpointing.py, live/endpointing_model.py)."""
from __future__ import annotations

import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live import endpointing_model as em  # noqa: E402
from live.endpointing import Endpointer, classify_text  # noqa: E402
from live.events import FRAME, SR  # noqa: E402

LOUD = (np.sin(np.arange(FRAME) / 5) * 8000).astype(np.int16)
QUIET = (np.random.RandomState(0).randn(FRAME) * 32768 * 10 ** (-65 / 20)).astype(np.int16)


class FakeText:
    """Stands in for the Azure text probe: returns a fixed transcript after a delay."""

    def __init__(self, text: str, delay_s: float = 0.0) -> None:
        self.text, self.delay_s, self.calls = text, delay_s, 0

    def transcribe(self, pcm):
        self.calls += 1
        time.sleep(self.delay_s)
        return self.text

    def refresh_if_idle(self):
        pass


def make(text: str | None, **cfg) -> Endpointer:
    ep = Endpointer({"text_probe": "off", "smart_turn": False, "warm": False, **cfg})
    if text is not None:
        ep.text = FakeText(text)
    return ep


def run(ep: Endpointer, plan: list[tuple[str, float]], frame_sleep: float = 0.0):
    """Feed (kind, ms) blocks; return [(kind, t_ms, source)]. Sleeps a little per frame so probes finish."""
    out, t = [], 0.0
    for kind, ms in plan:
        for _ in range(int(ms / 20)):
            t += 20
            for d in ep.feed(LOUD if kind == "speech" else QUIET, t):
                out.append((d.kind, d.t_ms, d.source))
            if frame_sleep:
                time.sleep(frame_sleep)
    return out


class TestClassify(unittest.TestCase):
    def test_classes(self):
        cases = {
            "What's the capital of Australia?": "yield",
            "Thanks!": "yield",
            "Yeah.": "yield",
            "Mm-hmm.": "yield",
            "Tell me about the history of the Roman Empire.": "statement",
            "I was wondering if...": "hold",
            "I want to, um...": "hold",
            "I want to, um, change the way the": "hold",
            "So I've been working on this,": "hold",
            "Can you help me with": "hold",
            "М-м.": "short",
            "Got it.": "short",
            "": "none",
            "   ": "none",
            "and the result looks good": "statement_unsure",
            "Who did you go with?": "yield",
            "I love it.": "statement",
            "Yes, I think so.": "statement",
            "I was talking to the": "hold",
        }
        for text, want in cases.items():
            self.assertEqual(classify_text(text), want, text)


class TestEndpointer(unittest.TestCase):
    def test_plain_silence_mode(self):
        ep = Endpointer({"silence_ms": 100})
        out = run(ep, [("speech", 200), ("quiet", 200)])
        self.assertEqual([k for k, *_ in out], ["speech_start", "turn_end"])

    def test_question_ends_fast(self):
        ep = make("What time is it?")
        out = run(ep, [("quiet", 400), ("speech", 1000), ("quiet", 1500)], frame_sleep=0.002)
        ends = [(t, s) for k, t, s in out if k == "turn_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0][1], "semantic_yield")
        self.assertLess(ends[0][0] - 1400, 400)          # yield_ms 250 plus a frame or two

    def test_incomplete_holds_through_long_pause(self):
        ep = make("I was wondering if...")
        out = run(ep, [("quiet", 400), ("speech", 1000), ("quiet", 1500), ("speech", 1000), ("quiet", 600)],
                  frame_sleep=0.002)
        self.assertEqual([k for k, *_ in out], ["speech_start"])    # no cut-off at the 1.5 s pause
        self.assertTrue(ep.in_turn)

    def test_statement_waits_out_sentence_pause(self):
        ep = make("I've been working on this for a couple of months now.")
        out = run(ep, [("quiet", 400), ("speech", 1000), ("quiet", 700), ("speech", 500), ("quiet", 1200)],
                  frame_sleep=0.002)
        kinds = [k for k, *_ in out]
        self.assertEqual(kinds, ["speech_start", "turn_end"])
        t_end = [t for k, t, _ in out if k == "turn_end"][0]
        self.assertGreaterEqual(t_end - 2600, 1000)      # statement_unsure_ms after the last speech

    def test_no_text_falls_back_to_timeout(self):
        ep = make(None)
        out = run(ep, [("speech", 400), ("quiet", 2000)])
        ends = [(t, s) for k, t, s in out if k == "turn_end"]
        self.assertEqual(ends, [(400 + 1600, "vad_timeout")])

    def test_short_blip_does_not_end_pause_or_turn(self):
        ep = make(None, no_text_ms=500)
        out = run(ep, [("speech", 400), ("quiet", 300), ("speech", 20), ("quiet", 300)])
        ends = [t for k, t, _ in out if k == "turn_end"]
        self.assertEqual(ends, [400 + 320 + 200])        # the blip frame does not count as quiet

    def test_one_turn_end_per_speech_start(self):
        ep = Endpointer({"silence_ms": 100})
        out = run(ep, [("speech", 200), ("quiet", 600), ("speech", 200), ("quiet", 600)])
        self.assertEqual([k for k, *_ in out], ["speech_start", "turn_end", "speech_start", "turn_end"])

    def test_hint_mode_uses_stt_text(self):
        ep = Endpointer({"text_probe": "hint", "smart_turn": False, "warm": False})
        run(ep, [("speech", 400), ("quiet", 100)])
        ep.on_transcript("Is it raining?", final=False)
        out = run(ep, [("quiet", 300)])
        self.assertEqual([s for k, _, s in out if k == "turn_end"], ["semantic_yield"])


class TestModelPieces(unittest.TestCase):
    def test_conv_matches_naive(self):
        rng = np.random.RandomState(1)
        x = rng.randn(1, 3, 11).astype(np.float32)
        w = rng.randn(4, 3, 3).astype(np.float32)
        b = rng.randn(4).astype(np.float32)
        y = em.OnnxGraph._op_Conv({"pads": [1, 1], "strides": [2]}, x, w, b)
        xp = np.pad(x, ((0, 0), (0, 0), (1, 1)))
        ref = np.stack([np.array([(xp[0, :, i * 2:i * 2 + 3] * w[o]).sum() + b[o] for i in range(6)])
                        for o in range(4)])[None]
        np.testing.assert_allclose(y, ref, rtol=1e-5, atol=1e-5)

    def test_erf(self):
        import math
        xs = np.linspace(-4, 4, 801)
        np.testing.assert_allclose(em._erf(xs), [math.erf(v) for v in xs], atol=2e-7)

    def test_log_mel_shape_and_range(self):
        x = np.sin(np.arange(SR) * 2 * np.pi * 220 / SR) * 0.3
        f = em.log_mel(em.resample_24k_to_16k(x))
        self.assertEqual(f.shape, (1, 80, 800))
        self.assertTrue(np.isfinite(f).all())

    def test_resample_keeps_tone(self):
        x = np.sin(np.arange(SR) * 2 * np.pi * 440 / SR)
        y = em.resample_24k_to_16k(x)
        self.assertEqual(len(y), 16000)
        k = int(np.argmax(np.abs(np.fft.rfft(y))))
        self.assertEqual(k, 440)

    @unittest.skipUnless(em.model_path(download=False), "Smart Turn model not downloaded")
    def test_smart_turn_runs(self):
        st = em.SmartTurn()
        p = st.predict_24k((np.random.RandomState(0).randn(SR * 2) * 300).astype(np.int16))
        self.assertTrue(0.0 <= p <= 1.0)


if __name__ == "__main__":
    unittest.main()
