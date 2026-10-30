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
            if not (self.sink.paused and ev.kind == "keepalive"):
                self.sink.q.append([ev.piece, ev.kind, ev.text, ev.dur_ms, 0.0])
        self.events.append((ev.t_ms, ev))
        acts = self.f.on(ev)
        for a in acts:
            self.acts.append((ev.t_ms, a))
        for a in acts:
            self.apply(a, ev.t_ms)
        if self.checker:
            self.checker.after(ev, acts)
        if isinstance(ev, F.TtsDone):
            self.tts_done.add(ev.rid)
            if not self.sink.q and ev.rid == self.cur:
                self.dispatch(F.Drained(ev.rid), ev.t_ms)

    def apply(self, a, t: float) -> None:
        if self.checker:
            self.checker.on_action(a)
        if isinstance(a, F.StartReply):
            if self.checker:
                self.checker.on_start(a)
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
            self.sink.q = [p for p in self.sink.q if p[1] != "keepalive"]
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


class Checker:
    """FLOOR-SPEC.md invariants. I3, I4, I5, I11, I12, I13 after every event; I1, I2, I6, I7, I8 at
    quiescence. Words come from the scenario's ground truth (Sim.truth)."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.ended: set = set()
        self.stopped: set = set()
        self.active: Optional[str] = None
        self.last_q_by_label: dict = {}
        self.pending_words: set = set()  # utts with an interim with words and no final yet
        self.reply_label: dict = {}

    def fail(self, inv: str, msg: str) -> None:
        raise InvariantError(f"{inv} at t={self.sim.t:.0f}: {msg}")

    # -- per event -------------------------------------------------------------------------------
    def on_start(self, a) -> None:
        # I4 One voice: the previous reply ended or was stopped before a new one starts.
        if self.active is not None and self.active not in self.ended | self.stopped:
            self.fail("I4", f"{a.rid} started while {self.active} is live")
        self.active = a.rid
        self.reply_label[a.rid] = a.label
        # I13 No same-question restart.
        if a.kind == "answer":
            if self.last_q_by_label.get(a.label) == a.question:
                self.fail("I13", f"label {a.label} asked {a.question!r} twice in a row")
            self.last_q_by_label[a.label] = a.question
            self._check_order(a.question)

    def _check_order(self, question: str) -> None:
        # I3 Right order: the question is word segments' finals, each at most once, in segment order.
        words = _words_of(question)
        segs = sorted((g, u) for u, g in self.sim.f.by_utt.items() if self.sim.truth.get(u))
        i = 0
        for _, u in segs:
            w = _words_of(self.sim.truth[u])
            if words[i:i + len(w)] == w:
                i += len(w)
        if i != len(words):
            self.fail("I3", f"question {question!r} is not word finals in segment order")

    def on_action(self, a) -> None:
        """Each action in order, as the adapter applies it."""
        if isinstance(a, F.StopReply):
            self.stopped.add(a.rid)
        if isinstance(a, F.Play) and a.rid in self.stopped | self.ended:
            self.fail("I4", f"play for ended reply {a.rid}")
        if isinstance(a, F.Emit) and a.name == "x_floor" and a.fields.get("act") == "end":
            self.ended.add(a.fields["rid"])

    def after(self, ev, acts) -> None:
        f, sim = self.sim.f, self.sim
        if isinstance(ev, F.Interim) and self._words(ev.utt_id, ev.text):
            seg = f.segs.get(f.by_utt.get(ev.utt_id))
            if seg is not None and seg.status != "final":
                self.pending_words.add(ev.utt_id)
        if isinstance(ev, F.Final):
            self.pending_words.discard(ev.utt_id)
        # I5 No talking over words.
        if self.pending_words and sim.sink.audible() and sim.sink.rid == sim.cur and sim.cur not in self.stopped:
            self.fail("I5", f"audio plays while {sorted(self.pending_words)} has interim words")
        r = f.reply
        if r is None:
            return
        # I11 Pause set is live.
        for g in r.pause_on:
            s = f.segs[g]
            if s.status == "final":
                self.fail("I11", f"resolved g{g} in the pause set")
            if s.kind == "blip" and f.now - s.start_ms > f.cfg["short_bargein_ms"]:
                self.fail("I11", f"old blip g{g} in the pause set")
        # I12 No early play from a hold.
        if any(isinstance(a, F.Play) for a in acts) and r.pause_on:
            newest = max(f.segs[g].start_ms for g in r.pause_on)
            if f.now < newest + f.cfg["pause_max_ms"] or not all(f._quiet(f.segs[g]) for g in r.pause_on):
                self.fail("I12", f"play at {f.now:.0f} while held on {sorted(r.pause_on)}")

    def _words(self, utt: str, text: str) -> bool:
        truth = self.sim.truth.get(utt, "")
        return bool(truth) and bool(set(_words_of(text)) & set(_words_of(truth)))

    # -- at quiescence ---------------------------------------------------------------------------
    def quiescent(self) -> None:
        sim, f = self.sim, self.sim.f
        if f.ended:
            return
        # The quiescence condition itself.
        open_segs = [g for g, s in f.segs.items() if s.status != "final"]
        if open_segs or sim.timers or sim.q or sim.sink.q:
            self.fail("quiescence", f"open {open_segs}, timers {sim.timers}, sink {len(sim.sink.q)}")
        # I8 No deadlock.
        if f.reply is not None:
            self.fail("I8", f"reply {f.reply.rid} still live (gate {f.reply.gate})")
        ends = sim.ends()
        starts = sim.starts()
        errors = {rid for rid, why in ends.items() if why == "error"}
        # I1 No lost question.
        last_by_label = {}
        for a in starts:
            last_by_label[a.label] = a
        for utt, words in sim.truth.items():
            if not words:
                continue
            w = " ".join(_words_of(words))
            hits = [a for a in last_by_label.values() if w in " ".join(_words_of(a.question))]
            if not hits:
                if any(w in " ".join(_words_of(a.question)) for a in starts if a.rid in errors):
                    continue
                self.fail("I1", f"{utt} {words!r} is in no label's last question")
            if not any(ends.get(a.rid) in ("drained", "interrupt") or a.rid in errors for a in hits):
                self.fail("I1", f"{utt}: last reply ended {[ends.get(a.rid) for a in hits]}")
        # I2 Noise never costs an answer.
        word_starts = [e.t_ms for _, e in sim.events if isinstance(e, F.SpeechStart) and sim.truth.get(e.utt_id)]
        for t0, fields in sim.floor_log("end"):
            rid = fields["rid"]
            by = fields.get("paused_by", [])
            a = next(x for x in starts if x.rid == rid)
            if not by or a.kind != "answer":
                continue
            if any(u and sim.truth.get(u) for u in by):
                continue
            began = next(t for t, x in sim.ops(F.StartReply) if x.rid == rid)
            if any(began <= ts <= t0 for ts in word_starts):
                continue
            if fields["reason"] != "drained":
                self.fail("I2", f"{rid} paused only by noise ended {fields['reason']}")
        # I6 Announcements once.
        heard = [f_["id"] for _, f_ in sim.floor_log("announce_heard")]
        for item in f.announcements:
            if heard.count(item.id) != 1:
                self.fail("I6", f"announcement {item.id} heard {heard.count(item.id)} times")
        seen_heard: set = set()
        for t, a in sim.acts:
            if isinstance(a, F.StartReply):
                for c in a.carries:
                    if c["id"] in seen_heard:
                        self.fail("I6", f"{a.rid} carries heard item {c['id']}")
            if isinstance(a, F.Emit) and a.name == "x_floor" and a.fields.get("act") == "announce_heard":
                seen_heard.add(a.fields["id"])
        # I7 Metrics.
        user_labels = {a.label for a in starts if a.kind == "answer"}
        ted = sim.emits("turn_end_detected")
        fra = sim.emits("first_reply_audio")
        for lab in user_labels:
            n = [x for x in ted if x[1].label == lab]
            if len(n) != 1:
                self.fail("I7", f"label {lab} has {len(n)} turn_end_detected")
            fr = [x for x in fra if x[1].label == lab]
            if len(fr) > 1:
                self.fail("I7", f"label {lab} has {len(fr)} first_reply_audio")
            if fr and fr[0][0] < n[0][1].fields["t_ms"]:
                self.fail("I7", f"label {lab} first_reply_audio before its turn_end_detected")
        for _, e in ted:
            if e.label not in user_labels:
                self.fail("I7", f"turn_end_detected for unanswered label {e.label}")
        for lab in {a.label for a in starts}:
            heard_content = any(a.label == lab and any(
                isinstance(ev, F.Played) and ev.rid == a.rid and ev.piece.startswith("p") for _, ev in sim.events)
                for a in starts)
            if heard_content and not [x for x in fra if x[1].label == lab]:
                self.fail("I7", f"label {lab} has heard content but no first_reply_audio")
