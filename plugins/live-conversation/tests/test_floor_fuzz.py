"""Floor fuzzer, first layer (FLOOR-SPEC.md "Fuzzer"): live/floor.py on a simulated clock, with the
real BargeIn on rendered mic audio. Run: .venv/bin/python -m unittest tests.test_floor_fuzz

    FLOOR_FUZZ_SEEDS=3000     how many seeds (default 300)
    FLOOR_FUZZ_SEED=1234      run one seed, print its scenario and the floor log

Each scenario is a list of sounds with ground truth (words with text, backchannel, noise including
sub-60 ms blips, hallucination, echo on speakers), rendered as mic audio with the synthetic
generators of test_bargein.py. Every 20 ms frame the real BargeIn hears that audio plus the
simulated sink's output mixed back in, with the controller's `floor` and `reset_detector`.
A rule-based endpointer and an STT model make the segment events from the sounds; a random brain
answers each start_reply. floor_driver.Checker checks the invariants after every event and at
quiescence. A failing seed is shrunk by removing and shortening sounds, and printed.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from dataclasses import dataclass, field, replace
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_bargein as gen  # noqa: E402
from floor_driver import TICK, InvariantError, Sim  # noqa: E402

from live import floor as F  # noqa: E402
from live.bargein import BargeIn  # noqa: E402
from live.events import FRAME, SR  # noqa: E402
from live.turntaking import fold_hums, is_hesitation, normalize  # noqa: E402

SEEDS = int(os.environ.get("FLOOR_FUZZ_SEEDS", "300"))
ONE_SEED = os.environ.get("FLOOR_FUZZ_SEED")

# User words and reply text share no word, so echo removal never eats what the user said.
VERBS = ["show me", "check", "rerun", "open", "explain", "compare", "list", "summarize"]
OBJECTS = ["staging logs", "database errors", "lint warnings", "migration plan", "config file",
           "test failures", "payment service", "error budget", "queue depth", "login flow"]
TAILS = ["", "please", "for today", "again", "quickly"]
BACKCHANNELS = ["yeah", "okay", "right", "got it", "mm-hmm", "uh huh"]
HALLUCINATIONS = ["Thank you.", "You", "Thanks for watching."]
REPLY_TEXT = ["The build is green.", "Deploy finished in four minutes.", "The cache was cold.",
              "Branch merged cleanly.", "Server restarted at noon.", "Nothing else changed.",
              "Two jobs were retried.", "Latency went back down."]


@dataclass
class Sound:
    id: int
    kind: str            # words | backchannel | noise | blip | hallucination | echo
    start: float
    dur: float
    text: str = ""


@dataclass
class Scenario:
    seed: int
    sounds: list
    tasks: list          # [(t_ms, id)]
    speakers: bool
    silence_ms: float    # endpointer: turn_end this long after the sound ends
    echo_db: float
    dropout: set = field(default_factory=set)   # sound ids whose segment's final comes back empty

    def describe(self) -> str:
        lines = [f"seed {self.seed}: speakers={self.speakers} silence_ms={self.silence_ms} "
                 f"echo_db={self.echo_db:.0f} dropout={sorted(self.dropout)}"]
        for s in sorted(self.sounds, key=lambda s: s.start):
            lines.append(f"  sound {s.id:2d} {s.kind:13s} {s.start:7.0f} +{s.dur:5.0f} {s.text!r}")
        for t, tid in self.tasks:
            lines.append(f"  task {tid} at {t:.0f}")
        return "\n".join(lines)


def make_scenario(seed: int) -> Scenario:
    rng = np.random.default_rng(seed)
    speakers = bool(rng.random() < 0.25)
    sounds: list = []
    used: set = set()

    def phrase() -> str:
        while True:
            p = f"{rng.choice(VERBS)} {rng.choice(OBJECTS)} {rng.choice(TAILS)}".strip()
            if p not in used:
                used.add(p)
                return p + f" ticket {100 + len(used)}"

    def add(kind, start, dur, text=""):
        sounds.append(Sound(len(sounds), kind, float(start), float(dur), text))

    def words(start):
        text = phrase()
        toks = text.split()
        if rng.random() < 0.3:              # two parts with a pause: early turn_ends on short silence
            k = int(rng.integers(1, len(toks)))
            d1 = 250.0 * k
            add("words", start, d1, " ".join(toks[:k]))
            add("words", start + d1 + rng.uniform(350, 700), 250.0 * (len(toks) - k), " ".join(toks[k:]))
        else:
            add("words", start, 250.0 * len(toks), text)

    words(rng.uniform(100, 500))
    for _ in range(int(rng.integers(0, 4))):
        words(rng.uniform(1500, 22000))
    for _ in range(int(rng.integers(2, 9))):
        t = rng.uniform(500, 24000)
        kind = rng.choice(["noise", "noise", "blip", "blip", "backchannel", "hallucination"]
                          + (["echo", "echo"] if speakers else []))
        if kind == "blip":
            add("blip", t, rng.uniform(20, 59))
        elif kind == "noise":
            add("noise", t, rng.uniform(60, 1500))
        elif kind == "backchannel":
            add("backchannel", t, rng.uniform(250, 600), str(rng.choice(BACKCHANNELS)))
        elif kind == "hallucination":
            add("hallucination", t, rng.uniform(300, 1500))
        else:
            add("echo", t, rng.uniform(400, 2000))
    tasks = [(float(rng.uniform(0, 20000)), f"t{i + 1}") for i in range(int(rng.choice([0, 0, 1, 1, 2])))]
    dropout = {s.id for s in sounds if s.kind == "words" and rng.random() < 0.06}
    return Scenario(seed, sounds, tasks, speakers, float(rng.choice([600, 600, 600, 300, 1500])),
                    float(rng.uniform(-45, -35)) + (20.0 if speakers else 0.0), dropout)


# -- audio ---------------------------------------------------------------------------------------

@np.errstate(all="ignore")
def render(sc: Scenario) -> np.ndarray:
    end = max([s.start + s.dur for s in sc.sounds] + [0.0]) + 120000
    mic = np.random.default_rng((sc.seed, 999)).normal(0, 32768 * 10 ** (-65 / 20), int(SR * end / 1000))
    for s in sc.sounds:
        gen.RNG = rng = np.random.default_rng((sc.seed, s.id))
        if s.kind == "words":
            parts, left = [], s.dur
            while left >= 5:
                v = min(left, rng.uniform(120, 250))
                parts.append(gen.vowel(v, f0=rng.uniform(110, 220), db=rng.uniform(-26, -14)))
                left -= v
                if left >= 5:
                    f_ = min(left, rng.uniform(40, 80))
                    parts.append(gen.fricative(f_, db=rng.uniform(-30, -22)))
                    left -= f_
            x = np.concatenate(parts)
        elif s.kind == "backchannel":
            x = gen.hum(s.dur, f0=rng.uniform(110, 220), db=rng.uniform(-24, -16))
        elif s.kind == "blip":
            x = gen.vowel(s.dur, f0=rng.uniform(110, 220), db=rng.uniform(-30, -18))
        elif s.kind == "noise":
            pick = rng.integers(0, 3)
            x = (gen.noise(s.dur, db=rng.uniform(-36, -26)) if pick == 0 else
                 gen.fricative(s.dur, db=rng.uniform(-34, -24)) if pick == 1 else
                 gen.rumble(s.dur, db=rng.uniform(-36, -26)))
        elif s.kind == "hallucination":
            x = gen.noise(s.dur, db=rng.uniform(-48, -38))
        else:
            continue                        # echo: the sink's own output is mixed in at run time
        i = int(SR * s.start / 1000)
        n = min(len(x), len(mic) - i)
        mic[i:i + n] += x[:n]
    return np.clip(mic, -32768, 32767).astype(np.int16)


_REPLY_AUDIO = gen.vowel(2000, f0=200.0, db=-20.0).astype(np.float64)


# -- endpointer and STT model ----------------------------------------------------------------------

@dataclass
class Group:
    g: int
    sounds: list
    speech_start: float
    end: float
    turn_end: float

    @property
    def utt(self) -> str:
        return f"u{self.g}"


def segment(sc: Scenario) -> list:
    """Rule-based endpointer: a segment starts after 60 ms of one sound and ends silence_ms after
    the last sound that started before then. Sounds under 60 ms only extend a segment."""
    groups: list = []
    cur: Optional[Group] = None
    for s in sorted(sc.sounds, key=lambda s: s.start):
        if cur is not None and s.start < cur.end + sc.silence_ms:
            cur.sounds.append(s)
            cur.end = max(cur.end, s.start + s.dur)
            cur.turn_end = cur.end + sc.silence_ms
            continue
        if s.dur < 60:
            continue
        cur = Group(len(groups) + 1, [s], s.start + 60, s.start + s.dur, s.start + s.dur + sc.silence_ms)
        groups.append(cur)
    return groups


@dataclass
class Lazy:
    """A scheduled STT result whose text depends on what has played (echo)."""
    make: object
    t_ms: float = 0.0


class Truth:
    """utt -> words ("" = none). A backchannel-only segment ("yeah") is no words when reply content
    played from 0.5 s before it to its speech end (the sim's own record of what played), words when
    nothing played, and optional when a detector barge-in near it may have paused the reply (the
    spec counts that pause as heard content too)."""

    def __init__(self, sim) -> None:
        self.sim = sim
        self.words: dict = {}
        self.backchannel: dict = {}   # utt -> Group, for backchannel-only segments
        self.word_tokens: dict = {}   # utt -> tokens of its words sounds (not backchannels)

    def _over_heard(self, grp) -> Optional[bool]:
        start, end = grp.speech_start - 500, grp.end
        played = any(isinstance(e, F.Played) and e.piece.startswith("p") and start <= t <= end + TICK
                     for t, e in self.sim.events)
        if played:
            return True
        near = any(isinstance(e, F.Detector) and e.decision == "barge_in" and start - 800 <= t <= end
                   for t, e in self.sim.events)
        return None if near else False

    def get(self, utt, default=None):
        if utt in self.backchannel:
            over = self._over_heard(self.backchannel[utt])
            return "" if over else self.words[utt]
        return self.words.get(utt, default)

    def __getitem__(self, utt):
        return self.get(utt)

    def items(self):
        return [(u, self.get(u)) for u in self.words if not self.optional(u)]

    def optional(self, utt) -> bool:
        return utt in self.backchannel and self._over_heard(self.backchannel[utt]) is None

    def interim_has_words(self, utt, text) -> bool:
        return bool(set(said(text).split()) & self.word_tokens.get(utt, set()))


def said(text: str) -> str:
    """Ground-truth words of a text: hums and hesitations are no words (spec "Words" 3)."""
    toks = fold_hums(normalize(text)).split()
    return " ".join(w for w in toks if w not in F.HUMS and not is_hesitation(w))


class FuzzSim(Sim):
    def __init__(self, sc: Scenario) -> None:
        super().__init__({"speakers": sc.speakers})
        self.sc = sc
        self.truth = Truth(self)
        self.bi = BargeIn()
        self.floor_on = False
        self.mic = render(sc)
        self.echo_gain = 10 ** (sc.echo_db / 20)
        self.busy_until = 0.0
        self.n_replies = 0
        self.sounds_end = max([s.start + s.dur for s in sc.sounds] + [0.0])
        for t, tid in sc.tasks:
            self.at(t, F.TaskResult(tid, f"Task {tid} finished."))
        for grp in segment(sc):
            self._script(grp)

    def _script(self, grp: Group) -> None:
        rng = np.random.default_rng((self.sc.seed, 5, grp.sounds[0].id))
        utt = grp.utt
        kinds = {s.kind for s in grp.sounds}
        spoken = [s for s in grp.sounds if s.kind in ("words", "backchannel")]
        text = " ".join(s.text for s in spoken)
        speech_ms = sum(s.dur for s in spoken) or 1.0
        self.truth.word_tokens[utt] = {w for s in grp.sounds if s.kind == "words" for w in said(s.text).split()}
        self.at(grp.speech_start, F.SpeechStart(grp.g, utt))
        self.at(grp.turn_end, F.TurnEnd(grp.g, grp.end))
        r = rng.random()
        final_at = grp.turn_end + (rng.uniform(100, 500) if r < 0.6 else rng.uniform(500, 1500) if r < 0.9
                                   else rng.uniform(1500, 3000))
        halluc = str(rng.choice(HALLUCINATIONS))
        last_interim = ""
        k = 1
        while grp.speech_start - 300 + 1000 * k < grp.turn_end:
            issue = grp.speech_start - 300 + 1000 * k
            arrive = issue + rng.uniform(350, 500)
            heard = sum(min(s.dur, max(0.0, issue - s.start)) for s in spoken)
            toks = text.split()
            itext = " ".join(toks[:int(len(toks) * heard / speech_ms)]) if spoken else ""
            if not spoken and "hallucination" in kinds and rng.random() < 0.5:
                itext = halluc
            if "echo" in kinds and not spoken:
                self.at(arrive, Lazy(lambda t, g=grp, i=issue: F.Interim(g.utt, self._echo_text(g, i))))
            else:
                self.at(arrive, F.Interim(utt, itext))
                if arrive < final_at and spoken and itext:
                    last_interim = itext
            k += 1
        dropout = any(s.id in self.sc.dropout for s in grp.sounds) and "words" in kinds
        if dropout:
            ftext = ""
            self.truth.words[utt] = said(last_interim)   # the STT lost the rest; nothing recovers it
        elif spoken:
            ftext = text
            self.truth.words[utt] = said(text)
            if "words" not in kinds:
                self.truth.backchannel[utt] = grp
        else:
            ftext = ""
            if "hallucination" in kinds and rng.random() < 0.7 or rng.random() < 0.1:
                ftext = halluc
            self.truth.words[utt] = ""
        if "echo" in kinds and not spoken:
            self.at(final_at, Lazy(lambda t, g=grp: F.Final(g.utt, self._echo_text(g, t), "spec")))
        else:
            self.at(final_at, F.Final(utt, ftext, "timeout" if dropout else "spec"))

    def _echo_text(self, grp: Group, t: float) -> str:
        """What the STT hears in an echo segment: a played piece, or 2-3 of its words in order."""
        start = grp.sounds[0].start - 500
        spans = [e for _, e in self.events if isinstance(e, F.Played) and _ >= start and _ <= t]
        if not spans:
            return ""
        last = spans[-1]
        r = self.f.reply
        piece = r.pieces.get(last.piece) if r is not None and r.rid == last.rid else None
        text = piece.text if piece else next((s.text for s in self.f.played if s.piece == last.piece
                                              and s.rid == last.rid), "")
        toks = text.split()
        if len(toks) > 3 and grp.g % 2:
            i = grp.g % (len(toks) - 2)
            return " ".join(toks[i:i + 2 + grp.g % 2])
        return text

    # -- the adapter's side ----------------------------------------------------------------------
    def apply(self, a, t: float) -> None:
        if isinstance(a, F.SetFloor):
            self.floor_on = a.assistant
        elif isinstance(a, F.ResetDetector):
            self.bi.reset()
        elif isinstance(a, F.StopReply):
            r = np.random.default_rng((self.sc.seed, 11, self.n_replies)).random()
            self.busy_until = t + (r * 300 if r < 0.8 else r * 5000)   # brain.cancel can take 5 s
        elif isinstance(a, F.Play):
            self.bi.reset()                          # spec: play ends with bargein.reset()
        super().apply(a, t)

    def schedule(self, a, t: float) -> None:
        self.n_replies += 1
        rng = np.random.default_rng((self.sc.seed, 7, self.n_replies))
        t0 = max(t, self.busy_until)
        r = rng.random()
        first = rng.uniform(200, 1500) if r < 0.6 else rng.uniform(1500, 6000) if r < 0.9 else rng.uniform(6000, 20000)
        error = rng.uniform(100, first + 4000) if rng.random() < 0.08 else None
        ti = t0 + first
        last = ti
        for i in range(int(rng.integers(1, 5))):
            if error is not None and ti >= t0 + error:
                break
            text = str(rng.choice(REPLY_TEXT))
            self.at(ti, F.BrainText(a.rid, text))
            self.at(ti + 1, F.TtsPiece(a.rid, f"p{i}", "content", text, float(rng.uniform(800, 3000))))
            last = ti + 1
            ti += rng.uniform(50, 2500)
        if error is not None:
            self.at(t0 + error, F.BrainError(a.rid))
            return
        self.at(last + 1, F.BrainDone(a.rid))
        self.at(last + 2, F.TtsDone(a.rid))

    # -- the frame loop --------------------------------------------------------------------------
    def _next(self, t: float):
        cands = []
        if self.q:
            cands.append((self.q[0][0], 0))
        if self.timers:
            cands.append((min(self.timers.values()), 1))
        cands = [c for c in cands if c[0] <= t]
        return min(cands) if cands else None

    def _drain(self, t: float) -> None:
        import heapq
        while True:
            nxt = self._next(t)
            if nxt is None:
                return
            self.t = max(self.t, nxt[0])
            if nxt[1] == 0:
                ev = heapq.heappop(self.q)[2]
                if isinstance(ev, Lazy):
                    ev = ev.make(self.t)
                    ev.t_ms = self.t
                self.dispatch(ev)
            else:
                name = min(self.timers, key=self.timers.get)
                del self.timers[name]
                self.dispatch(F.Timer(name), self.t)

    def quiet(self) -> bool:
        return (not self.q and not self.timers and not self.sink.q and self.f.reply is None
                and all(s.status == "final" for s in self.f.segs.values()))

    def run(self, until: float = 0.0) -> "FuzzSim":
        cap = self.sounds_end + 100000
        t = 0.0
        while True:
            t += TICK
            self._drain(t)
            self.t = t
            audible = self.sink.audible()
            if audible:
                self.tick()
            i = int(SR * (t - TICK) / 1000)
            frame = self.mic[i:i + FRAME].astype(np.float64)
            if audible:
                j = i % (len(_REPLY_AUDIO) - FRAME)
                frame = frame + _REPLY_AUDIO[j:j + FRAME] * self.echo_gain
            d = self.bi.feed(np.clip(frame, -32768, 32767).astype(np.int16), t, self.floor_on)
            if d:
                self.dispatch(F.Detector(d, self.bi.last.get("reason", "")), t)
            if t > self.sounds_end + 1000 and self.quiet():
                break
            if t > cap:
                raise InvariantError(f"no quiescence by {cap:.0f} ms")
        if self.checker:
            self.checker.quiescent()
        return self


# -- running and shrinking -------------------------------------------------------------------------

def fails(sc: Scenario) -> Optional[str]:
    try:
        FuzzSim(sc).run()
    except Exception as e:                       # noqa: BLE001 - any crash is a finding
        return f"{type(e).__name__}: {e}"
    return None


def shrink(sc: Scenario, why: str) -> tuple:
    """Remove sounds and tasks, then shorten sounds, while the scenario still fails the same way."""
    kind = why.split(" at ")[0]

    def same(c):
        w = fails(c)
        return w is not None and w.split(" at ")[0] == kind

    changed = True
    while changed:
        changed = False
        for s in list(sc.sounds):
            c = replace(sc, sounds=[x for x in sc.sounds if x is not s])
            if same(c):
                sc, changed = c, True
        for task in list(sc.tasks):
            c = replace(sc, tasks=[x for x in sc.tasks if x is not task])
            if same(c):
                sc, changed = c, True
        for s in list(sc.sounds):
            if s.dur > 120 and s.kind not in ("words",):
                c = replace(sc, sounds=[replace(x, dur=x.dur / 2) if x is s else x for x in sc.sounds])
                if same(c):
                    sc, changed = c, True
    return sc, fails(sc)


def floor_log(sc: Scenario) -> str:
    sim = FuzzSim(sc)
    try:
        sim.run()
    except Exception as e:                       # noqa: BLE001
        print("FAILED:", e)
    out = []
    for t, ev in sim.events:
        if not isinstance(ev, (F.Played, F.Timer)):
            out.append(f"{t:8.0f}  > {ev}")
    for t, a in sim.acts:
        if isinstance(a, F.Emit) and a.name == "x_floor" or not isinstance(a, (F.Emit, F.SetTimer, F.CancelTimer)):
            out.append(f"{t:8.0f}  < {a}")
    return "\n".join(sorted(out, key=lambda line: float(line.split()[0])))


class TestFloorFuzz(unittest.TestCase):
    def test_seeds(self):
        if ONE_SEED is not None:
            sc = make_scenario(int(ONE_SEED))
            print(sc.describe())
            print(floor_log(sc))
            why = fails(sc)
            self.assertIsNone(why, why)
            return
        t0 = time.time()
        found = []
        for seed in range(SEEDS):
            sc = make_scenario(seed)
            why = fails(sc)
            if why is not None:
                small, why2 = shrink(sc, why)
                found.append(f"{why}\n  minimal: {why2}\n{small.describe()}")
                if len(found) >= 3:
                    break
        if os.environ.get("FLOOR_FUZZ_VERBOSE"):
            print(f"\n{SEEDS} seeds in {time.time() - t0:.1f} s")
        self.assertEqual(found, [], "\n\n".join(found))


if __name__ == "__main__":
    unittest.main()
