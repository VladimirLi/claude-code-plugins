"""Regression scenarios for live/floor.py (FLOOR-SPEC.md). Run: .venv/bin/python -m unittest discover -s tests

Each scenario is a small event script run on the simulated clock in floor_driver.py. The invariant
checker runs after every event and at quiescence, so every scenario also checks I1-I8, I11-I13.

Timeline of the default question `ask(sim)`: speech 0-1000 ms, turn_end 1600, final 1900, so
start_reply at 1900; first piece "The logs show no errors." (1500 ms) at 2201, playing about
2220-3720; second piece "Everything passed." (1500 ms) until about 5220.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from floor_driver import Plan, Sim  # noqa: E402

from live import floor as F  # noqa: E402

Q1 = "What is in the logs?"
LONG = Plan(pieces=[(f"Sentence number {i} of the answer.", 2000) for i in range(6)])


def ask(sim, g=1, start=0, text=Q1, end=None, **kw):
    return sim.say(g, start, start + 1000 if end is None else end, text, **kw)


def played_between(sim, rid, t0, t1):
    return [t for t, e in sim.events if isinstance(e, F.Played) and e.rid == rid and t0 <= t <= t1]


class FloorCase(unittest.TestCase):
    def assertOneAnswer(self, sim, question=Q1):
        self.assertEqual(sim.questions(), [question])
        self.assertEqual(sim.ends(), {"r1": "drained"})
        self.assertEqual(sim.ops(F.StopReply), [])


# -- the sweep bugs (sweep-bugs.md) ----------------------------------------------------------------

class TestSweepBugs(FloorCase):
    def test_bug01_cap_play_over_noise_then_detector_ducks_not_stops(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 4000, "", interims=[(2800, "")])      # noise from before the first audio
        sim.detector(3600, "barge_in", "long")                 # fires on that noise once it plays
        sim.run()
        self.assertOneAnswer(sim)
        self.assertTrue(sim.ops(F.ResetDetector))
        self.assertTrue(sim.ops(F.Duck))
        self.assertEqual(sim.emits("barge_in_detected"), [])

    def _released_then(self, text, cfg=None):
        sim = Sim(cfg)
        ask(sim)
        sim.say(2, 2000, 4000, text, interims=[(2800, "")], truth="")
        return sim.run()

    def test_bug02_backchannel_after_cap_play_is_not_a_restart(self):
        self.assertOneAnswer(self._released_then("Okay."))

    def test_bug02_hallucination_after_cap_play_is_not_a_restart(self):
        self.assertOneAnswer(self._released_then("Thank you."))

    def test_bug02_speaker_echo_after_cap_play_is_not_a_restart(self):
        self.assertOneAnswer(self._released_then("The logs show no errors.", {"speakers": True}))

    def test_bug03_second_part_before_audio_joins_and_keeps_the_first(self):
        sim = Sim(plans=Plan(first_ms=2000))
        ask(sim, text="What are you doing?")
        sim.say(2, 2300, 3000, "in the repo")
        sim.run()
        self.assertEqual(sim.questions(), ["What are you doing?", "What are you doing? in the repo"])
        self.assertEqual(len({a.label for a in sim.starts()}), 1)

    def test_bug04_resolved_newer_noise_does_not_hold_the_reply(self):
        sim = Sim()
        ask(sim, final_ms=2000)                                # final at 3600
        sim.say(2, 1800, 2200, "", final_ms=100)               # '' back at 2900, first
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(sim.ops(F.Pause), [])
        first = played_between(sim, "r1", 0, 1e9)[0]
        self.assertLessEqual(first, 3600 + 400)

    def test_bug05_announcement_heard_in_a_user_reply_is_not_carried_again(self):
        sim = Sim()
        sim.at(0, F.TaskResult("t1", "Task 1 finished: tests pass."))
        sim.say(1, 900, 1300, "hi there")                      # before the announcement's audio
        sim.say(2, 5500, 6000, "and what about lint")          # right after r2 ends
        sim.run()
        kinds = [(a.kind, [c["id"] for c in a.carries]) for a in sim.starts()]
        self.assertEqual(kinds, [("announce", ["t1"]), ("answer", ["t1"]), ("answer", [])])
        self.assertEqual(len(sim.floor_log("announce_heard")), 1)

    def test_bug08_click_on_content_resolves_as_blip_and_plays_on(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600, "barge_in", "open")                 # 40 ms click, no speech_start
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(len(sim.emits("playback_paused")), 1)
        self.assertEqual(len(sim.emits("playback_resumed")), 1)

    def test_bug09_hums_are_no_words(self):
        m = F.make_meaningful()
        for t in ["Hmm.", "Mmmm.", "Mm.", "Mm-hmm.", "Ah.", "Oh.", "Hmmm, um."]:
            self.assertEqual(m(t, []), "", t)

    def test_bug10_newer_noise_final_first_then_words_final_joins(self):
        sim = Sim(plans=Plan(first_ms=2500))
        ask(sim)
        sim.say(2, 2200, 3000, "and the errors too", final_ms=400)            # final 4000
        sim.say(3, 3200, 3300, "", turn_end_ms=3500, final_ms=100)            # '' at 3600, first
        sim.run()
        self.assertEqual(sim.questions(), [Q1, Q1 + " and the errors too"])

    def test_bug11_announcement_waits_for_a_pending_final(self):
        sim = Sim()
        ask(sim)
        sim.detector(2560)
        sim.say(2, 2600, 2900, "", final_ms=1500)              # '' at 5000
        sim.at(2700, F.TaskResult("t1", "Task 1 finished."))
        sim.run()
        starts = sim.ops(F.StartReply)
        self.assertEqual([a.kind for _, a in starts], ["answer", "announce"])
        self.assertEqual(sim.ends()["r1"], "drained")
        r1_end = [t for t, f in sim.floor_log("end") if f["rid"] == "r1"][0]
        self.assertGreaterEqual(starts[1][0], max(5000, r1_end) + 800)

    def test_bug12_keepalive_in_flight_during_a_hold_never_plays(self):
        sim = Sim(plans=Plan(pieces=[("Found it.", 1000)], first_ms=9000))
        ask(sim)                                               # keepalive due at 6400, piece at 6600
        sim.say(2, 6500, 6800, "")                             # noise holds the gate at 6500
        sim.run()
        self.assertEqual(sim.ops(F.Keepalive)[0][0], 6400)
        self.assertNotIn("k1", sim.played_pieces("r1"))
        self.assertOneAnswer(sim)

    def test_bug14_older_noise_final_after_reply_start_does_not_restart(self):
        sim = Sim()
        sim.say(1, 0, 300, "", final_ms=2500)                  # '' at 3400
        ask(sim, g=2, start=1000, final_ms=200)                # final 2800
        sim.run()
        self.assertOneAnswer(sim)

    def test_bug15_new_segment_restarts_the_cap(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 2300, "", final_ms=2000)              # noise, '' at 4900
        sim.say(3, 3400, 4800, "and also the config", interims=[(4400, "and also")])
        sim.run()
        r1 = sim.starts()[0].rid
        self.assertEqual(played_between(sim, r1, 3400, 5700), [])

    def test_bug16_older_noise_final_after_a_join_keeps_the_first_question(self):
        sim = Sim(plans=Plan(first_ms=3000))
        ask(sim)
        sim.say(2, 2100, 2300, "", final_ms=2500)              # '' at 5400
        sim.say(3, 2500, 3200, "and the errors")               # final 3900
        sim.run()
        self.assertEqual(sim.questions(), [Q1, Q1 + " and the errors"])

    def test_bug17_hallucination_on_paused_noise_plays_on(self):
        sim = Sim()
        ask(sim)
        sim.detector(2560)
        sim.say(2, 2600, 3000, "Thank you.", interims=[(3300, "Thank you.")], truth="")
        sim.run()
        self.assertOneAnswer(sim)

    def test_bug20_blip_on_first_audio_resolves_and_plays_on(self):
        sim = Sim()
        ask(sim)
        sim.detector(2240)
        sim.run()
        self.assertOneAnswer(sim)

    def test_bug21_barge_in_after_turn_end_before_final(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2500, 2800, "", final_ms=600)               # turn_end 3400, '' at 4000
        sim.detector(3500)                                     # no open segment: a blip
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(len(sim.emits("playback_resumed")), 1)

    def test_bug22_announcement_waits_for_a_blip_and_the_reply(self):
        sim = Sim()
        ask(sim)
        sim.at(2300, F.TaskResult("t1", "Task 1 finished."))  # after r1 started: r1 does not carry it
        sim.detector(2500)
        sim.run()
        starts = sim.ops(F.StartReply)
        self.assertEqual([a.kind for _, a in starts], ["answer", "announce"])
        self.assertEqual(sim.ends()["r1"], "drained")

    def test_bug23_noise_turn_end_mid_reply_is_no_second_first_audio(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2600, 2700, "")
        sim.run()
        self.assertEqual(len(sim.emits("first_reply_audio")), 1)
        self.assertEqual(len(sim.emits("turn_end_detected")), 1)

    def test_bug24_cap_waits_for_an_interim_then_words_hold_until_final(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 3900, "and also the config", interims=[(3600, "and also the")])
        sim.run()
        self.assertEqual(sim.played_pieces("r1"), [])
        self.assertEqual(sim.questions(), [Q1, Q1 + " and also the config"])

    def test_bug25_cough_on_content_pauses_and_plays_on(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600, "barge_in", "fric")
        sim.say(2, 2640, 2800, "")                             # '' at 3700
        sim.run()
        self.assertOneAnswer(sim)
        plays = [a for _, a in sim.ops(F.Play)]
        self.assertEqual([p.rewind_ms for p in plays], [380.0])   # paused 1.1 s: 500, clamped to the piece
        # bug 18 too: one first_reply_audio for the label, at the first content audio
        self.assertEqual(len(sim.emits("first_reply_audio")), 1)


# -- the critics' orderings (critic-orderings.md) --------------------------------------------------

class TestOrderings(FloorCase):
    def test_o01_slow_first_final_joins_in_segment_order(self):
        sim = Sim()
        sim.say(1, 0, 1000, "check the logs", final_ms=1900)   # final 3500
        sim.say(2, 1800, 2600, "and the errors", final_ms=200)  # final 3400
        sim.run()
        self.assertEqual(sim.questions(), ["and the errors", "check the logs and the errors"])
        self.assertEqual(len({a.label for a in sim.starts()}), 1)

    def test_o02_tentative_play_then_words_interrupt(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 4200, "wait actually use the staging logs",
                interims=[(2800, ""), (3900, "wait actually")])
        sim.run()
        r1, r2 = sim.starts()
        self.assertEqual(r2.question, "wait actually use the staging logs")
        self.assertEqual(r2.context.question, Q1)
        self.assertTrue(r2.context.spoken)
        self.assertNotEqual(r1.label, r2.label)
        self.assertEqual(sim.ends()["r1"], "interrupt")

    def test_o03_continuous_noise_ducks_instead_of_looping(self):
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(2600)
        sim.say(2, 2640, 5000, "", interims=[(3600, ""), (4600, "")])
        sim.detector(4300)                                     # again on the same segment
        sim.detector(6200)                                     # inside the resume guard
        sim.run()
        self.assertEqual(len(sim.emits("playback_paused")), 1)
        self.assertEqual(len(sim.ops(F.ResetDetector)), 2)
        self.assertEqual(sim.ends(), {"r1": "drained"})

    def test_o04_speakers_echo_is_ignored(self):
        sim = Sim({"speakers": True})
        ask(sim)
        sim.detector(2350)
        sim.say(2, 2400, 3500, "The logs show no errors.", interims=[(3300, "the logs show no")], truth="")
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(sim.ops(F.Pause), [])

    def test_o05_announcement_90_percent_heard_then_interrupted_is_heard(self):
        sim = Sim(plans={"Task": Plan(pieces=[("Your tests finished and all passed.", 2000)]), "": Plan()})
        sim.at(0, F.TaskResult("t1", "Task 1 finished: tests pass."))
        sim.say(1, 2900, 3600, "great what next", interims=[(3300, "great what")])
        sim.run()
        a1, r2 = sim.starts()
        self.assertEqual(a1.kind, "announce")
        self.assertEqual(r2.carries, [])
        self.assertEqual(len(sim.floor_log("announce_heard")), 1)

    def test_o05_announcement_opening_heard_before_brain_done_is_requeued(self):
        plan = Plan(pieces=[("Sure, let me check.", 1200), ("The tests pass.", 1500)], gap_ms=5000, done_ms=6000)
        sim = Sim(plans={"Task": plan, "": Plan()})
        sim.at(0, F.TaskResult("t1", "Task 1 finished: tests pass."))
        sim.say(1, 2600, 3200, "wait what was that", interims=[(3000, "wait what")])
        sim.run()
        a1, r2 = sim.starts()[:2]
        self.assertEqual([c["tries"] for c in r2.carries], [1])
        self.assertEqual(len(sim.floor_log("announce_heard")), 1)

    def test_o06_noise_gets_no_label_and_labels_are_never_reused(self):
        sim = Sim()
        sim.say(1, 0, 300, "")
        ask(sim, g=2, start=2000)
        ask(sim, g=3, start=9000, text="And the errors?")
        sim.say(4, 12500, 13500, "no stop", interims=[(13300, "no stop")])
        sim.run()
        labels = [a.label for a in sim.starts()]
        self.assertEqual(labels, ["1", "2", "3"])
        self.assertEqual(sorted(e.label for _, e in sim.emits("turn_end_detected")), ["1", "2", "3"])

    def test_o07_carried_pending_segment_joins_the_new_reply_in_order(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 1700, 2000, "and the errors", final_ms=4000)                    # final 6600
        sim.say(3, 3600, 4400, "use the staging logs", interims=[(4200, "use the")])  # final 5300
        sim.run()
        qs = sim.questions()
        self.assertEqual(qs, [Q1, "use the staging logs", "and the errors use the staging logs"])
        labels = [a.label for a in sim.starts()]
        self.assertNotEqual(labels[0], labels[1])
        self.assertEqual(labels[1], labels[2])

    def test_o08_brain_error_while_paused_carries_the_segment_with_context(self):
        sim = Sim(plans={Q1: Plan(error_ms=1100), "": Plan()})
        ask(sim)
        sim.detector(2560)
        sim.say(2, 2600, 3100, "what about the config")        # final 4000
        sim.run()
        r1, r2 = sim.starts()
        self.assertEqual(sim.ends()["r1"], "error")
        self.assertEqual(r2.question, "what about the config")
        self.assertEqual(r2.context.question, Q1)
        self.assertTrue(r2.context.spoken)

    def test_o09_interim_after_final_is_dropped(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2500, 2700, "")                             # '' at 3600
        sim.at(3700, F.Interim("u2", "wait stop"))
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(sim.ops(F.Pause), [])

    def test_o10_segment_absorbs_blip_pause_and_duck(self):
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(2500)                                     # blip pauses
        sim.at(2540, F.SpeechStart(2, "u2"))                   # absorbs it
        sim.run(until=2600)
        r = sim.f.reply
        g = sim.f.by_utt["u2"]
        self.assertEqual(r.pause_on, {g})
        self.assertEqual(sim.f.segs[g].caused, "pause")
        blip = [s for s in sim.f.segs.values() if s.kind == "blip"][0]
        self.assertEqual(blip.status, "final")
        self.assertNotIn(blip.g, r.pause_on)

    def test_o10_absorbed_ducked_blip_moves_to_the_segment(self):
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(2500)                                     # no-words pause 1, resumes 3300
        sim.detector(4000)                                     # inside the guard: duck only
        sim.at(4040, F.SpeechStart(2, "u2"))
        sim.run(until=4100)
        g = sim.f.by_utt["u2"]
        self.assertEqual(sim.f.reply.ducked_on, {g})
        self.assertEqual(sim.f.reply.gate, "open")

    def test_o11_blip_in_tentative_play_resolves_and_plays(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 2300, "", final_ms=2100)              # turn_end 2900, '' at 5000
        sim.detector(3800)                                     # tentative since 3500; blip
        sim.run()
        self.assertOneAnswer(sim)
        plays = [t for t, _ in sim.ops(F.Play)]
        self.assertEqual(plays, [3500.0, 4600.0])

    def test_o12_cough_turn_end_satisfies_the_cap(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 2100, "", turn_end_ms=2400, final_ms=2100)
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Play)], [3500.0])

    def test_o12_turn_end_after_the_cap_ends_the_wait_at_once(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 2100, "", turn_end_ms=3800, final_ms=1000)
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Play)], [3800.0])

    def test_o13_blip_then_segment_saying_yeah_plays_on(self):
        sim = Sim()
        ask(sim)
        sim.detector(2500)
        sim.say(2, 2540, 2900, "yeah", truth="")
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(len(sim.emits("playback_resumed")), 1)

    def test_o14_after_two_no_word_pauses_words_still_pause_on_interim(self):
        sim = Sim(plans=Plan(pieces=[(f"Sentence number {i} of the answer.", 2000) for i in range(8)]))
        ask(sim)
        sim.detector(2500)                                     # pause 1, resumes 3300
        sim.detector(6500)                                     # pause 2, resumes 7300
        sim.detector(10500)                                    # limit reached: duck only
        sim.say(3, 10540, 11800, "stop and show me the errors", interims=[(11500, "stop and")])
        sim.run()
        pauses = [t for t, _ in sim.ops(F.Pause)]
        self.assertEqual(pauses, [2500.0, 6500.0, 11500.0])
        self.assertIn(10500.0, [t for t, _ in sim.ops(F.Duck)])
        self.assertEqual(sim.questions()[-1], "stop and show me the errors")

    def test_o15_speakers_short_echo_interim_is_echo(self):
        sim = Sim({"speakers": True}, plans=Plan(pieces=[("Running the tests now.", 2500)]))
        ask(sim)
        sim.say(2, 2800, 3400, "the tests", interims=[(3300, "the tests")], truth="")
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(sim.ops(F.Pause), [])

    def test_o16_yes_over_the_last_word_of_a_question_is_answered(self):
        sim = Sim(plans={Q1: Plan(pieces=[("I found two failures.", 1200), ("Shall I run the tests?", 1500)]),
                         "": Plan()})
        ask(sim)                                               # piece 2 plays about 3420-4920
        sim.detector(4760)
        sim.say(2, 4800, 5100, "Yes.")
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "Yes."])
        self.assertEqual(sim.ends()["r1"], "interrupt")

    def test_o16_okay_over_a_statement_end_is_a_backchannel(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 4600, 4900, "okay", truth="")
        sim.run()
        self.assertOneAnswer(sim)

    def test_o17_rhetorical_question_that_plays_on_is_no_tail(self):
        sim = Sim(plans=Plan(pieces=[("Why?", 600), ("Because the cache was stale.", 2000)]))
        ask(sim)                                               # "Why?" about 2220-2820
        sim.say(2, 2700, 3200, "yeah", truth="")
        sim.run()
        self.assertOneAnswer(sim)

    def test_o18_question_unchanged_by_a_segment_final_no_restart(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 1700, 2000, "Thank you.", final_ms=1500, truth="")   # in r1's question group
        sim.run()
        self.assertOneAnswer(sim)

    def test_o19_keepalive_not_while_paused_and_timer_restarts_on_play(self):
        sim = Sim(plans=Plan(pieces=[("Found it.", 1000)], first_ms=20000))
        ask(sim)                                               # due at 6400
        sim.say(2, 6000, 9000, "", interims=[(7000, ""), (8000, "")])  # tentative 7500, '' at 9900
        sim.run()
        kas = [t for t, _ in sim.ops(F.Keepalive)]
        self.assertEqual(kas, [7500.0 + 4500, 7500.0 + 4500 + 9000])

    def test_o19_heard_keepalive_makes_new_words_interrupt(self):
        sim = Sim({"keepalive_after_ms": 500}, plans={Q1: Plan(first_ms=6000), "": Plan()})
        ask(sim)                                               # keepalive 2400, plays 2620-
        sim.say(2, 2800, 3300, "in the repo")                  # within join_gap_ms of speech end
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "in the repo"])
        self.assertEqual(sim.ends()["r1"], "interrupt")

    def test_o20_announcement_interrupted_by_words_is_carried_with_tries(self):
        sim = Sim()
        sim.at(0, F.TaskResult("t1", "Task 1 finished."))
        sim.say(1, 1200, 1800, "hi what is up", interims=[(1700, "hi what")])
        sim.run()
        a1, r2 = sim.starts()
        self.assertEqual((a1.kind, r2.kind), ("announce", "answer"))
        self.assertEqual(r2.carries, [{"id": "t1", "prompt": "Task 1 finished.", "tries": 1}])
        self.assertEqual(sim.ends()["a1"] if "a1" in sim.ends() else sim.ends()[a1.rid], "interrupt")

    def test_o21_out_of_order_finals_noise_and_words_over_heard(self):
        sim = Sim()
        ask(sim)
        sim.detector(2460)
        sim.say(2, 2500, 2700, "", final_ms=2300)              # '' at 5600
        sim.say(3, 2900, 3500, "stop and show the errors", final_ms=100)   # final 4200
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "stop and show the errors"])
        self.assertEqual(sim.ends()["r1"], "interrupt")
        self.assertEqual(sim.ends()["r2"], "drained")

    def test_o21_interrupter_and_newer_pending_segment_join_in_segment_order(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2600, 3000, "stop show the errors", interims=[(2900, "stop")])    # final 3900
        sim.say(3, 3300, 3500, "and the config", final_ms=900)                       # final 5000
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "stop show the errors", "stop show the errors and the config"])

    def test_o22_reply_drains_before_an_over_heard_final_with_words(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 5000, 5600, "what about the config", final_ms=1500)   # final 7700
        sim.run()
        r1, r2 = sim.starts()
        self.assertEqual(sim.ends()["r1"], "drained")
        self.assertEqual(r2.context.question, Q1)


# -- one test per rule ----------------------------------------------------------------------------

class TestRules(FloorCase):
    def test_rule1_sound_before_heard_content_holds(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 2200, "", turn_end_ms=2500, final_ms=200)       # '' at 2700
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Pause)], [2000.0])
        self.assertEqual(sim.emits("barge_in_detected"), [])
        self.assertEqual(sim.emits("playback_paused"), [])
        self.assertOneAnswer(sim)

    def test_rule2_detector_barge_in_pauses_in_the_same_event(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600, "barge_in", "open")
        sim.run(until=2600)
        self.assertEqual(sim.ops(F.Pause)[0][0], 2600.0)
        self.assertEqual(len(sim.emits("barge_in_detected")), 1)
        self.assertEqual(sim.f.reply.cause, "barge_in")

    def test_rule2_interim_words_over_content_pause_with_x_bargein(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2600, 3400, "wait use staging", interims=[(3200, "wait use")])
        sim.run()
        self.assertEqual(sim.ops(F.Pause)[0][0], 3200.0)
        self.assertEqual([f.fields["reason"] for _, f in sim.emits("x_bargein")], ["interim"])

    def test_rule2_detector_duck_and_resume_set_the_gain(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600, "duck")
        sim.detector(2900, "resume")
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Duck)], [2600.0])
        self.assertEqual([t for t, _ in sim.ops(F.Unduck)], [2900.0])

    def test_rule3_stt_dropout_uses_the_last_interim(self):
        sim = Sim()
        sim.say(1, 0, 1000, "", interims=[(700, "what is in the")], truth="what is in the")
        sim.run()
        self.assertEqual(sim.questions(), ["what is in the"])

    def test_rule4_blip_in_the_set_waits_for_short_bargein(self):
        sim = Sim()
        ask(sim)
        sim.say(2, 2000, 2100, "", turn_end_ms=2300, final_ms=3000)       # quiet at 2300
        sim.detector(2150)                                                # a blip joins the set
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Play)][0], 3500.0)

    def test_rule5_reply_starts_on_known_words_paused_on_the_rest(self):
        sim = Sim()
        ask(sim, final_ms=300)                                 # final 1900
        sim.say(2, 1700, 1800, "", final_ms=600)               # pending at 1900
        sim.run(until=1900)
        acts = [a for t, a in sim.acts if t == 1900.0]
        kinds = [type(a).__name__ for a in acts if isinstance(a, (F.StartReply, F.Pause))]
        self.assertEqual(kinds, ["StartReply", "Pause"])

    def test_rule6_a_stopped_reply_never_plays_again(self):
        f = F.Floor()
        f.on(F.SpeechStart(1, "u1", t_ms=0))
        f.on(F.TurnEnd(1, 1000, t_ms=1600))
        acts = f.on(F.Final("u1", Q1, "spec", t_ms=1900))
        rid = [a for a in acts if isinstance(a, F.StartReply)][0].rid
        f.on(F.TtsPiece(rid, "p0", "content", "The logs show no errors.", 1500, t_ms=2200))
        f.on(F.Played(rid, "p0", 200, t_ms=2400))
        f.on(F.SpeechStart(2, "u2", t_ms=2500))
        f.on(F.TurnEnd(2, 3000, t_ms=3600))
        acts = f.on(F.Final("u2", "no stop", "spec", t_ms=3900))
        self.assertTrue(any(isinstance(a, F.StopReply) and a.rid == rid for a in acts))
        for ev in [F.TtsPiece(rid, "p1", "content", "x", 100, t_ms=4000), F.Played(rid, "p0", 400, t_ms=4000),
                   F.Drained(rid, t_ms=4000), F.BrainDone(rid, t_ms=4000)]:
            acts = f.on(ev)
            self.assertFalse(any(isinstance(a, F.Play) and a.rid == rid for a in acts))
        self.assertNotEqual(f.reply.rid, rid)

    def test_rule7_announcement_only_after_announce_quiet_ms_of_free_floor(self):
        sim = Sim()
        sim.say(1, 0, 300, "", final_ms=300)                   # '' at 1200
        sim.at(100, F.TaskResult("t1", "Task 1 finished."))
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.StartReply)], [1200.0 + 800])

    def test_rule8_keepalive_after_4500_then_every_9000(self):
        sim = Sim(plans=Plan(pieces=[("Found it.", 1000)], first_ms=26000))
        ask(sim)
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Keepalive)], [6400.0, 15400.0, 24400.0])

    def test_rule9_brain_error_before_audio_has_no_context(self):
        sim = Sim(plans={Q1: Plan(error_ms=100), "": Plan()})
        ask(sim)
        ask(sim, g=2, start=4000, text="Try again please.")
        sim.run()
        self.assertEqual(sim.ends()["r1"], "error")
        self.assertIsNone(sim.starts()[1].context)

    def test_rule10_detector_resume_does_not_end_a_pause(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600)
        sim.detector(2900, "resume")
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Play)], [3400.0])


# -- review findings and fuzzer finds ---------------------------------------------------------------

class TestReviewFindings(FloorCase):
    def test_brain_done_after_tts_done_and_drained_still_ends(self):
        f = F.Floor()
        f.on(F.SpeechStart(1, "u1", t_ms=0))
        f.on(F.TurnEnd(1, 1000, t_ms=1600))
        rid = [a for a in f.on(F.Final("u1", Q1, "spec", t_ms=1900)) if isinstance(a, F.StartReply)][0].rid
        f.on(F.TtsPiece(rid, "p0", "content", "Done.", 500, t_ms=2000))
        f.on(F.Played(rid, "p0", 500, t_ms=2500))
        f.on(F.TtsDone(rid, t_ms=2500))
        f.on(F.Drained(rid, t_ms=2510))
        f.on(F.BrainDone(rid, t_ms=2600))
        self.assertIsNone(f.reply)

    def test_reply_with_no_pieces_ends_without_drained(self):
        sim = Sim(plans={Q1: Plan(pieces=[]), "": Plan()})
        ask(sim)
        sim.at(2500, F.TaskResult("t1", "Task 1 finished."))
        sim.run()
        self.assertEqual([a.kind for a in sim.starts()], ["answer", "announce"])

    def test_interim_pause_does_not_make_a_segment_over_heard(self):
        plan = Plan(pieces=[("I am looking.", 800), ("Found it.", 800)], gap_ms=8000, done_ms=9000)
        sim = Sim(plans={Q1: plan, "": Plan()})
        ask(sim)                                               # content 2220-3020, then silence
        sim.say(2, 5000, 5400, "yeah", interims=[(5300, "yeah")])
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "yeah"])        # words in silence, not a backchannel

    def test_interrupted_label_keeps_its_own_turn_end(self):
        sim = Sim({"keepalive_after_ms": 500}, plans={Q1: Plan(first_ms=6000), "": Plan()})
        ask(sim)                                               # turn_end 1600
        sim.say(2, 2800, 3300, "in the repo")                  # turn_end 3900, interrupts
        sim.run()
        ted = {e.label: e.fields["t_ms"] for _, e in sim.emits("turn_end_detected")}
        self.assertEqual(ted, {"1": 1600.0, "2": 3900.0})

    def test_interim_words_in_a_hold_tentative_are_no_barge_in(self):
        sim = Sim(plans=Plan(first_ms=4000))                   # nothing to play until 5900
        ask(sim)
        sim.say(2, 2000, 4200, "and the config", interims=[(2800, ""), (3900, "and the")])
        sim.run()
        self.assertEqual(sim.emits("barge_in_detected"), [])
        self.assertEqual(sim.emits("playback_paused"), [])

    def test_join_restart_carries_announcements_without_a_try(self):
        sim = Sim(plans=Plan(first_ms=2000))
        sim.at(0, F.TaskResult("t1", "Task 1 finished."))
        sim.say(1, 200, 1000, "What are you doing?")           # before the announcement starts
        sim.say(2, 2600, 3200, "in the repo")
        sim.run()
        carries = [[c["tries"] for c in a.carries] for a in sim.starts()]
        self.assertEqual(carries, [[0], [0]])

    def test_detector_pause_counts_over_heard_without_content_in_window(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600)                                     # pauses; nothing plays after 2600
        sim.say(2, 3300, 3600, "yeah", truth="")               # absorbs the blip 700 ms later
        sim.run()
        self.assertOneAnswer(sim)

    def test_new_speech_lifts_the_resume_guard(self):
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(2500)                                     # no-words pause, resumes 3300
        sim.say(2, 3500, 3900, "")
        sim.detector(3700)                                     # speech since the resume: pause
        sim.run()
        self.assertIn(3700.0, [t for t, _ in sim.ops(F.Pause)])

    def test_play_after_a_hold_restarts_the_keepalive_timer(self):
        sim = Sim(plans=Plan(pieces=[("Found it.", 1000)], first_ms=12000))
        ask(sim)
        sim.say(2, 3000, 3200, "", turn_end_ms=3500, final_ms=500)   # hold, '' and play at 4000
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Keepalive)][0], 4000.0 + 4500)

    def test_blip_in_the_set_waits_even_past_a_short_cap(self):
        sim = Sim({"pause_max_ms": 500})
        ask(sim)
        sim.say(2, 2000, 2050, "", turn_end_ms=2100, final_ms=3000)   # quiet at 2100
        sim.detector(2150)                                     # blip in the set until 2950
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Play)][0], 2950.0)

    def test_rewind_counts_from_the_last_stop_after_a_tentative_play(self):
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(2600)                                     # 380 ms into piece 1
        sim.say(2, 2640, 2800, "", interims=[(3200, "")], turn_end_ms=3400, final_ms=2600)
        sim.detector(4400)                                     # stops the tentative play
        sim.run()
        self.assertEqual([a.rewind_ms for _, a in sim.ops(F.Play)], [380.0, 0.0])

    def test_keepalive_playing_at_a_hold_is_dropped(self):
        sim = Sim({"keepalive_after_ms": 500}, plans={Q1: Plan(first_ms=6000), "": Plan()})
        ask(sim)                                               # keepalive 2400, plays from 2620
        sim.say(2, 2900, 3000, "", turn_end_ms=3300, final_ms=200)   # hold 2900-3500
        sim.run()
        self.assertEqual([t for t, e in sim.events if isinstance(e, F.Played) and e.piece == "k1" and t > 2900], [])

    def test_announcement_that_never_plays_is_dropped_after_three_tries(self):
        sim = Sim(plans={"Task": Plan(error_ms=100), "": Plan()})
        sim.at(0, F.TaskResult("t1", "Task 1 finished."))
        ask(sim, start=8000)
        sim.run()
        starts = sim.starts()
        self.assertEqual([a.kind for a in starts], ["announce"] * 3 + ["answer"])
        self.assertEqual(starts[-1].carries, [])
        self.assertEqual([e.fields["prompt"] for _, e in sim.emits("x_announce_dropped")], ["Task 1 finished."])

    def test_hold_reaching_content_is_never_a_barge_in(self):
        # critic probe_hold: the held segment's tentative play reached content, then "got" / "got it"
        sim = Sim(plans=LONG)
        ask(sim)
        sim.say(2, 1500, 3500, "got it", interims=[(2000, ""), (3300, "got")], truth="")
        sim.detector(5500)                                     # a real barge-in 1.1 s after the play
        sim.run()
        self.assertEqual([e.fields["reason"] for _, e in sim.emits("barge_in_detected")], ["detector_open"])
        self.assertIn(5500.0, [t for t, _ in sim.ops(F.Pause)])

    def test_barge_in_on_a_new_blip_counts_while_a_hold_is_open(self):
        # fuzz seed 279: segments joining during the tentative play kept the hold "open" forever
        sim = Sim(plans=LONG)
        ask(sim)
        sim.say(2, 2000, 2100, "", turn_end_ms=2400, final_ms=4000)      # held; tentative at 3500
        sim.say(3, 3700, 3800, "", turn_end_ms=4000, final_ms=3000)      # joins the set over content
        sim.detector(4300)                                                # a new blip: a barge-in
        sim.run()
        self.assertEqual([t for t, _ in sim.emits("barge_in_detected")], [4300.0])
        self.assertEqual(len(sim.emits("playback_resumed")), 1)

    def test_content_after_a_keepalive_restarts_the_short_timer(self):
        # fuzz seed 51: the timer stayed at keepalive + 9 s although content played after it
        plan = Plan(pieces=[("Found the logs.", 1000), ("And the errors.", 1000)], first_ms=5000, gap_ms=9000,
                    done_ms=30000)
        sim = Sim(plans=plan)
        ask(sim)                                               # keepalive 6400 plays 6620-7420
        sim.run()                                              # then "Found the logs." to 8380
        kas = [t for t, _ in sim.ops(F.Keepalive)]
        self.assertEqual(kas[:2], [6400.0, 8380.0 + 4500])     # not 6400 + 9000

    def test_backchannel_just_after_the_reply_ends_is_over_heard(self):
        sim = Sim()
        ask(sim)                                               # drains about 5220
        sim.say(2, 5600, 5900, "okay", truth="")               # within echo_lead_ms of its end
        sim.run()
        self.assertOneAnswer(sim)

    def test_reply_end_unducks(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600, "duck")                             # ducked, never resumed
        sim.say(2, 2600, 3200, "stop show the errors", interims=[(3100, "stop show")])
        sim.run()
        self.assertEqual([a.rid for _, a in sim.ops(F.Unduck)], ["r1"])   # the ducked reply, at its stop

    def test_paused_gap_is_not_heard_content(self):
        # fuzz seed 1216: a piece's played span ran across the pause once the tentative play resumed it
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(2600)
        sim.say(2, 2640, 2700, "", turn_end_ms=3000, final_ms=3000)      # holds until 6000
        sim.say(3, 3300, 3500, "okay", interims=[(4000, "")], turn_end_ms=5300, final_ms=300)
        # tentative replay at 4800 (G3's cap); G3 said "okay" in the silence before it
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "okay"])         # words, not a backchannel over content

    def test_a_reply_never_ends_while_paused(self):
        # adapter critic e1: drained between sentences, a blip pauses, the brain finishes with no more
        sim = Sim(plans=Plan(pieces=[("The logs show no errors.", 1500)], done_ms=2500))
        ask(sim)                                               # plays 2220-3720; brain_done at 4400
        sim.detector(4000)                                     # paused 4000-4800
        sim.run()
        end = [t for t, f in sim.floor_log("end")]
        self.assertEqual(end, [4800.0])                        # after the blip, not at brain_done
        self.assertEqual(len(sim.emits("playback_resumed")), 1)

    def test_hum_before_a_hallucination_is_no_words(self):
        self.assertEqual(F.make_meaningful()("Hmm, thank you.", []), "")

    def test_announcement_that_fails_after_its_first_sentence_is_dropped(self):
        # adapter fuzz seed 371: content played each try, then an error: retried forever
        plan = Plan(pieces=[("Should I keep going?", 600), ("The build is green.", 900)], gap_ms=1500,
                    error_ms=1500)
        sim = Sim(plans={"Task": plan, "": Plan()})
        sim.at(0, F.TaskResult("t1", "Task 1 finished."))
        sim.run()
        self.assertEqual([a.kind for a in sim.starts()], ["announce"] * 3)
        self.assertEqual(len(sim.emits("x_announce_dropped")), 1)

    def test_played_while_paused_is_ignored(self):
        f = F.Floor()
        f.on(F.SpeechStart(1, "u1", t_ms=0))
        f.on(F.TurnEnd(1, 1000, t_ms=1600))
        rid = [a for a in f.on(F.Final("u1", Q1, "spec", t_ms=1900)) if isinstance(a, F.StartReply)][0].rid
        f.on(F.TtsPiece(rid, "p0", "content", "The logs show no errors.", 1500, t_ms=2000))
        f.on(F.Played(rid, "p0", 300, t_ms=2300))
        f.on(F.Detector("barge_in", "open", t_ms=2310))
        f.on(F.Played(rid, "p0", 600, t_ms=2600))               # a late report after the pause
        self.assertEqual(f.reply.offset, ("p0", 300.0))

    def test_backchannel_final_after_partial_interim_is_no_dropout(self):
        # fuzz seed 1076: interim "got" had words, final "got it" over content is a backchannel
        sim = Sim()
        ask(sim)
        sim.say(2, 2600, 3000, "got it", interims=[(2900, "got")], truth="")
        sim.run()
        self.assertOneAnswer(sim)


# -- echo (A/B 2026-10-30: ab-new-echo, ab-new-echo-spk) ----------------------------------------

class TestEcho(FloorCase):
    """The reply's own echo opens segments; the STT writes short, misheard fragments of what plays.
    From the runs: "Shreds are cheaper." over "Threads are cheaper", "Thread reuse." over "A thread
    reuses", "A full reload." over "a full rewrite"."""

    PLAN = Plan(pieces=[("Threads are cheaper because they share memory.", 3000),
                        ("A thread reuses the process's address space.", 3000)])

    def _echo(self, cfg, interim, final):
        sim = Sim(cfg, plans=self.PLAN)
        ask(sim)                                               # piece 1 plays about 2220-5220
        sim.say(2, 2600, 3400, final, interims=[(3300, interim)], truth="")
        return sim.run()

    def test_misheard_echo_fragment_on_speakers(self):
        sim = self._echo({"speakers": True}, "Shreds are", "Shreds are cheaper.")
        self.assertOneAnswer(sim)

    def test_misheard_echo_fragment_with_echo_on_headphones(self):
        sim = self._echo({}, "Shreds are", "Shreds are cheaper.")
        self.assertOneAnswer(sim)

    def test_echo_score_makes_any_text_echo(self):
        # "A full reload." over "a full rewrite": no text rule catches it; the audio does
        sim = Sim({"speakers": True}, plans=self.PLAN)
        ask(sim)
        sim.say(2, 2600, 3400, "A full reload.", interims=[(3300, "A full")], truth="", echo=0.9)
        sim.run()
        self.assertOneAnswer(sim)
        self.assertEqual(sim.ops(F.Pause), [])

    def test_real_words_over_the_echo_still_interrupt(self):
        sim = Sim({"speakers": True}, plans=self.PLAN)
        ask(sim)
        sim.say(2, 2600, 3400, "stop show me the errors", interims=[(3300, "stop show")], echo=0.1)
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "stop show me the errors"])

    def test_echo_segment_leaves_the_pause_set_at_once(self):
        sim = Sim(plans=self.PLAN)
        ask(sim)
        sim.detector(2600)                                     # the detector heard the echo
        sim.say(2, 2640, 4000, "A full reload.", interims=[(3600, "A full")], truth="", echo=0.9,
                final_ms=1500)
        sim.run()
        self.assertEqual([t for t, _ in sim.ops(F.Play)], [3600.0])   # not at the final (6100)
        self.assertOneAnswer(sim)

    def test_users_words_repeating_the_reply_with_a_clear_score_are_words(self):
        sim = Sim({"speakers": True}, plans=Plan(pieces=[("Now the tests pass.", 2000)]))
        ask(sim)                                               # plays about 2220-4220
        sim.say(2, 3000, 3600, "the tests", interims=[(3500, "the tests")], echo=0.1)
        sim.run()
        self.assertEqual(sim.questions(), [Q1, "the tests"])

    def test_gray_zone_score_keeps_the_text_rule(self):
        sim = Sim({"speakers": True}, plans=self.PLAN)
        ask(sim)
        sim.say(2, 2600, 3400, "Shreds are cheaper.", interims=[(3300, "Shreds are")], truth="", echo=0.4)
        sim.run()
        self.assertOneAnswer(sim)

    def test_short_echo_fragment(self):
        sim = Sim({"speakers": True}, plans=self.PLAN)
        ask(sim)
        sim.say(2, 5400, 6000, "Thread reuse.", interims=[(5900, "Thread reuse")], truth="")
        sim.run()
        self.assertOneAnswer(sim)


# -- rewind, words, labels -------------------------------------------------------------------------

class TestDetails(FloorCase):
    def _pause_for(self, ms):
        sim = Sim(plans=LONG)
        ask(sim)
        sim.detector(3000)                                     # 780 ms into piece 1
        sim.say(2, 3040, 3100, "", turn_end_ms=3000 + ms - 300, final_ms=300)
        sim.run()
        return [a.rewind_ms for _, a in sim.ops(F.Play)]

    def test_rewind_scales_with_the_pause(self):
        self.assertEqual(self._pause_for(900), [0.0])
        self.assertEqual(self._pause_for(2000), [500.0])
        self.assertEqual(self._pause_for(4000)[-1], 780.0)    # to the piece start, under 2 s

    def test_meaningful_echo_runs_whole_pieces_and_hallucinations(self):
        m = F.make_meaningful()
        played = ["The logs show no errors.", "Sure."]
        self.assertEqual(m("the logs show no errors but why", played), "but why")
        self.assertEqual(m("Sure.", played), "")
        self.assertEqual(m("the logs please", played), "the logs please")
        self.assertEqual(m("Thank you.", []), "")
        self.assertEqual(m("Thank you.", [], earlier_words=True), "Thank you.")
        self.assertEqual(F.make_meaningful(speakers=True)("errors logs", played), "")
        self.assertEqual(m("errors logs", played), "errors logs")

    def test_turn_end_detected_has_the_newest_word_turn_end(self):
        sim = Sim(plans=Plan(first_ms=2000))
        ask(sim)                                               # turn_end 1600
        sim.say(2, 2300, 3000, "in the repo")                  # turn_end 3600, joins
        sim.run()
        ted = sim.emits("turn_end_detected")
        self.assertEqual([(e.label, e.fields["t_ms"]) for _, e in ted], [("1", 3600.0)])
        self.assertEqual(len(sim.emits("x_turn_end")), 2)
        self.assertEqual([e.fields["text"] for _, e in sim.emits("final_transcript")],
                         [Q1, Q1 + " in the repo"])

    def test_x_floor_reason_for_every_gate_action(self):
        sim = Sim()
        ask(sim)
        sim.detector(2600)
        sim.say(2, 2640, 2800, "")
        sim.run()
        acts = {f["act"] for _, f in sim.floor_log()}
        self.assertLessEqual({"pause", "play", "start", "end"}, acts)
        self.assertTrue(all(f.get("reason") for _, f in sim.floor_log()))


if __name__ == "__main__":
    unittest.main()
