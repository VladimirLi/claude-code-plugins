"""Test driver for live/floor.py: a simulated clock, brain, TTS and sink, and the invariant checker.

    sim = Sim()
    sim.say(1, 0, 1000, "What is in the logs?")      # speech_start, turn_end, final for segment 1
    sim.at(2500, Detector("barge_in", "open"))
    sim.run()                                          # to quiescence; invariants checked on the way

The sim plays the adapter's part (FLOOR-SPEC.md "What changes"): it drops callbacks of a stopped
reply, buffers TTS output at a paused gate and drops keepalive pieces there, fires timers, and
turns `play`/`pause` into `played`/`drained` events on a 20 ms tick. The brain answers every
`start_reply` from a `Plan`.

Ground truth: `say(..., truth=...)` says what a segment really said ("" for noise, echo,
hallucination or a backchannel over heard content). The checker judges the controller against it.
"""
from __future__ import annotations

import heapq
import itertools
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)

from live import floor as F  # noqa: E402
from live.turntaking import normalize  # noqa: E402

TICK = 20


@dataclass
class Plan:
    """How the fake brain and TTS answer one start_reply."""
    pieces: list = field(default_factory=lambda: [("The logs show no errors.", 1500),
                                                  ("Everything passed.", 1500)])
    first_ms: float = 300.0          # start_reply -> first piece
    gap_ms: float = 100.0            # between pieces
    done_ms: Optional[float] = None  # brain_done this long after start (default: after the last piece)
    error_ms: Optional[float] = None  # brain_error this long after start; no pieces after it


class SimSink:
    def __init__(self) -> None:
        self.rid: Optional[str] = None
        self.q: list = []            # [piece, kind, text, dur_ms, pos_ms]
        self.paused = False

    def audible(self) -> bool:
        return not self.paused and bool(self.q)


class Sim:
    def __init__(self, cfg: Optional[dict] = None, plans: Union[None, Plan, dict, Callable] = None,
                 check: bool = True, **floor_kw) -> None:
        self.f = F.Floor(cfg, **floor_kw)
        self.plans = plans
        self.q: list = []
        self.seq = itertools.count()
        self.t = 0.0
        self.acts: list = []         # (t, action)
        self.events: list = []       # (t, event)
        self.timers: dict = {}
        self.sink = SimSink()
        self.cur: Optional[str] = None
        self.dead: set = set()
        self.truth: dict = {}        # utt_id -> words ("" = no words)
        self.ka = 0
        self.tts_done: set = set()
        self.checker = Checker(self) if check else None

    # -- script ----------------------------------------------------------------------------------
    def at(self, t: float, ev) -> None:
        ev.t_ms = float(t)
        heapq.heappush(self.q, (float(t), next(self.seq), ev))

    def say(self, g: int, start: float, end: float, text: str = "", final_ms: Optional[float] = 300.0,
            interims=(), hint=None, truth: Optional[str] = None, turn_end_ms: Optional[float] = None) -> str:
        """One endpointer segment: speech_start at `start`, sound until `end`, turn_end 600 ms
        later (or at `turn_end_ms`), final `final_ms` after the turn_end (None: no final)."""
        utt = f"u{g}"
        self.at(start, F.SpeechStart(g, utt, hint))
        for ti, itext in interims:
            self.at(ti, F.Interim(utt, itext))
        te = end + 600 if turn_end_ms is None else turn_end_ms
        self.at(te, F.TurnEnd(g, end))
        if final_ms is not None:
            self.at(te + final_ms, F.Final(utt, text, "spec"))
        self.truth[utt] = text if truth is None else truth
        return utt

    def detector(self, t: float, decision: str = "barge_in", reason: str = "open") -> None:
        self.at(t, F.Detector(decision, reason))

    # -- run -------------------------------------------------------------------------------------
    def run(self, until: float = 120000.0) -> "Sim":
        while True:
            cands = []
            if self.q:
                cands.append(self.q[0][0])
            if self.timers:
                cands.append(min(self.timers.values()))
            if self.sink.audible():
                cands.append((self.t // TICK + 1) * TICK)
            if not cands:
                break
            t = min(cands)
            if t > until:
                break
            self.t = max(self.t, t)
            while self.q and self.q[0][0] <= self.t:
                self.dispatch(heapq.heappop(self.q)[2])
            for name, due in sorted(self.timers.items(), key=lambda kv: kv[1]):
                if due <= self.t and self.timers.get(name) == due:
                    del self.timers[name]
                    self.dispatch(F.Timer(name), self.t)
            if self.sink.audible() and self.t % TICK == 0:
                self.tick()
        if self.checker and until >= 120000.0:
            self.checker.quiescent()
        return self

    def tick(self) -> None:
        head = self.sink.q[0]
        head[4] += TICK
        rid, piece, dur = self.sink.rid, head[0], head[3]
        ms = min(head[4], dur)
        if head[4] >= dur:
            self.sink.q.pop(0)
        self.dispatch(F.Played(rid, piece, ms), self.t)
        if not self.sink.q and self.sink.rid == rid and rid == self.cur:
            self.dispatch(F.Drained(rid), self.t)

    def dispatch(self, ev, t: Optional[float] = None) -> None:
        if t is not None:
            ev.t_ms = float(t)
        rid = getattr(ev, "rid", None)
        if rid is not None and (rid in self.dead or rid != self.cur):
            return                                   # the adapter drops a stale reply's callbacks
        if isinstance(ev, F.TtsPiece):
            self.sink.q.append([ev.piece, ev.kind, ev.text, ev.dur_ms, 0.0])
        self.events.append((ev.t_ms, ev))
        if self.checker:
            self.checker.before(ev)
        acts = self.f.on(ev)
        for a in acts:
            self.acts.append((ev.t_ms, a))
        for a in acts:
            self.apply(a, ev.t_ms)
        if self.checker:
            self.checker.after(ev, acts)

    def apply(self, a, t: float) -> None:
        if self.checker:
            self.checker.on_action(a)
        if isinstance(a, F.StartReply):
            self.cur = a.rid
            self.sink = SimSink()
            self.sink.rid = a.rid
            self.schedule(a, t)
        elif isinstance(a, F.StopReply):
            self.dead.add(a.rid)
            if self.sink.rid == a.rid:
                self.sink.q = []
        elif isinstance(a, F.Pause) and a.rid == self.sink.rid:
            self.sink.paused = True
        elif isinstance(a, F.Drop) and a.rid == self.sink.rid:
            had = bool(self.sink.q)
            self.sink.q = [p for p in self.sink.q if p[0] != a.piece]
            if had and not self.sink.q:
                self.at(t, F.Drained(a.rid))         # nothing queued or buffered any more
        elif isinstance(a, F.Play) and a.rid == self.sink.rid:
            self.sink.paused = False
            if self.sink.q:
                self.sink.q[0][4] = max(0.0, self.sink.q[0][4] - a.rewind_ms)
        elif isinstance(a, F.Keepalive):
            self.ka += 1
            self.at(t + 200, F.TtsPiece(a.rid, f"k{self.ka}", "keepalive", "Still on it.", 800))
        elif isinstance(a, F.SetTimer):
            self.timers[a.name] = a.at_ms
        elif isinstance(a, F.CancelTimer):
            self.timers.pop(a.name, None)

    def plan_for(self, a) -> Plan:
        p = self.plans
        if p is None:
            return Plan()
        if isinstance(p, Plan):
            return p
        if callable(p):
            return p(a)
        for key, plan in p.items():
            if key in a.question:
                return plan
        return p.get("", Plan())

    def schedule(self, a, t: float) -> None:
        plan = self.plan_for(a)
        rid = a.rid
        last = t
        for i, (text, dur) in enumerate(plan.pieces):
            ti = t + plan.first_ms + i * plan.gap_ms
            if plan.error_ms is not None and ti >= t + plan.error_ms:
                break
            self.at(ti, F.BrainText(rid, text))
            self.at(ti + 1, F.TtsPiece(rid, f"p{i}", "content", text, dur))
            last = ti + 1
        if plan.error_ms is not None:
            self.at(t + plan.error_ms, F.BrainError(rid))
            return
        done = t + plan.done_ms if plan.done_ms is not None else last + 1
        self.at(done, F.BrainDone(rid))
        self.at(done + 1, F.TtsDone(rid))

    # -- reading the result ----------------------------------------------------------------------
    def ops(self, cls) -> list:
        return [(t, a) for t, a in self.acts if isinstance(a, cls)]

    def starts(self) -> list:
        return [a for _, a in self.ops(F.StartReply)]

    def questions(self) -> list:
        return [a.question for a in self.starts()]

    def emits(self, name: str) -> list:
        return [(t, a) for t, a in self.ops(F.Emit) if a.name == name]

    def floor_log(self, act: Optional[str] = None) -> list:
        return [(t, a.fields) for t, a in self.emits("x_floor") if act is None or a.fields.get("act") == act]

    def ends(self) -> dict:
        """rid -> how the reply ended (drained | interrupt | restart | error | end)."""
        return {f["rid"]: f["reason"] for _, f in self.floor_log("end")}

    def played_pieces(self, rid: str) -> list:
        return [e.piece for _, e in self.events if isinstance(e, F.Played) and e.rid == rid]


def _words_of(text: str) -> list:
    return normalize(text or "").split()


class InvariantError(AssertionError):
    pass


@dataclass
class _Seg:
    utt: str
    order: int
    start: float
    turn_end: Optional[float] = None
    final: Optional[float] = None
    seen: bool = False           # an interim came before the final
    words_interim: bool = False  # an interim with ground-truth words came before the final


@dataclass
class _Reply:
    rid: str
    label: str
    kind: str
    question: str
    start: float
    carries: list
    held: set                    # rule 1/5: segments unresolved at start or started before content
    first_content: Optional[float] = None
    brain_done: bool = False
    tts_done: bool = False
    error: bool = False
    stopped: bool = False
    drained: bool = True         # nothing queued since the sink's last drained
    content: dict = field(default_factory=dict)   # piece -> dur_ms
    played: dict = field(default_factory=dict)    # piece -> ms
    det_pauses: list = field(default_factory=list)  # [t] detector pauses not yet ended
    nowords_det_pauses: int = 0

    @property
    def ended(self) -> bool:
        return self.stopped or self.error or (self.brain_done and self.tts_done and self.drained)


class Checker:
    """FLOOR-SPEC.md invariants, judged only from ground truth (Sim.truth and the scripted segment
    events) and from what the adapter sees (actions, the sink). It never reads the controller's
    state. I3, I4, I5, I9, I10, I11, I12, I13 after every event; I1, I6, I7, I8 at quiescence.
    I2's "noise never stops a reply" is checked on every stop (I9); its rewind clause is not."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.cfg = sim.f.cfg                 # configuration only
        self.segs: dict = {}                 # utt -> _Seg
        self.ext: dict = {}                  # endpointer g -> utt
        self.replies: dict = {}
        self.order: list = []                # rids in start order
        self.detector_t: list = []
        self.word_finals: list = []          # times of finals whose ground truth has words
        self.pending_words: set = set()
        self.last_q_by_label: dict = {}
        self.ev = None
        self.emits: list = []                # (t, Emit)

    def fail(self, inv: str, msg: str) -> None:
        raise InvariantError(f"{inv} at t={self.sim.t:.0f}: {msg}")

    # -- ground truth ----------------------------------------------------------------------------
    def _truth(self, utt: str) -> str:
        return self.sim.truth.get(utt, "") or ""

    def _optional(self, utt: str) -> bool:
        return getattr(self.sim.truth, "optional", lambda u: False)(utt)

    def _words(self, utt: str, text: str) -> bool:
        if hasattr(self.sim.truth, "interim_has_words"):
            return self.sim.truth.interim_has_words(utt, text)
        truth = self._truth(utt)
        return bool(truth) and bool(set(_words_of(text)) & set(_words_of(truth)))

    def _unresolved(self) -> list:
        return [s for s in self.segs.values() if s.final is None]

    def _quiet(self, s: _Seg) -> bool:
        """Rule 4, from ground truth: an interim with no words, or the turn_end with none."""
        return not s.words_interim and (s.seen or s.turn_end is not None)

    def partition(self, question: str, before: Optional[float] = None, exclude=()) -> Optional[list]:
        """The word segments whose truths make up `question`, in segment order, or None. Only
        segments final by `before` and not in `exclude` (two segments can say the same thing)."""
        words = _words_of(question)
        segs = sorted((s for s in self.segs.values() if _words_of(self._truth(s.utt)) and s.utt not in exclude
                       and (before is None or (s.final is not None and s.final <= before))),
                      key=lambda s: s.order)
        texts = [_words_of(self._truth(s.utt)) for s in segs]

        def fits(i: int, k: int) -> Optional[list]:
            if i == len(words):
                return []
            for j in reversed(range(k, len(texts))):     # a repeated text: the newest segment
                w = texts[j]
                if words[i:i + len(w)] == w:
                    rest = fits(i + len(w), j + 1)
                    if rest is not None:
                        return [segs[j]] + rest
            return None

        return fits(0, 0) if words else None

    def before(self, ev) -> None:
        """Record the event's ground truth before the controller sees it."""
        self.ev = ev
        t = ev.t_ms
        if isinstance(ev, F.SpeechStart):
            s = _Seg(ev.utt_id, len(self.segs), t)
            self.segs[ev.utt_id], self.ext[ev.g] = s, ev.utt_id
            r = self._live()
            if r is not None and r.first_content is None:
                r.held.add(ev.utt_id)
        elif isinstance(ev, F.Interim):
            s = self.segs.get(ev.utt_id)
            if s is not None and s.final is None:
                s.seen = True
                if self._words(ev.utt_id, ev.text):
                    s.words_interim = True
                    self.pending_words.add(ev.utt_id)
        elif isinstance(ev, F.TurnEnd):
            s = self.segs.get(self.ext.get(ev.g))
            if s is not None and s.turn_end is None:
                s.turn_end = t
        elif isinstance(ev, F.Final):
            s = self.segs.get(ev.utt_id)
            if s is not None and s.final is None:
                s.final = t
                self.pending_words.discard(ev.utt_id)
                if self._truth(ev.utt_id):
                    self.word_finals.append(t)
        elif isinstance(ev, F.Detector) and ev.decision == "barge_in":
            self.detector_t.append(t)
        elif getattr(ev, "rid", None) in self.replies:
            r = self.replies[ev.rid]
            if isinstance(ev, F.BrainDone):
                r.brain_done = True
            elif isinstance(ev, F.TtsDone):
                r.tts_done = True
            elif isinstance(ev, F.BrainError):
                r.error = True
            elif isinstance(ev, F.TtsPiece):
                r.drained = False
                if ev.kind == "content":
                    r.content[ev.piece] = ev.dur_ms
            elif isinstance(ev, F.Drained):
                r.drained = True
            elif isinstance(ev, F.Played) and ev.piece in r.content:
                r.played[ev.piece] = max(r.played.get(ev.piece, 0.0), ev.ms)
                if r.first_content is None:
                    r.first_content = t

    def _live(self) -> Optional[_Reply]:
        r = self.replies.get(self.order[-1]) if self.order else None
        return r if r is not None and not r.ended else None

    def _heard(self, r: _Reply) -> bool:
        """Rule 7, from what played: brain and TTS done and 60% of the content audio played."""
        total = sum(r.content.values())
        done = sum(min(r.played.get(p, 0.0), d) for p, d in r.content.items())
        return r.brain_done and r.tts_done and total > 0 and done / total >= self.cfg["heard_frac"]

    # -- actions, in the order the adapter applies them ------------------------------------------
    def on_action(self, a) -> None:
        sim, t, ev = self.sim, self.sim.t, self.ev
        if isinstance(a, F.StartReply):
            prev = self.replies.get(self.order[-1]) if self.order else None
            # I4 One voice: the previous reply ended, or was stopped, before a new one starts.
            if prev is not None and not prev.ended:
                self.fail("I4", f"{a.rid} started while {prev.rid} is live")
            # I6: never carry an item a reply already told.
            for c in a.carries:
                if any(c["id"] in x.carries and self._heard(x) for x in self.replies.values()):
                    self.fail("I6", f"{a.rid} carries heard item {c['id']}")
            unresolved = {s.utt for s in self._unresolved()}
            self.replies[a.rid] = _Reply(a.rid, a.label, a.kind, a.question, t, [c["id"] for c in a.carries],
                                         unresolved)
            self.order.append(a.rid)
            if a.kind == "answer":
                # I13 No same-question restart.
                if self.last_q_by_label.get(a.label) == a.question:
                    self.fail("I13", f"label {a.label} asked {a.question!r} twice in a row")
                self.last_q_by_label[a.label] = a.question
                # I3 Right order.
                if self.partition(a.question) is None:
                    self.fail("I3", f"question {a.question!r} is not word finals in segment order")
            return
        r = self.replies.get(getattr(a, "rid", None))
        if isinstance(a, F.StopReply) and r is not None:
            r.stopped = True
            # I9 (and I2): only words stop a reply; a brain error or the session end may too.
            ok = (isinstance(ev, F.Final) and (self._truth(ev.utt_id) or self._optional(ev.utt_id))
                  or isinstance(ev, (F.BrainError, F.End)))
            if not ok:
                self.fail("I9", f"{a.rid} stopped by {type(ev).__name__} without words")
        elif isinstance(a, F.Pause) and r is not None:
            if isinstance(ev, F.Detector):
                if sim.f.cfg["speakers"]:
                    self.fail("I9", "a detector decision paused a reply on speakers")
                if r.first_content is not None:
                    r.det_pauses.append(t)
        elif isinstance(a, F.Play) and r is not None:
            if r.ended:
                self.fail("I4", f"play for ended reply {a.rid}")
            # I10 No stutter: a detector pause has ended with no words when the reply plays with
            # nothing unresolved and no word final since the pause.
            if not self._unresolved():
                for tp in r.det_pauses:
                    if not any(tp <= tf <= t for tf in self.word_finals):
                        r.nowords_det_pauses += 1
                r.det_pauses = []
                if r.nowords_det_pauses > self.cfg["max_nowords_pauses"]:
                    self.fail("I10", f"{a.rid} had {r.nowords_det_pauses} no-word detector pauses")
        elif isinstance(a, F.Emit):
            self.emits.append((t, a))

    # -- after every event -----------------------------------------------------------------------
    def after(self, ev, acts) -> None:
        sim, t = self.sim, self.sim.t
        r = self._live()
        audible = r is not None and sim.sink.rid == r.rid and sim.sink.audible()
        # I5 No talking over words.
        if audible and self.pending_words:
            self.fail("I5", f"audio plays while {sorted(self.pending_words)} has interim words")
        if r is None:
            return
        # I12 No early play from a hold: rule 1/5 holds play only at rule 4's time.
        held = [self.segs[u] for u in r.held if self.segs[u].final is None]
        if audible and held:
            cap = max(s.start for s in held) + self.cfg["pause_max_ms"]
            if t < cap or not all(self._quiet(s) for s in held):
                self.fail("I12", f"{r.rid} plays at {t:.0f} while held on {sorted(s.utt for s in held)}")
        # I11 Pause set is live: a paused gate with audio waits only on an unresolved segment or a
        # detector barge-in younger than short_bargein_ms.
        if sim.sink.rid == r.rid and sim.sink.paused and sim.sink.q:
            young = any(t - td <= self.cfg["short_bargein_ms"] for td in self.detector_t)
            if not self._unresolved() and not young:
                self.fail("I11", f"{r.rid} paused with nothing unresolved")

    # -- at quiescence ---------------------------------------------------------------------------
    def quiescent(self) -> None:
        sim = self.sim
        if any(isinstance(e, F.End) for _, e in sim.events):
            return
        open_segs = [s.utt for s in self._unresolved()]
        if open_segs or sim.timers or sim.q or sim.sink.q:
            self.fail("quiescence", f"open {open_segs}, timers {sim.timers}, sink {len(sim.sink.q)}")
        # I8 No deadlock: every reply ended.
        for r in self.replies.values():
            if not r.ended:
                self.fail("I8", f"reply {r.rid} never ended")
        answers = [self.replies[rid] for rid in self.order if self.replies[rid].kind == "answer"]
        last = {}
        for r in answers:
            last[r.label] = r
        # I1 No lost question: every word segment is in the last question of a label. (That
        # reply drained, or words stopped it: I9 checks every stop.)
        # Each label's segments, labels in start order, so a repeated text goes to the right label.
        claimed: dict = {}
        used: set = set()
        for lab, r in last.items():
            claimed[lab] = self.partition(r.question, r.start, used) or []
            used |= {s.utt for s in claimed[lab]}
        covered = used
        errored = set()
        for r in answers:
            if r.error:
                errored |= {s.utt for s in self.partition(r.question) or []}
        for utt, s in self.segs.items():
            if self._truth(utt) and not self._optional(utt) and utt not in covered | errored:
                self.fail("I1", f"{utt} {self._truth(utt)!r} is in no label's last question")
        # I6 Announcements once.
        items = {c for r in self.replies.values() for c in r.carries}
        items |= {e.id for _, e in sim.events if isinstance(e, F.TaskResult)}
        dropped = {e.fields["id"] for _, e in self.emits if e.name == "x_announce_dropped"}
        for item in items:
            n = sum(1 for r in self.replies.values() if item in r.carries and self._heard(r))
            silent = sum(1 for r in self.replies.values() if item in r.carries and r.first_content is None)
            if item in dropped:
                # Rule 7: dropped only after 3 carrying replies that played nothing, and never heard.
                if n or silent < self.cfg["max_silent_tries"]:
                    self.fail("I6", f"announcement {item} dropped after {silent} silent tries, heard {n}")
            elif n != 1:
                self.fail("I6", f"announcement {item} heard {n} times")
            if any(item in r.carries and r.start > t for r in self.replies.values()
                   for t, e in self.emits if e.name == "x_announce_dropped" and e.fields["id"] == item):
                self.fail("I6", f"announcement {item} carried after it was dropped")
        # I7 Metrics.
        ted = [(t, e) for t, e in self.emits if e.name == "turn_end_detected"]
        fra = [(t, e) for t, e in self.emits if e.name == "first_reply_audio"]
        for lab in last:
            mine = [e for _, e in ted if e.label == lab]
            if len(mine) != 1:
                self.fail("I7", f"label {lab} has {len(mine)} turn_end_detected")
            heard = [r for r in answers if r.label == lab and r.first_content is not None]
            first = min(heard, key=lambda r: r.first_content) if heard else last[lab]
            mine_segs = {s.utt for s in claimed[lab]}
            segs = [s for s in self.partition(first.question, first.start,
                                              used - mine_segs) or []]
            want = max(segs, key=lambda s: s.order).turn_end if segs else None
            if mine[0].fields["t_ms"] != want:
                self.fail("I7", f"label {lab} turn_end_detected t_ms {mine[0].fields['t_ms']} != {want}")
            got = [t for t, e in fra if e.label == lab]
            if heard and got != [first.first_content]:
                self.fail("I7", f"label {lab} first_reply_audio at {got}, content at {first.first_content}")
            if not heard and got:
                self.fail("I7", f"label {lab} first_reply_audio with no content")
        for _, e in ted:
            if e.label not in last:
                self.fail("I7", f"turn_end_detected for unanswered label {e.label}")
