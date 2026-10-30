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
    speech_end: Optional[float] = None
    final: Optional[float] = None
    seen: bool = False           # an interim came before the final
    words_interim: bool = False  # an interim with ground-truth words came before the final


@dataclass
class _Span:
    """One piece as it played: from the sim's TtsPiece and Played events."""
    rid: str
    piece: str
    kind: str
    text: str
    t_start: float
    t_end: float
    open: bool = True            # a pause ends the span; the next play starts a new one


@dataclass
class _Reply:
    rid: str
    label: str
    kind: str
    question: str
    context: object
    start: float
    carries: list
    group: set                   # segments unresolved at start: its question group (rule 3)
    held: set                    # rule 1/5: the group, and segments started before content
    first_content: Optional[float] = None
    brain_done: bool = False
    tts_done: bool = False
    error: bool = False
    stopped: bool = False
    drained: bool = True         # nothing queued since the sink's last drained
    pieces: list = field(default_factory=list)    # [(piece, kind, text, dur)] in arrival order
    played: dict = field(default_factory=dict)    # piece -> furthest ms
    offset: Optional[tuple] = None                # (piece, ms) of the last Played
    ka_played: bool = False
    last_pause: Optional[float] = None
    last_play: float = 0.0
    sound: float = 0.0           # rule 8: start, last play, last content audio
    ka_sent: Optional[float] = None
    det_pauses: list = field(default_factory=list)
    nowords_det_pauses: int = 0
    resumed: Optional[float] = None   # last play after a no-word detector pause (rule 2's guard)
    speech_since: bool = False        # a speech_start came after that play

    @property
    def ended(self) -> bool:
        return self.stopped or self.error or (self.brain_done and self.tts_done and self.drained)

    @property
    def content(self) -> dict:
        return {p: d for p, k, _, d in self.pieces if k == "content"}

    def spoken(self) -> str:
        """Spec: the text up to the furthest played offset, a piece in proportion to its words."""
        out = []
        for p, k, text, dur in self.pieces:
            ms = self.played.get(p, 0.0)
            if k != "content" or ms <= 0:
                continue
            words = text.split()
            out.extend(words[:len(words) if ms >= dur else round(len(words) * ms / dur)])
        return " ".join(out)


class Checker:
    """FLOOR-SPEC.md invariants and rules, judged only from ground truth (Sim.truth and the
    scripted segment events) and from what the adapter sees (actions, the sink, played audio). It
    never reads the controller's state; it keeps its own model of what played.

    After every event: I3, I4, I5, I9, I10, I11, I12, I13, and the rule oracles: join vs interrupt
    (rule 3), the next reply's context, Play.rewind_ms (the rewind table), keepalive timing (rule
    8), duck (rule 2). At quiescence: I1, I6, I7, I8, and every reply that drained played all its
    content (I2)."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.cfg = sim.f.cfg                 # configuration only
        self.segs: dict = {}                 # utt -> _Seg
        self.ext: dict = {}                  # endpointer g -> utt
        self.replies: dict = {}
        self.order: list = []                # rids in start order
        self.spans: dict = {}                # (rid, piece) -> its current _Span
        self.all_spans: list = []            # every stretch that played
        self.blips: list = []                # [t, absorbing utt]: detector pauses with nothing open
        self.caused: set = set()             # segments that surely paused the gate (spec over heard)
        self.det_ducking = False             # last detector decision was duck (no resume yet)
        self.duck_only: list = []            # times of duck-only detector decisions
        self.detector_bi: list = []          # detector barge-ins while a reply was live (headphones):
                                             # each makes a blip or attaches to a segment
        self.word_finals: list = []          # times of finals whose ground truth has words
        self.pending_words: set = set()
        self.last_q_by_label: dict = {}
        self.ctx_pending = None              # context the next new utterance's reply must get
        self.last_ended = None               # (spoken, question) of the last ended answer
        self.ducked = False
        self.open_since: Optional[float] = None   # since when the gate has been open (rule 8)
        self.ev = None
        self.live_before: Optional[_Reply] = None
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

    def over_heard(self, start: float, end: float, caused: bool = False) -> Optional[bool]:
        """Spec "over heard content" from what played: content audio from 0.5 s before `start` to
        `end`, except a question tail. None when a detector pause or duck near the segment may
        have made it over heard ("it paused or ducked the gate") though no content was in the
        window, unless it surely did (`caused`: its own detector pause, or its blip's)."""
        content = [s for s in self.all_spans if s.kind == "content"]
        at = [s for s in content if s.t_start <= start <= s.t_end]
        recent = [s for s in content if 0 <= start - s.t_end <= self.cfg["tail_ms"]]
        tail = at[-1] if at else (max(recent, key=lambda s: s.t_end) if recent else None)
        if tail is not None and tail.text.rstrip().endswith("?") and \
                not any(s.piece != tail.piece and tail.t_start < s.t_start <= end for s in content):
            return False
        lead = start - self.cfg["echo_lead_ms"]
        if any(s.t_end >= lead and s.t_start <= end for s in content):
            return True
        if caused:
            return True
        if any(start - self.cfg["short_bargein_ms"] <= t <= end for t in self.detector_bi):
            return None
        return False

    def seg_over_heard(self, utt: str) -> Optional[bool]:
        s = self.segs[utt]
        return self.over_heard(s.start, s.speech_end if s.speech_end is not None else self.sim.t,
                               utt in self.caused)

    def partition(self, question: str, before: Optional[float] = None, exclude=()) -> Optional[list]:
        """The word segments whose truths make up `question`, in segment order, or None. Only
        segments final by `before` and not in `exclude` (two segments can say the same thing);
        a repeated text goes to the newest segment."""
        words = _words_of(question)
        segs = sorted((s for s in self.segs.values() if _words_of(self._truth(s.utt)) and s.utt not in exclude
                       and (before is None or (s.final is not None and s.final <= before))),
                      key=lambda s: s.order)
        texts = [_words_of(self._truth(s.utt)) for s in segs]

        def fits(i: int, k: int) -> Optional[list]:
            if i == len(words):
                return []
            for j in reversed(range(k, len(texts))):
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
        self.live_before = self._live()
        t = ev.t_ms
        if isinstance(ev, F.SpeechStart):
            s = _Seg(ev.utt_id, len(self.segs), t)
            self.segs[ev.utt_id], self.ext[ev.g] = s, ev.utt_id
            r = self._live()
            if r is not None and r.first_content is None:
                r.held.add(ev.utt_id)
            if r is not None:
                r.speech_since = True
            # A segment within short_bargein_ms of a blip absorbs it, and what it caused.
            live = [b for b in self.blips if b[1] is None and t - b[0] <= self.cfg["short_bargein_ms"]]
            if live:
                live[-1][1] = ev.utt_id
                self.caused.add(ev.utt_id)
            for b in self.blips:
                if b[1] is None:
                    b[1] = ""
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
                if s.final is None:
                    s.speech_end = ev.pause_start_ms if ev.pause_start_ms is not None else t
        elif isinstance(ev, F.Final):
            s = self.segs.get(ev.utt_id)
            if s is not None and s.final is None:
                s.final = t
                if s.speech_end is None:
                    s.speech_end = t
                self.pending_words.discard(ev.utt_id)
                if self._truth(ev.utt_id):
                    self.word_finals.append(t)
        elif isinstance(ev, F.Detector):
            if ev.decision in ("duck", "resume"):
                self.det_ducking = ev.decision == "duck"
            elif ev.decision == "barge_in" and self.live_before is not None and not self.cfg["speakers"]:
                self.detector_bi.append(t)
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
                r.pieces.append((ev.piece, ev.kind, ev.text, ev.dur_ms))
            elif isinstance(ev, F.Drained):
                r.drained = True
            elif isinstance(ev, F.Played):
                kind = next((k for p, k, _, _ in r.pieces if p == ev.piece), None)
                if kind is None or self.sim.sink.paused:
                    return
                r.played[ev.piece] = max(r.played.get(ev.piece, 0.0), ev.ms)
                r.offset = (ev.piece, ev.ms)
                text = next(x for p, _, x, _ in r.pieces if p == ev.piece)
                span = self.spans.get((r.rid, ev.piece))
                if span is None or not span.open:
                    span = _Span(r.rid, ev.piece, kind, text, max(t - ev.ms, r.last_play), t)
                    self.spans[(r.rid, ev.piece)] = span
                    self.all_spans.append(span)
                span.t_end = t
                if kind == "content":
                    r.sound = t
                    if r.first_content is None:
                        r.first_content = t
                else:
                    r.ka_played = True

    def _live(self) -> Optional[_Reply]:
        r = self.replies.get(self.order[-1]) if self.order else None
        return r if r is not None and not r.ended else None

    def _heard(self, r: _Reply) -> bool:
        """Rule 7, from what played: brain and TTS done and 60% of the content audio played."""
        content = r.content
        total = sum(content.values())
        done = sum(min(r.played.get(p, 0.0), d) for p, d in content.items())
        return r.brain_done and r.tts_done and total > 0 and done / total >= self.cfg["heard_frac"]

    def _ctx(self, c) -> Optional[tuple]:
        return None if c is None else (c.spoken, c.question)

    # -- actions, in the order the adapter applies them ------------------------------------------
    def on_action(self, a) -> None:
        sim, t, ev = self.sim, self.sim.t, self.ev
        if isinstance(a, F.StartReply):
            self._on_start(a, t, ev)
            return
        if isinstance(a, F.Emit):
            self.emits.append((t, a))
            return
        if isinstance(a, F.ResetDetector) and isinstance(ev, F.Detector):
            self.duck_only.append(t)
        if isinstance(a, (F.Duck, F.Unduck)):
            self.ducked = isinstance(a, F.Duck)
        r = self.replies.get(getattr(a, "rid", None))
        if r is None:
            return
        if isinstance(a, F.StopReply):
            r.stopped = True
            self._ended(r)
            # I9 (and I2): only words stop a reply; a brain error or the session end may too.
            ok = (isinstance(ev, F.Final) and (self._truth(ev.utt_id) or self._optional(ev.utt_id))
                  or isinstance(ev, (F.BrainError, F.End)))
            if not ok:
                self.fail("I9", f"{a.rid} stopped by {type(ev).__name__} without words")
        elif isinstance(a, F.Pause):
            r.last_pause = t
            for (rid, _), span in self.spans.items():
                if rid == r.rid:
                    span.open = False
            if isinstance(ev, F.Detector):
                if sim.f.cfg["speakers"]:
                    self.fail("I9", "a detector decision paused a reply on speakers")
                if r.first_content is not None:
                    # Rule 2: after a no-word pause the detector only ducks, for resume_guard_ms
                    # (until new speech) and for good after max_nowords_pauses.
                    if r.resumed is not None and not r.speech_since and t - r.resumed < self.cfg["resume_guard_ms"]:
                        self.fail("rule 2", f"{r.rid} paused inside the resume guard")
                    if r.nowords_det_pauses >= self.cfg["max_nowords_pauses"]:
                        self.fail("rule 2", f"{r.rid} paused after {r.nowords_det_pauses} no-word pauses")
                    open_ = [s for s in self.segs.values() if s.turn_end is None and s.final is None]
                    recent_duck = any(0 <= t - td <= 3000 for td in self.duck_only)
                    if not open_ and not recent_duck:
                        self.blips.append([t, None])
                    elif open_ and not recent_duck:
                        self.caused.add(max(open_, key=lambda s: s.order).utt)
                # Rule 1: a pause on a held segment is part of its hold and never counts.
                open_ = [s for s in self.segs.values() if s.turn_end is None and s.final is None]
                target = max(open_, key=lambda s: s.order).utt if open_ else None
                if r.first_content is not None and target not in r.held:
                    r.det_pauses.append(t)
        elif isinstance(a, F.Play):
            if r.ended:
                self.fail("I4", f"play for ended reply {a.rid}")
            self._check_rewind(r, a, t)
            r.sound = r.last_play = t
            # I10 No stutter: a detector pause has ended with no words when the reply plays with
            # nothing unresolved and no word final since the pause.
            if not self._unresolved():
                # One pause episode ends here; it counts once if a detector pause in it had no
                # word final after it.
                if any(not any(tp <= tf <= t for tf in self.word_finals) for tp in r.det_pauses):
                    r.nowords_det_pauses += 1
                    r.resumed, r.speech_since = t, False
                r.det_pauses = []
                if r.nowords_det_pauses > self.cfg["max_nowords_pauses"]:
                    self.fail("I10", f"{a.rid} had {r.nowords_det_pauses} no-word detector pauses")
        elif isinstance(a, F.Keepalive):
            # Rule 8: not while paused, not with the brain done, not before the silence is long.
            if sim.sink.rid == r.rid and sim.sink.paused:
                self.fail("rule 8", f"keepalive for {r.rid} while paused")
            if r.brain_done:
                self.fail("rule 8", f"keepalive for {r.rid} after brain_done")
            due = (r.ka_sent + self.cfg["keepalive_every_ms"] if r.ka_sent is not None and r.ka_sent >= r.sound
                   else r.sound + self.cfg["keepalive_after_ms"])
            if t < due - 1e-6:
                self.fail("rule 8", f"keepalive for {r.rid} at {t:.0f}, due {due:.0f}")
            r.ka_sent = t

    def _ended(self, r: _Reply) -> None:
        if r.kind == "answer":
            self.last_ended = (r.spoken(), r.question)
        else:
            self.last_ended = None

    def _check_rewind(self, r: _Reply, a, t: float) -> None:
        """The rewind table, from the pause length and the offset into the piece."""
        if r.last_pause is None or r.offset is None:
            want = 0.0
        else:
            length, into = t - r.last_pause, r.offset[1]
            want = None
            for under, ms in self.cfg["rewind"]:
                if length < under:
                    want = float(min(ms, into))
                    break
            if want is None:
                want = float(min(into, self.cfg["rewind_max_ms"]))
        if abs(a.rewind_ms - want) > 1e-6:
            self.fail("rewind", f"{r.rid} rewind {a.rewind_ms} != {want}")

    def _on_start(self, a, t: float, ev) -> None:
        prev = self.replies.get(self.order[-1]) if self.order else None
        # I4 One voice: the previous reply ended, or was stopped, before a new one starts.
        if prev is not None and not prev.ended:
            self.fail("I4", f"{a.rid} started while {prev.rid} is live")
        if prev is not None and not prev.stopped and not prev.error:
            self._ended(prev)
        # I6: never carry an item a reply already told.
        for c in a.carries:
            if any(c["id"] in x.carries and self._heard(x) for x in self.replies.values()):
                self.fail("I6", f"{a.rid} carries heard item {c['id']}")
        if a.kind == "answer":
            self._check_context(a, prev, ev)
        unresolved = {s.utt for s in self._unresolved()}
        self.replies[a.rid] = _Reply(a.rid, a.label, a.kind, a.question, a.context, t,
                                     [c["id"] for c in a.carries], set(unresolved), set(unresolved), sound=t)
        self.order.append(a.rid)
        if a.kind == "answer":
            # I13 No same-question restart.
            if self.last_q_by_label.get(a.label) == a.question:
                self.fail("I13", f"label {a.label} asked {a.question!r} twice in a row")
            self.last_q_by_label[a.label] = a.question
            # I3 Right order.
            if self.partition(a.question) is None:
                self.fail("I3", f"question {a.question!r} is not word finals in segment order")

    def _check_context(self, a, prev: Optional[_Reply], ev) -> None:
        """The context a reply starts with: a restart passes what was heard (or the old context);
        a new utterance's first reply gets the interrupted, failed or over-heard reply's."""
        restart = prev is not None and prev.kind == "answer" and prev.label == a.label and prev.stopped \
            and prev.start == max(r.start for r in self.replies.values())
        if restart:
            want = (prev.spoken(), prev.question) if prev.first_content is not None else self._ctx(prev.context)
        else:
            stopped_now = prev is not None and prev.stopped and self.live_before is prev
            if stopped_now:
                want = (prev.spoken(), prev.question)
            elif self.ctx_pending is not None:
                want = self.ctx_pending
            elif isinstance(ev, F.Final) and self.live_before is None and self.last_ended is not None:
                over = self.seg_over_heard(ev.utt_id)
                if over is None:
                    self.ctx_pending = None
                    return
                want = self.last_ended if over else None
            else:
                want = None
            self.ctx_pending = None
        if self._ctx(a.context) != want:
            self.fail("context", f"{a.rid} context {self._ctx(a.context)} != {want}")

    # -- after every event -----------------------------------------------------------------------
    def after(self, ev, acts) -> None:
        sim, t = self.sim, self.sim.t
        prev = self.live_before
        if isinstance(ev, F.BrainError) and prev is not None and prev.rid == ev.rid:
            self._ended(prev)
            if prev.first_content is not None:
                self.ctx_pending = (prev.spoken(), prev.question)
        if prev is not None and prev.ended and not prev.stopped and not prev.error:
            self._ended(prev)                # drained after brain and TTS, in any order
        if isinstance(ev, F.Final) and prev is not None:
            self._check_join(ev, prev, acts)
        r = self._live()
        audible = r is not None and sim.sink.rid == r.rid and sim.sink.audible()
        # I5 No talking over words.
        if audible and self.pending_words:
            self.fail("I5", f"audio plays while {sorted(self.pending_words)} has interim words")
        # Rule 2: the volume is ducked only while something may still be speaking.
        if self.ducked:
            young = any(t - td <= self.cfg["short_bargein_ms"] for td in self.detector_bi)
            if r is None or not (self._unresolved() or young or self.det_ducking):
                self.fail("duck", "ducked with nothing to duck for")
        if isinstance(ev, F.Detector) and ev.decision == "duck" and r is not None and not sim.f.cfg["speakers"]:
            if not self.ducked:
                self.fail("duck", "detector duck did not duck")
        if r is None:
            return
        # Rule 8 liveness: with the brain running and the gate open (playing, nothing unresolved,
        # no blip), a keepalive is sent once nothing has played for keepalive_after_ms.
        young = any(t - td <= self.cfg["short_bargein_ms"] for td in self.detector_bi)
        if (not r.brain_done and not r.error and sim.sink.rid == r.rid and not sim.sink.paused
                and not self._unresolved() and not young):
            if self.open_since is None:
                self.open_since = t                  # the gate opened now: an overdue keepalive is due now
            due = (r.ka_sent + self.cfg["keepalive_every_ms"] if r.ka_sent is not None and r.ka_sent >= r.sound
                   else r.sound + self.cfg["keepalive_after_ms"])
            due = max(due, self.open_since)
            if t > due + TICK and not (r.ka_sent is not None and r.ka_sent >= due - 1e-6):
                self.fail("rule 8", f"{r.rid} sent no keepalive by {t:.0f} (due {due:.0f})")
        else:
            self.open_since = None
        # I12 No early play from a hold: rule 1/5 holds play only at rule 4's time.
        held = [self.segs[u] for u in r.held if self.segs[u].final is None]
        if audible and held:
            cap = max(s.start for s in held) + self.cfg["pause_max_ms"]
            if t < cap or not all(self._quiet(s) for s in held):
                self.fail("I12", f"{r.rid} plays at {t:.0f} while held on {sorted(s.utt for s in held)}")
        # I11 Pause set is live: a paused gate with audio waits only on an unresolved segment or a
        # detector pause younger than short_bargein_ms.
        if sim.sink.rid == r.rid and sim.sink.paused and sim.sink.q:
            young = any(t - td <= self.cfg["short_bargein_ms"] for td in self.detector_bi)
            if not self._unresolved() and not young:
                self.fail("I11", f"{r.rid} paused with nothing unresolved")

    def _check_join(self, ev, r: _Reply, acts) -> None:
        """Rule 3, from ground truth: a word final joins the live reply or interrupts it."""
        utt = ev.utt_id
        if utt not in self.segs or not self._truth(utt) or self._optional(utt):
            return
        starts = [a for a in acts if isinstance(a, F.StartReply)]
        if r.kind == "announce":
            want = "interrupt"
        elif utt in r.group:
            want = "join"
        else:
            over = self.seg_over_heard(utt)
            if over is None:
                return
            last = self.partition(r.question, r.start)
            last_end = max(last, key=lambda s: s.order).speech_end if last else None
            join = (not over and r.first_content is None and not r.ka_played and last_end is not None
                    and self.segs[utt].start - last_end <= self.cfg["join_gap_ms"])
            want = "join" if join else "interrupt"
        got = ("join" if starts and starts[-1].label == r.label else
               "interrupt" if starts and any(isinstance(a, F.StopReply) for a in acts) else "none")
        if got != want:
            self.fail("rule 3", f"{utt} {self._truth(utt)!r}: {got}, want {want}")

    # -- at quiescence ---------------------------------------------------------------------------
    def quiescent(self) -> None:
        sim = self.sim
        if any(isinstance(e, F.End) for _, e in sim.events):
            return
        open_segs = [s.utt for s in self._unresolved()]
        if open_segs or sim.timers or sim.q or sim.sink.q:
            self.fail("quiescence", f"open {open_segs}, timers {sim.timers}, sink {len(sim.sink.q)}")
        if self.ducked:
            self.fail("duck", "still ducked at quiescence")
        for r in self.replies.values():
            # I8 No deadlock: every reply ended.
            if not r.ended:
                self.fail("I8", f"reply {r.rid} never ended")
            # I2: a reply that ended by draining played all its content.
            if not r.stopped and not r.error:
                for p, d in r.content.items():
                    if r.played.get(p, 0.0) < d:
                        self.fail("I2", f"{r.rid} drained without playing {p} ({r.played.get(p, 0.0)}/{d})")
        answers = [self.replies[rid] for rid in self.order if self.replies[rid].kind == "answer"]
        last = {}
        for r in answers:
            last[r.label] = r
        # I1 No lost question: every word segment is in the last question of a label (labels in
        # start order, so a repeated text goes to the right label). That reply drained, or words
        # stopped it: I9 checks every stop.
        claimed: dict = {}
        used: set = set()
        for lab, r in last.items():
            claimed[lab] = self.partition(r.question, r.start, used) or []
            used |= {s.utt for s in claimed[lab]}
        errored = set()
        for r in answers:
            if r.error:
                errored |= {s.utt for s in self.partition(r.question) or []}
        for utt in self.segs:
            if self._truth(utt) and not self._optional(utt) and utt not in used | errored:
                self.fail("I1", f"{utt} {self._truth(utt)!r} is in no label's last question")
        # I6 Announcements: heard once, or dropped after 3 silent tries and never carried again.
        items = {c for r in self.replies.values() for c in r.carries}
        items |= {e.id for _, e in sim.events if isinstance(e, F.TaskResult)}
        dropped = {e.fields["id"]: t for t, e in self.emits if e.name == "x_announce_dropped"}
        for item in items:
            n = sum(1 for r in self.replies.values() if item in r.carries and self._heard(r))
            silent = sum(1 for r in self.replies.values() if item in r.carries and r.first_content is None)
            if item in dropped:
                if n or silent < self.cfg["max_silent_tries"]:
                    self.fail("I6", f"announcement {item} dropped after {silent} silent tries, heard {n}")
                if any(item in r.carries and r.start > dropped[item] for r in self.replies.values()):
                    self.fail("I6", f"announcement {item} carried after it was dropped")
            elif n != 1:
                self.fail("I6", f"announcement {item} heard {n} times")
        # Metrics: each playback_paused ends in playback_resumed or playback_stopped, or its reply
        # ended while paused; never two in a row.
        open_: dict = {}
        ends = {r.label: [] for r in self.replies.values()}
        for t, e in self.emits:
            if e.name == "playback_paused":
                if open_.get(e.label):
                    self.fail("metrics", f"label {e.label}: playback_paused twice without an end")
                open_[e.label] = t
            elif e.name in ("playback_resumed", "playback_stopped"):
                if e.name == "playback_resumed" and not open_.get(e.label):
                    self.fail("metrics", f"label {e.label}: playback_resumed without a pause")
                open_[e.label] = None
            elif e.name == "x_floor" and e.fields.get("act") == "end":
                open_[e.label] = None
        for lab, t in open_.items():
            if t is not None:
                self.fail("metrics", f"label {lab}: playback_paused at {t:.0f} never ended")
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
            segs = self.partition(first.question, first.start, used - mine_segs) or []
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
