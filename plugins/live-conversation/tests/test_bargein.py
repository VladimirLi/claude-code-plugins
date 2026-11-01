"""Tests for live/bargein.py. Run: .venv/bin/python -m unittest discover -s tests

Synthetic signals check each rule; the bench scenario WAVs (if present) check the
real cases: s08/s09 interruptions barge in, the s13 "Mm-hmm" never does.
"""
import json
import os
import sys
import unittest

import numpy as np

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)

from live.bargein import BargeIn, is_backchannel  # noqa: E402
from live.events import FRAME, SR  # noqa: E402

SCEN = os.path.join(PLUGIN, "bench", "scenarios")
RNG = np.random.default_rng(1)


def noise(ms, db=-65.0):
    n = int(SR * ms / 1000)
    return (RNG.normal(0, 32768 * 10 ** (db / 20), n)).astype(np.int16)


def hum(ms, f0=180.0, db=-14.0):
    """Closed-mouth hum: harmonics of f0 falling off fast, nothing above 1 kHz."""
    t = np.arange(int(SR * ms / 1000)) / SR
    x = sum((0.6 ** k) * np.sin(2 * np.pi * f0 * (k + 1) * t) for k in range(4))
    x = x / np.sqrt(np.mean(x * x)) * 32768 * 10 ** (db / 20)
    return x.astype(np.int16)


def vowel(ms, f0=180.0, db=-14.0):
    """Open vowel: harmonics with strong energy around 1-2.5 kHz."""
    t = np.arange(int(SR * ms / 1000)) / SR
    x = np.zeros_like(t)
    for k in range(1, 15):
        f = f0 * k
        amp = 1.0 if 600 <= f <= 2500 else 0.25
        x += amp * np.sin(2 * np.pi * f * t)
    x = x / np.sqrt(np.mean(x * x)) * 32768 * 10 ** (db / 20)
    return x.astype(np.int16)


def fricative(ms, db=-22.0):
    """'s': high-passed noise."""
    n = int(SR * ms / 1000)
    x = RNG.normal(0, 1, n + 1)
    x = np.diff(np.diff(np.concatenate([[0], x])))[:n]   # strong high-pass
    x = x / np.sqrt(np.mean(x * x)) * 32768 * 10 ** (db / 20)
    return x.astype(np.int16)


def run(bi, x, speaking=True, t0=0.0):
    """Feed x frame by frame; returns [(decision, t_ms)]."""
    out = []
    for i in range(len(x) // FRAME):
        t = t0 + (i + 1) * FRAME * 1000.0 / SR
        d = bi.feed(x[i * FRAME:(i + 1) * FRAME], t, speaking)
        if d:
            out.append((d, t))
    return out


def kinds(dec):
    return [d for d, _ in dec]


class TestRules(unittest.TestCase):
    def test_silence_and_noise_never_fire(self):
        bi = BargeIn()
        self.assertEqual(run(bi, noise(3000)), [])
        # steady loud hiss (fan) after a quiet start: becomes the floor, not speech
        bi = BargeIn()
        self.assertNotIn("barge_in", kinds(run(bi, np.concatenate([noise(500), noise(4000, -38)]))))

    def test_not_speaking_never_fires(self):
        bi = BargeIn()
        x = np.concatenate([noise(500), vowel(800)])
        self.assertEqual(run(bi, x, speaking=False), [])

    def test_open_vowel_barges_in_fast(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), vowel(400), noise(500)]))
        self.assertEqual(kinds(dec)[-1], "barge_in")
        self.assertEqual(bi.last["reason"], "open")
        self.assertLessEqual(dec[-1][1] - 500, 80)

    def test_fricative_barges_in_fast(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), fricative(100), hum(200), noise(400)]))
        self.assertIn("barge_in", kinds(dec))
        self.assertEqual(bi.last["reason"], "fric")

    def test_short_hum_is_a_backchannel(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), hum(450), noise(1000)]))
        self.assertEqual(kinds(dec), ["duck", "resume"])
        self.assertLessEqual(dec[0][1] - 500, 80)            # pause starts fast

    def test_long_hum_barges_in(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), hum(900), noise(500)]))
        self.assertEqual(kinds(dec), ["duck", "barge_in"])
        self.assertEqual(bi.last["reason"], "long")

    def test_two_bursts_barge_in(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), hum(300), noise(250), hum(300), noise(500)]))
        self.assertEqual(kinds(dec), ["duck", "barge_in"])
        self.assertEqual(bi.last["reason"], "burst")

    def test_speech_soon_after_resume_barges_in(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), hum(300), noise(500), hum(200), noise(500)]))
        self.assertEqual(kinds(dec), ["duck", "resume", "barge_in"])

    def test_fires_once(self):
        bi = BargeIn()
        dec = run(bi, np.concatenate([noise(500), vowel(2000)]))
        self.assertEqual(kinds(dec).count("barge_in"), 1)

    def test_no_duck_when_disabled(self):
        bi = BargeIn({"duck": False})
        dec = run(bi, np.concatenate([noise(500), hum(450), noise(1000)]))
        self.assertEqual(dec, [])

    def test_transcript_settles_open_episode(self):
        x = np.concatenate([noise(500), hum(200)])
        bi = BargeIn()
        run(bi, x)
        bi.on_transcript("Mm-hmm.")
        self.assertEqual(kinds(run(bi, hum(20), t0=10000)), ["resume"])
        bi = BargeIn()
        run(bi, x)
        bi.on_transcript("Wait, what about merge?")
        self.assertEqual(kinds(run(bi, hum(20), t0=10000)), ["barge_in"])

    def test_reply_starting_over_the_user_stops_at_once(self):
        bi = BargeIn()
        x = np.concatenate([noise(500), vowel(400)])
        self.assertEqual(run(bi, x, speaking=False), [])
        dec = run(bi, vowel(100), speaking=True, t0=900)
        self.assertEqual(dec[0], ("barge_in", 900 + 20))

    def test_reply_after_the_user_finished_is_not_stopped(self):
        bi = BargeIn()
        x = np.concatenate([noise(500), vowel(800), noise(200)])   # user done, reply starts 200 ms later
        run(bi, x, speaking=False)
        self.assertEqual(run(bi, noise(1000), speaking=True, t0=1500), [])

    def test_legacy_min_ms(self):
        self.assertEqual(BargeIn({"min_ms": 40}).cfg["long_ms"], 40)


class TestNoiseEvents(unittest.TestCase):
    def test_click_and_fan_onset_do_not_barge_in(self):
        bi = BargeIn()
        click = noise(20, -6)
        x = np.concatenate([noise(500), click, noise(500), noise(3000, -38)])
        self.assertNotIn("barge_in", kinds(run(bi, x)))


class TestAfterInterruptMetric(unittest.TestCase):
    def test_repeat_and_restart(self):
        sys.path.insert(0, os.path.join(PLUGIN, "bench"))
        import piece_metrics as pm
        r = pm._after_interrupt("A hash map stores key value pairs", "A hash map stores key value pairs by hashing",
                                "A hash map stores key value pairs by hashing the key")
        self.assertTrue(r["next_reply_restarts"])
        self.assertGreater(r["next_reply_repeat_ratio"], 0.5)
        r = pm._after_interrupt("Git rebase takes your commits", "Git rebase takes your commits and replays them",
                                "Merge keeps both histories and adds a join commit")
        self.assertFalse(r["next_reply_restarts"])
        self.assertEqual(r["next_reply_repeat_ratio"], 0.0)


class TestBackchannelText(unittest.TestCase):
    def test_words(self):
        for t in ["Mm-hmm.", "Mhm.", "mm hmm", "Uh-huh", "Yeah.", "okay, right", "Hmm."]:
            self.assertTrue(is_backchannel(t), t)
        for t in ["Wait, wait, how is that different from merge?", "Actually, just give me the short version.",
                  "No, I meant undo it", "", "stop"]:
            self.assertFalse(is_backchannel(t), t)


@unittest.skipUnless(os.path.isdir(SCEN), "bench scenarios not built")
class TestScenarioAudio(unittest.TestCase):
    """The real user turns, played over the assistant from a quiet start, 3 frame phases."""

    def turn(self, sid, n):
        from live.audio_io import read_wav
        sc = json.load(open(os.path.join(SCEN, sid, "scenario.json")))
        t = sc["turns"][n - 1]
        return read_wav(os.path.join(SCEN, sid, t["audio"]["24k"]["file"])), t["audio"]["24k"]["speech_start_sample"]

    def decide(self, sid, n, phase):
        from live.audio_io import read_wav
        floor = read_wav(os.path.join(SCEN, "noise_floor_24k.wav"))
        w, ss = self.turn(sid, n)
        lead = SR + phase
        x = np.concatenate([floor[:lead], w, floor[:2 * SR]])
        bi = BargeIn()
        onset = (lead + ss) * 1000.0 / SR
        return [(d, t - onset) for d, t in run(bi, x)]

    def test_interruptions_barge_in(self):
        limits = {("s08_bargein_early", 2): 200, ("s09_bargein_late", 2): 750}
        for (sid, n), lim in limits.items():
            for phase in (0, 160, 320):
                dec = self.decide(sid, n, phase)
                b = [t for d, t in dec if d == "barge_in"]
                self.assertTrue(b, (sid, phase, dec))
                self.assertLessEqual(b[0], lim, (sid, phase, dec))

    def test_mm_hmm_is_not_an_interruption(self):
        for phase in (0, 160, 320):
            dec = self.decide("s13_backchannel", 2, phase)
            self.assertEqual([d for d, _ in dec], ["duck", "resume"], (phase, dec))

    def test_every_sentence_barges_in(self):
        """Every scripted user sentence (not the one-word backchannels) stops the assistant."""
        from live.audio_io import read_wav  # noqa: F401
        for sid in sorted(os.listdir(SCEN)):
            p = os.path.join(SCEN, sid, "scenario.json")
            if not os.path.exists(p):
                continue
            for t in json.load(open(p))["turns"]:
                if is_backchannel(t["text"]):
                    continue
                dec = self.decide(sid, t["turn"], 0)
                b = [x for d, x in dec if d == "barge_in"]
                self.assertTrue(b, (sid, t["turn"], dec))
                self.assertLessEqual(b[0], 750, (sid, t["turn"], dec))


if __name__ == "__main__":
    unittest.main()
