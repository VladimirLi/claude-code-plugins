"""Tests for live/tts.py. The model test is slow (~20 s, loads Kokoro); run it with LIVE_TTS_MODEL_TEST=1."""
import asyncio
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from live.events import SR  # noqa: E402
from live.tts import (Chunker, KokoroTTS, SayTTS, apply_lexicon, clean_text, end_punct,  # noqa: E402
                      make_tts, shape)


def chunk_all(text, stream=False):
    c = Chunker()
    if not stream:
        return c.push(text) + c.flush()
    out = []
    for i, w in enumerate(text.split(" ")):
        out += c.push(w if i == 0 else " " + w)
    return out + c.flush()


class TestChunker(unittest.TestCase):
    def test_sentences(self):
        self.assertEqual(chunk_all("Canberra. Most people guess Sydney, so you're in good company."),
                         ["Canberra.", "Most people guess Sydney, so you're in good company."])

    def test_first_clause_split_off(self):
        self.assertEqual(chunk_all("Honestly, I'd hold off. Rewrites feel clean, but no."),
                         ["Honestly,", "I'd hold off.", "Rewrites feel clean, but no."])

    def test_only_first_clause(self):
        got = chunk_all("Sure, running them now. This might, maybe, take a while.")
        self.assertEqual(got, ["Sure,", "running them now.", "This might, maybe, take a while."])

    def test_long_first_clause_not_split(self):
        got = chunk_all("There's a brain dump tool for voice prompts and more, plus a cache timer.")
        self.assertEqual(got, ["There's a brain dump tool for voice prompts and more, plus a cache timer."])

    def test_number_comma_not_split(self):
        self.assertEqual(chunk_all("About 1,000 tests ran."), ["About 1,000 tests ran."])

    def test_stream_same_pieces(self):
        t = "Oh nice, how long was the walk? Sounds like you earned a lazy evening."
        self.assertEqual(chunk_all(t, stream=True), chunk_all(t))

    def test_stream_waits_for_char_after_stop(self):
        c = Chunker()
        self.assertEqual(c.push("It is 3."), [])
        self.assertEqual(c.push("5 now. Ok"), ["It is 3.5 now."])

    def test_question_and_ellipsis(self):
        self.assertEqual(chunk_all("Really? Wow... okay"), ["Really?", "Wow...", "okay"])


class TestText(unittest.TestCase):
    def test_clean_markdown(self):
        self.assertEqual(clean_text("**Run** `git status` see [docs](http://x.y)"), "Run git status see docs")

    def test_end_punct(self):
        self.assertEqual(end_punct("Hello there."), ".")
        self.assertEqual(end_punct('He said "no?"'), "?")
        self.assertEqual(end_punct("Oh nice,"), ",")
        self.assertEqual(end_punct("no stop"), "")

    def test_lexicon(self):
        self.assertEqual(apply_lexicon("Run git reset, ugh."), "Run [git](/ɡˈɪʔt/) reset, [ugh](/ˈʌɡ/).")
        self.assertEqual(apply_lexicon("digit gitlab"), "digit gitlab")


class TestShape(unittest.TestCase):
    def tone(self, lead_s, tone_s, tail_s):
        t = np.arange(int(SR * tone_s)) / SR
        return np.concatenate([np.zeros(int(SR * lead_s)), 0.3 * np.sin(2 * np.pi * 220 * t),
                               np.zeros(int(SR * tail_s))]).astype(np.float32)

    def test_trims_lead_and_sets_pause(self):
        y = shape(self.tone(0.4, 0.5, 0.6), pause_ms=300)
        self.assertEqual(y.dtype, np.int16)
        first = np.nonzero(np.abs(y) > 32768 * 10 ** (-45 / 20))[0][0]
        self.assertLess(first / SR, 0.02)                         # leading 400 ms of silence is gone
        self.assertAlmostEqual(len(y) / SR, 0.5 + 0.06 + 0.3 + 0.012, delta=0.02)

    def test_level_and_peak(self):
        y = shape(self.tone(0.0, 0.5, 0.0), pause_ms=0, level_dbfs=-18, peak_dbfs=-1).astype(np.float64) / 32768
        act = y[: SR // 2 - SR // 50]
        self.assertAlmostEqual(20 * np.log10(np.sqrt(np.mean(act ** 2))), -18, delta=1.0)
        self.assertLessEqual(np.abs(y).max(), 10 ** (-1 / 20) + 1e-3)

    def test_no_clicks_at_edges(self):
        y = shape(self.tone(0.1, 0.3, 0.1), pause_ms=100).astype(np.float64) / 32768
        self.assertLess(np.abs(np.diff(y)).max(), 0.5)
        self.assertLess(abs(y[0]), 0.01)

    def test_silence_only(self):
        y = shape(np.zeros(SR // 2, np.float32), pause_ms=100)
        self.assertEqual(len(y), SR // 10)


class TestMake(unittest.TestCase):
    def test_kinds(self):
        self.assertIsInstance(make_tts({"kind": "say"}), SayTTS)
        self.assertIn(type(make_tts()), (KokoroTTS, SayTTS))


@unittest.skipUnless(os.environ.get("LIVE_TTS_MODEL_TEST"), "set LIVE_TTS_MODEL_TEST=1 to load Kokoro")
class TestKokoroModel(unittest.TestCase):
    def test_synthesize_order_and_cache(self):
        async def run():
            tts = KokoroTTS({"prewarm": False})
            await tts.start()
            outs = {}

            async def say(i, text):
                outs[i] = np.concatenate([c async for c in tts.synthesize(text)])
            await asyncio.gather(say(0, "Sure,"), say(1, "running them now."), say(2, "Okay."))
            again = np.concatenate([c async for c in tts.synthesize("Sure,")])
            await tts.close()
            return tts, outs, again
        tts, outs, again = asyncio.run(run())
        for i in range(3):
            self.assertGreater(len(outs[i]), SR // 5)
            self.assertEqual(outs[i].dtype, np.int16)
        self.assertEqual([s["text"] for s in tts.stats[:3]], ["Sure,", "running them now.", "Okay."])
        self.assertTrue(tts.stats[-1]["cache"])
        np.testing.assert_array_equal(again, outs[0])


if __name__ == "__main__":
    unittest.main()
