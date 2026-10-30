"""Tests for live/turntaking.py. Run: .venv/bin/python -m unittest discover -s tests"""
import os
import sys
import unittest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)

from live.turntaking import TurnContext, TurnTaking, classify, compose, estimate_ms, normalize  # noqa: E402

SCENARIO_TURNS = [
    "What's the difference between a process and a thread?",
    "Okay, so which one is cheaper to create?",
    "Huh. And why is that?",
    "Tell me about the history of the Roman Empire.",
    "Actually, just give me the short version.",
    "Can you explain how git rebase works?",
    "Wait, wait, how is that different from merge?",
    "How do I undo my last commit?",
    "No, I meant undo it but keep the changes.",
    "I'm thinking about renaming the main branch to trunk. Is that a bad idea?",
    "Walk me through how a hash map works.",
    "Can you run the tests and tell me if anything's broken?",
]


class TestDecide(unittest.TestCase):
    def test_empty_and_hesitation_ignored(self):
        tt = TurnTaking()
        self.assertEqual(tt.decide(TurnContext("")).kind, "ignore")
        self.assertEqual(tt.decide(TurnContext("  ... ")).kind, "ignore")
        self.assertEqual(tt.decide(TurnContext("Um.")).kind, "ignore")
        self.assertEqual(tt.decide(TurnContext("uh, um")).kind, "ignore")
        self.assertEqual(tt.decide(TurnContext("hi")).kind, "respond")

    def test_backchannel_while_speaking_keeps_reply(self):
        for t in ["Mm-hmm.", "Mhm.", "Uh-huh.", "Yeah.", "Right, right.", "Okay.", "Oh, I see.", "yeah yeah okay"]:
            a = TurnTaking().decide(TurnContext(t, assistant_was_speaking=True))
            self.assertEqual(a.kind, "ignore", t)
            self.assertIsNone(a.filler)

    def test_backchannel_after_silence_is_a_turn(self):
        # s11: "Yeah." after 3.5 s of silence answers the question.
        a = TurnTaking().decide(TurnContext("Yeah.", assistant_was_speaking=False, last_reply="Is that okay?"))
        self.assertEqual(a.kind, "respond")

    def test_real_question_while_speaking_responds(self):
        a = TurnTaking().decide(TurnContext("Wait, how is that different from merge?", assistant_was_speaking=True))
        self.assertEqual(a.kind, "respond")

    def test_merge(self):
        self.assertEqual(TurnTaking().on_user_resumed(TurnContext("I want to")), "merge")


class TestClassify(unittest.TestCase):
    def test_kinds(self):
        k = {t: classify(normalize(t)) for t in SCENARIO_TURNS}
        self.assertEqual(k["No, I meant undo it but keep the changes."], "correction")
        self.assertEqual(k["Actually, just give me the short version."], "shorter")
        self.assertEqual(k["Wait, wait, how is that different from merge?"], "clarify")
        self.assertEqual(k["Huh. And why is that?"], "why")
        self.assertEqual(k["Okay, so which one is cheaper to create?"], "compare")
        self.assertEqual(k["Can you run the tests and tell me if anything's broken?"], "action")
        self.assertEqual(k["I'm thinking about renaming the main branch to trunk. Is that a bad idea?"], "opinion")
        self.assertEqual(k["Walk me through how a hash map works."], "explain")
        self.assertEqual(classify(normalize("Yeah.")), "affirm")
        self.assertEqual(classify(normalize("Mm-hmm.")), "continue")


class TestFillers(unittest.TestCase):
    def test_no_filler_repeats_in_a_session(self):
        tt = TurnTaking()
        got = [tt.decide(TurnContext(t)).filler for t in SCENARIO_TURNS * 3]
        said = [f for f in got if f]
        self.assertEqual(len(said), len(set(said)))
        self.assertTrue(all(f.endswith(",") for f in said))

    def test_estimate_matches_measured_say(self):
        # measured totals (incl. trailing pause), macOS say Flo 190 wpm
        for text, ms in [("Ah, got it, so,", 1599), ("Oh, good question, so basically,", 2591),
                         ("Yeah, let me take a look,", 1726), ("Okay, so,", 1215)]:
            self.assertLess(abs(estimate_ms(text, "say") - ms), 300, text)
        # measured Kokoro af_heart, speed 1.1
        for text, ms in [("Ah, got it, so,", 842), ("Oh okay, sure thing, short version,", 1792),
                         ("Hmm, good question, well,", 1172), ("Mm, anyway,", 747)]:
            self.assertLess(abs(estimate_ms(text) - ms), 350, text)

    def test_compose_has_no_doubled_words(self):
        for kind in ["correction", "shorter", "explain", "affirm", "action", "default"]:
            for text, *_ in compose(kind):
                words = normalize(text).split()
                self.assertEqual(len(words), len(set(words)), text)

    def test_parts_not_reused_until_needed(self):
        tt = TurnTaking()
        fs = [tt.decide(TurnContext(t)).filler for t in SCENARIO_TURNS[:3]]
        firsts = [f.split(",")[0] for f in fs]
        self.assertEqual(len(firsts), len(set(firsts)), fs)

    def test_fillers_vary_across_sessions(self):
        # The first filler of each scenario must not be the same canned text everywhere.
        firsts = {TurnTaking().decide(TurnContext(t)).filler for t in SCENARIO_TURNS}
        self.assertGreaterEqual(len(firsts), 6)

    def test_filler_fits_the_wait(self):
        tt = TurnTaking({"filler_lead_ms": 150, "expected_content_ms": 1350})
        f = tt.decide(TurnContext("How do I undo my last commit?")).filler
        self.assertLess(abs(estimate_ms(f) - 1200), 400)

    def test_longer_filler_after_interruption(self):
        a = TurnTaking().decide(TurnContext("Actually, just give me the short version.")).filler
        b = TurnTaking().decide(TurnContext("Give me the short version.")).filler
        self.assertGreater(estimate_ms(a), estimate_ms(b))

    def test_no_filler_when_fast(self):
        tt = TurnTaking({"filler_lead_ms": 300, "expected_content_ms": 700})
        self.assertIsNone(tt.decide(TurnContext("How do I undo my last commit?")).filler)
        self.assertIsNone(TurnTaking({"fillers": False}).decide(TurnContext("How?")).filler)

    def test_observe_recalibrates(self):
        tt = TurnTaking({"filler_lead_ms": 300, "expected_content_ms": 700, "learn_rate": 1.0})
        self.assertIsNone(tt.decide(TurnContext("How do I undo my last commit?")).filler)
        tt.observe_content_latency(2200)
        self.assertIsNotNone(tt.decide(TurnContext("What is a rebase?")).filler)


if __name__ == "__main__":
    unittest.main()
