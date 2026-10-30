"""Floor controller: who has the floor, as a pure state machine. The contract is FLOOR-SPEC.md.

    floor = Floor(cfg)
    actions = floor.on(event)        # every event carries t_ms; no I/O, no asyncio, no clock reads

The adapter (the orchestrator) turns mic, endpointer, STT, brain, TTS and sink callbacks into the
events below, carries out the actions, and drops callbacks whose rid is not the current reply's.
Timers are events too: `set_timer(name, at_ms)` asks the adapter to send `timer(name)` at that time.

Rule numbers in comments are the spec's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, ClassVar, Optional

from .turntaking import STT_HALLUCINATIONS, fold_hums, is_backchannel, is_hesitation, normalize

DEFAULTS = {
    "pause_max_ms": 1500.0,      # rule 4: a held or paused gate plays tentatively after this
    "short_bargein_ms": 800.0,   # a blip resolves with no words after this
    "join_gap_ms": 2000.0,       # rule 3: words this soon after the last word segment join
    "resume_guard_ms": 3000.0,   # rule 2: after a no-words pause, the detector only ducks this long
    "keepalive_after_ms": 4500.0,
    "keepalive_every_ms": 9000.0,
    "announce_quiet_ms": 800.0,
    "speakers": False,           # rule 2: True ignores the detector (no echo cancellation)
    # Rewind on play, by pause length: under 1 s none, 1-3 s 500 ms, longer to the piece start.
    "rewind": [(1000.0, 0.0), (3000.0, 500.0)],
    "rewind_max_ms": 2000.0,
    "echo_lead_ms": 500.0,       # the echo and over-heard windows start this long before a segment
    "tail_ms": 1000.0,           # question tail: the piece last played within this before the start
    "heard_frac": 0.6,           # rule 7: an announcement is heard at this share of content audio
    "max_nowords_pauses": 2,     # rule 2: then the detector only ducks
    "echo_corr": 0.5,            # Words 1: an echo score at least this makes a segment echo
    "max_silent_tries": 3,       # rule 7: an item whose carrying replies played nothing this often is dropped
}

# Hums and breaths a transcript renders as a word (the orchestrator's _HUMS, after fold_hums).
HUMS = {"hm", "hmm", "mm", "mhm", "ah", "oh", "mmm", "mhmm", "hmmm", "ahh", "ohh"}


# -- the injected helpers ----------------------------------------------------------------------------

def _echo_free(tokens: list, played: list, speakers: bool) -> list:
    """Spec "Words" 1: drop runs of 3+ tokens that the played reply text has in the same order, and
    a text equal to one played piece. On speakers, a text whose tokens all were played is echo."""
    pieces = [p for p in (normalize(t).split() for t in played) if p]
    if not tokens or not pieces:
        return tokens
    if any(tokens == p for p in pieces):
        return []
    flat = [w for p in pieces for w in p]
    if speakers and set(tokens) <= set(flat):
        return []
    if len(tokens) >= 2 and _near_run(tokens, flat):
        return []                                  # a misheard echo: "shreds are cheaper"
    out, i = [], 0
    while i < len(tokens):
        run = 0
        for j in range(len(flat)):
            k = 0
            while i + k < len(tokens) and j + k < len(flat) and tokens[i + k] == flat[j + k]:
                k += 1
            run = max(run, k)
        if run >= 3:
            i += run
        else:
            out.append(tokens[i])
            i += 1
    return out


def _near(a: str, b: str) -> bool:
    """Nearly the same word: equal, or both 4+ letters and an edit distance of at most a third of
    the longer one ("shreds"/"threads", "reuse"/"reuses")."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= max(len(a), len(b)) / 3


def _near_run(tokens: list, flat: list, max_gap: int = 2) -> bool:
    """Every token nearly matches the played text, in order, skipping at most max_gap played
    words between two of them (the STT drops and mishears words of an echo)."""
    for start in range(len(flat)):
        if not _near(tokens[0], flat[start]):
            continue
        j, ok = start, True
        for t in tokens[1:]:
            hit = next((k for k in range(j + 1, min(len(flat), j + 2 + max_gap)) if _near(t, flat[k])), None)
            if hit is None:
                ok = False
                break
            j = hit
        if ok:
            return True
    return False


def make_meaningful(speakers: bool = False, hallucinations=None) -> Callable:
    """meaningful(text, played, earlier_words=False) -> the words left, "" for none (spec "Words").
    `played` is the reply text played in the segment's echo window. `earlier_words`: an earlier
    interim of the segment had words, so a hallucination-list text is kept."""
    hallucinations = STT_HALLUCINATIONS if hallucinations is None else hallucinations

    def meaningful(text: str, played=(), earlier_words: bool = False) -> str:
        tokens = fold_hums(normalize(text or "")).split()
        kept = _echo_free(tokens, list(played), speakers)
        kept = [w for w in kept if w not in HUMS and not is_hesitation(w)]
        if not kept or (" ".join(kept) in hallucinations and not earlier_words):
            return ""
        return text.strip() if kept == tokens else " ".join(kept)

    return meaningful


def default_backchannel(text: str) -> bool:
    return is_backchannel(normalize(text or ""))


# -- events ------------------------------------------------------------------------------------------

@dataclass
class SpeechStart:
    g: int                       # the endpointer's segment id; turn_end names it
    utt_id: str                  # stt.begin()
    hint: Optional[str] = None   # turn_hint() from the bench
    t_ms: float = 0.0


@dataclass
class Interim:
    utt_id: str
    text: str
    t_ms: float = 0.0
    echo: Optional[float] = None  # the adapter's echo score of the segment's audio so far (live/echo.py)


@dataclass
class TurnEnd:
    g: int
    pause_start_ms: Optional[float]
    t_ms: float = 0.0


@dataclass
class Final:
    utt_id: str
    text: str
    how: str = ""
    t_ms: float = 0.0
    echo: Optional[float] = None  # the echo score of the whole segment


@dataclass
class Detector:
    decision: str                # duck | barge_in | resume
    reason: str = ""
    t_ms: float = 0.0


@dataclass
class BrainText:
    rid: str
    delta: str
    t_ms: float = 0.0


@dataclass
class BrainDone:
    rid: str
    t_ms: float = 0.0


@dataclass
class BrainError:
    rid: str
    t_ms: float = 0.0


@dataclass
class TtsPiece:
    rid: str
    piece: str
    kind: str                    # content | keepalive
    text: str
    dur_ms: float
    t_ms: float = 0.0


@dataclass
class TtsDone:
    rid: str
    t_ms: float = 0.0


@dataclass
class Played:
    rid: str
    piece: str
    ms: float                    # how far into the piece playback has reached
    t_ms: float = 0.0
    heard_from_ms: Optional[float] = None   # when this report's audio began, if before t_ms


@dataclass
class Drained:
    rid: str
    t_ms: float = 0.0


@dataclass
class TaskResult:
    id: str
    prompt: str
    t_ms: float = 0.0


@dataclass
class Timer:
    name: str
    t_ms: float = 0.0


@dataclass
class End:
    t_ms: float = 0.0


# -- actions -----------------------------------------------------------------------------------------

@dataclass
class Interrupted:
    """Context for the next reply after a stop or error (brain.Interrupted's fields)."""
    spoken: str
    question: str


@dataclass
class StartReply:
    rid: str
    label: str
    question: str
    context: Optional[Interrupted]
    carries: list                # [{id, prompt, tries}]: queued announcements this reply tells
    kind: str = "answer"         # answer | announce
    op: ClassVar[str] = "start_reply"


@dataclass
class StopReply:
    rid: str
    op: ClassVar[str] = "stop_reply"


@dataclass
class Pause:
    rid: str
    op: ClassVar[str] = "pause"


@dataclass
class Play:
    rid: str
    rewind_ms: float
    op: ClassVar[str] = "play"


@dataclass
class Drop:
    """Drop a keepalive piece from the gate: rule 8 never buffers one across a pause."""
    rid: str
    piece: str
    op: ClassVar[str] = "drop"


@dataclass
class Duck:
    rid: str
    op: ClassVar[str] = "duck"


@dataclass
class Unduck:
    rid: str
    op: ClassVar[str] = "unduck"


@dataclass
class ResetDetector:
    op: ClassVar[str] = "reset_detector"


@dataclass
class Keepalive:
    rid: str
    op: ClassVar[str] = "keepalive"


@dataclass
class SetTimer:
    name: str
    at_ms: float
    op: ClassVar[str] = "set_timer"


@dataclass
class CancelTimer:
    name: str
    op: ClassVar[str] = "cancel_timer"


@dataclass
class SetFloor:
    assistant: bool
    op: ClassVar[str] = "floor"


@dataclass
class Emit:
    name: str
    label: Optional[str]
    fields: dict
    op: ClassVar[str] = "emit"


# -- state -------------------------------------------------------------------------------------------

@dataclass
class Seg:
    g: int
    kind: str                    # segment | blip
    start_ms: float
    utt_id: Optional[str] = None
    hint: Optional[str] = None
    status: str = "open"         # open | ended | final ("final" = resolved, for blips too)
    speech_end_ms: Optional[float] = None
    turn_end_ms: Optional[float] = None
    text: str = ""               # meaningful final
    over_heard: Optional[bool] = None
    caused: str = "none"         # none | pause | duck
    interim_seen_ms: Optional[float] = None
    interim_words: bool = False
    last_interim: str = ""
    echo: bool = False           # its audio is the reply's echo: it never holds, pauses or ducks


@dataclass
class Utterance:
    label: Optional[str] = None
    segs: list = field(default_factory=list)
    last_word_end_ms: Optional[float] = None


@dataclass
class Piece:
    kind: str
    text: str
    dur_ms: float
    played_ms: float = 0.0


@dataclass
class PlayedSpan:
    rid: str
    piece: str
    kind: str
    t_start_ms: float
    t_end_ms: float
    text: str
    open: bool = True             # still playing: a pause ends the span, the next play starts one


@dataclass
class Announcement:
    id: str
    prompt: str
    label: str
    state: str = "queued"        # queued | carried | heard | dropped
    rid: Optional[str] = None
    tries: int = 0               # failed deliveries (a join restart is none)
    silent_tries: int = 0        # failed deliveries that failed by themselves: no content played,
                                 # or the reply ended in an error


@dataclass
class Reply:
    rid: str
    label: str
    kind: str
    question: str
    context: Optional[Interrupted]
    carries: list
    segs: set
    started_ms: float
    brain: str = "running"       # running | done | failed
    tts: str = "running"
    pieces: dict = field(default_factory=dict)
    first_heard_ms: Optional[float] = None
    heard: str = "none"          # none | keepalive | content
    gate: str = "open"           # open | paused | tentative
    pause_on: set = field(default_factory=set)
    cause: Optional[str] = None  # hold | barge_in, from the first entry into the pause set
    since_ms: Optional[float] = None
    episode_paused: bool = False  # this pause episode stopped heard audio (playback_paused)
    holds: set = field(default_factory=set)   # rule 1 members: segments held before heard content
    ducked_on: set = field(default_factory=set)
    detector_ducked: bool = False
    resumed_ms: Optional[float] = None
    speech_since_resume: bool = False
    nowords_pauses: int = 0
    offset: Optional[tuple] = None
    drained: bool = True         # nothing queued or buffered at the gate
    ka_heard: bool = False       # a keepalive played with the gate open
    sound_ms: float = 0.0        # last play or content audio, for the keepalive timer
    play_ms: float = 0.0         # the last play action: a played span after a pause starts here
    ka_sent_ms: Optional[float] = None


class Floor:
    def __init__(self, cfg: Optional[dict] = None, backchannel: Optional[Callable] = None,
                 meaningful: Optional[Callable] = None) -> None:
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.speakers = bool(self.cfg["speakers"])
        self.backchannel = backchannel or default_backchannel
        self.meaningful = meaningful or make_meaningful(self.speakers)
        self.segs: dict = {}
        self.by_utt: dict = {}
        self.by_ext: dict = {}
        self.utt = Utterance()
        self.reply: Optional[Reply] = None
        self.last_context: Optional[Interrupted] = None
        self.last_heard: Optional[Interrupted] = None   # what the last ended answer played
        self.played: list = []
        self.announcements: list = []
        self.timers: dict = {}
        self.counter = 0
        self.announce_counter = 0
        self.rid_counter = 0
        self.g_counter = 0
        self.now = 0.0
        self.ended = False
        self.floor_on = False
        self.ducked = False
        self.ted_labels: set = set()     # labels with their turn_end_detected
        self.first_audio_labels: set = set()
        self.question_by_label: dict = {}
        self.out: list = []

    # -- entry -----------------------------------------------------------------------------------
    def on(self, ev) -> list:
        self.out = []
        if self.ended:
            return self.out
        self.now = float(ev.t_ms)
        getattr(self, "_on_" + type(ev).__name__)(ev)
        if not self.ended:
            self._settle()
        return self.out

    def _emit(self, name: str, label: Optional[str] = None, **fields) -> None:
        self.out.append(Emit(name, label, fields))

    def _floor(self, act: str, reason: str, **fields) -> None:
        r = self.reply
        label = fields.pop("label", r.label if r else self.utt.label)
        if r is not None:
            fields.setdefault("rid", r.rid)
        self._emit("x_floor", label, act=act, reason=reason, **fields)

    # -- events ----------------------------------------------------------------------------------
    def _on_SpeechStart(self, ev: SpeechStart) -> None:
        seg = self._new_seg("segment", utt_id=ev.utt_id, hint=ev.hint)
        self.by_utt[ev.utt_id] = seg.g
        self.by_ext[ev.g] = seg.g
        blip = self._live_blip()
        if blip is not None:
            self._absorb(blip, seg)
        self.utt.segs.append(seg.g)
        r = self.reply
        if r is None:
            return
        r.speech_since_resume = True
        if r.heard != "content":
            # Rule 1: before heard content, sound holds. A tentative play with nothing heard yet
            # stops too, so the cap restarts from this segment (bug 15).
            if r.gate == "tentative":
                self._close_gate("hold", "new_segment")
            self._hold_on(seg, "hold", "new_segment")
        elif r.gate != "open":
            self._hold_on(seg, "barge_in", "new_segment")   # rule 2: joins the set; tentative plays on

    def _on_Interim(self, ev: Interim) -> None:
        seg = self._seg_of(ev.utt_id)
        if seg is None or seg.status == "final":
            return
        seg.interim_seen_ms = self.now
        if self._is_echo(ev.echo):
            self._echo_seg(seg)
            return
        if not self._words(seg, ev.text, self._over_heard(seg, self.now)):
            return
        seg.interim_words = True
        seg.last_interim = ev.text
        r = self.reply
        if r is None:
            return
        # A held segment's own words stay part of its hold (rule 1), even after content played.
        cause = "barge_in" if r.heard == "content" and seg.g not in r.holds else "hold"
        if seg.g in r.pause_on:
            if r.gate == "tentative":
                self._close_gate(cause, "interim_words", g=seg.g)   # rule 4: words pause again at once
            return
        # Rule 2, both modes: interim words pause at once; the guard and the limit never block it.
        # It pauses even when the segment is not over heard content (I5), and does not set
        # `caused`, so it never changes whether the segment is over heard.
        self._hold_on(seg, cause, "interim_words", stop_tentative=True)
        if cause == "barge_in":
            self._emit("x_bargein", r.label, reason="interim", text=ev.text)

    def _on_TurnEnd(self, ev: TurnEnd) -> None:
        seg = self.segs.get(self.by_ext.get(ev.g))
        if seg is None:
            return
        self._emit("x_turn_end", self.utt.label, g=seg.g, pause_start_ms=ev.pause_start_ms)
        seg.turn_end_ms = self.now
        if seg.status == "open":
            seg.status = "ended"
            seg.speech_end_ms = ev.pause_start_ms if ev.pause_start_ms is not None else self.now
            seg.over_heard = self._over_heard(seg, seg.speech_end_ms)

    def _on_Final(self, ev: Final) -> None:
        seg = self._seg_of(ev.utt_id)
        if seg is None or seg.status == "final":
            return
        if seg.speech_end_ms is None:
            seg.speech_end_ms = self.now
            seg.over_heard = self._over_heard(seg, self.now)
        seg.status = "final"
        if self._is_echo(ev.echo) or (ev.echo is None and seg.echo):
            self._no_words(seg, "final_echo")       # Words 1: the reply's own audio, whatever the text
            return
        words = self._words(seg, ev.text, seg.over_heard)
        dropout = not self.meaningful(ev.text, self._window(seg), earlier_words=seg.interim_words)
        if dropout and seg.interim_words:
            # Rule 3: an STT dropout (the final has no words at all, not a backchannel). Read
            # before the no-words test, or it could never apply.
            words = self._words(seg, seg.last_interim, seg.over_heard)
        if not words:
            self._no_words(seg, "final_no_words")
            return
        seg.text = words
        r = self.reply
        if r is not None and seg.g in r.segs:
            self._join(seg, "in_question")
        elif not seg.over_heard and r is None:
            self._join(seg, "new_turn")
        elif (r is not None and r.kind == "answer" and not seg.over_heard and r.heard != "content"
              and not r.ka_heard and self.utt.last_word_end_ms is not None
              and seg.start_ms - self.utt.last_word_end_ms <= self.cfg["join_gap_ms"]):
            self._join(seg, "join_gap")
        else:
            self._interrupt(seg)

    def _on_Detector(self, ev: Detector) -> None:
        r = self.reply
        if r is None or self.speakers:
            return                                  # rule 2: on speakers the detector is only logged
        if ev.decision in ("duck", "resume"):
            r.detector_ducked = ev.decision == "duck"   # rule 10: resume never ends a pause
            self._floor("duck" if r.detector_ducked else "unduck", f"detector_{ev.decision}")
            return
        if ev.decision != "barge_in":
            return
        target = self._ducked_target(r) or self._open_seg() or self._new_blip()
        if target.echo:
            return                                  # rules 1-2: an echo segment never pauses or ducks
        if r.heard != "content":
            self._hold_on(target, "hold", f"detector_{ev.reason}", stop_tentative=True)
            return
        guard = (r.resumed_ms is not None and not r.speech_since_resume
                 and self.now - r.resumed_ms < self.cfg["resume_guard_ms"])
        if target.g in r.pause_on or guard or r.nowords_pauses >= self.cfg["max_nowords_pauses"]:
            why = "in_set" if target.g in r.pause_on else "guard" if guard else "limit"
            r.ducked_on.add(target.g)
            if target.caused == "none":
                target.caused = "duck"
            self.out.append(ResetDetector())
            self._floor("duck", f"detector_{ev.reason}_{why}", g=target.g)
            return
        self._hold_on(target, "barge_in", f"detector_{ev.reason}", stop_tentative=True)
        target.caused = "pause"

    def _on_BrainText(self, ev: BrainText) -> None:
        pass

    def _on_BrainDone(self, ev: BrainDone) -> None:
        r = self._current(ev.rid)
        if r is not None:
            r.brain = "done"
            self._maybe_end(r)

    def _on_BrainError(self, ev: BrainError) -> None:
        r = self._current(ev.rid)
        if r is None:
            return
        r.brain = "failed"
        if r.heard == "content":                    # rule 9
            self.last_context = Interrupted(self._spoken(r), r.question)
        self._end_reply("error")
        self._close_utterance()

    def _on_TtsPiece(self, ev: TtsPiece) -> None:
        r = self._current(ev.rid)
        if r is None:
            return
        if ev.kind == "keepalive" and r.gate == "paused":
            self.out.append(Drop(r.rid, ev.piece))  # rule 8: dropped, not buffered
            return
        r.pieces[ev.piece] = Piece(ev.kind, ev.text, float(ev.dur_ms))
        r.drained = False

    def _on_TtsDone(self, ev: TtsDone) -> None:
        r = self._current(ev.rid)
        if r is not None:
            r.tts = "done"
            self._maybe_end(r)

    def _on_Played(self, ev: Played) -> None:
        r = self._current(ev.rid)
        p = r.pieces.get(ev.piece) if r is not None else None
        if p is None or r.gate == "paused":
            return
        p.played_ms = max(p.played_ms, float(ev.ms))
        r.offset = (ev.piece, float(ev.ms))
        span = next((s for s in self.played if s.rid == r.rid and s.piece == ev.piece and s.open), None)
        if span is None:
            # A span is one stretch of sound: after a pause it starts again at the play, not at
            # the piece's start, so the paused gap never counts as heard.
            start = max(self.now - ev.ms, r.play_ms)
            self.played.append(PlayedSpan(r.rid, ev.piece, p.kind, start, self.now, p.text))
            self.played = [s for s in self.played if s.t_end_ms >= self.now - 60000]
        else:
            span.t_end_ms = self.now
        if p.kind == "content":
            r.heard = "content"
            r.sound_ms = self.now
            if r.first_heard_ms is None:
                r.first_heard_ms = self.now
                self._first_content(r, ev.heard_from_ms if ev.heard_from_ms is not None else self.now)
        elif r.gate == "open":
            r.ka_heard = True
            if r.heard == "none":
                r.heard = "keepalive"

    def _on_Drained(self, ev: Drained) -> None:
        r = self._current(ev.rid)
        if r is None:
            return
        r.drained = True
        self._maybe_end(r)

    def _maybe_end(self, r: Reply) -> None:
        """A reply ends when the brain and TTS are done and the gate has nothing left, in any order,
        and never while its gate is paused: the pause resolves first (no words: it plays on with
        nothing left and ends; words: it is stopped)."""
        if r.brain == "done" and r.tts == "done" and r.drained and r.gate != "paused":
            self._end_reply("drained", stop=False)
            self._close_utterance()

    def _on_TaskResult(self, ev: TaskResult) -> None:
        self.announce_counter += 1
        self.announcements.append(Announcement(ev.id, ev.prompt, f"a{self.announce_counter}"))

    def _on_Timer(self, ev: Timer) -> None:
        due = self.timers.get(ev.name)
        if due is None or self.now < due:
            return                                  # cancelled or moved
        del self.timers[ev.name]
        if ev.name.startswith("blip"):
            seg = self.segs.get(int(ev.name[4:]))
            if seg is not None and seg.status != "final":
                seg.status = "final"
                self._no_words(seg, "blip_no_words")
        elif ev.name == "keepalive":
            self._keepalive_due()
        elif ev.name == "announce":
            self._announce()
        # "cap" needs nothing here: _settle re-reads the gate.

    def _on_End(self, ev: End) -> None:
        if self.reply is not None:
            self._end_reply("end")
            self._close_utterance()
        for name in list(self.timers):
            self._cancel_timer(name)
        if self.floor_on:
            self.floor_on = False
            self.out.append(SetFloor(False))
        self.ended = True

    # -- segments and words ----------------------------------------------------------------------
    def _new_seg(self, kind: str, **kw) -> Seg:
        self.g_counter += 1
        seg = Seg(self.g_counter, kind, self.now, **kw)
        self.segs[seg.g] = seg
        return seg

    def _new_blip(self) -> Seg:
        blip = self._new_seg("blip")
        self._set_timer(f"blip{blip.g}", self.now + self.cfg["short_bargein_ms"])
        return blip

    def _seg_of(self, utt_id: str) -> Optional[Seg]:
        return self.segs.get(self.by_utt.get(utt_id))

    def _live_blip(self) -> Optional[Seg]:
        live = [s for s in self.segs.values() if s.kind == "blip" and s.status != "final"]
        return live[-1] if live else None

    def _live_blips(self) -> list:
        return [s.g for s in self.segs.values() if s.kind == "blip" and s.status != "final"]

    def _open_seg(self) -> Optional[Seg]:
        segs = [s for s in self.segs.values() if s.kind == "segment" and s.status == "open"]
        return segs[-1] if segs else None

    def _ducked_target(self, r: Reply) -> Optional[Seg]:
        live = [g for g in sorted(r.ducked_on) if self.segs[g].status != "final"]
        return self.segs[live[-1]] if live else None

    def _absorb(self, blip: Seg, seg: Seg) -> None:
        """The segment takes the blip's place everywhere: its pause, ducked_on and question group."""
        blip.status = "final"
        self._cancel_timer(f"blip{blip.g}")
        seg.caused = blip.caused
        r = self.reply
        if r is None:
            return
        for s in (r.pause_on, r.ducked_on, r.segs, r.holds):
            if blip.g in s:
                s.discard(blip.g)
                s.add(seg.g)

    def _window(self, seg: Seg) -> list:
        start = seg.start_ms - self.cfg["echo_lead_ms"]
        return [s.text for s in self.played if s.t_end_ms >= start]

    def _words(self, seg: Seg, text: str, over_heard: bool) -> str:
        """meaningful words, or "" when there are none or they are not an interruption."""
        words = self.meaningful(text, self._window(seg), earlier_words=seg.interim_words)
        if words and over_heard and self.backchannel(words):
            return ""
        return words

    def _over_heard(self, seg: Seg, end: float) -> bool:
        if self._question_tail(seg, end):
            return False
        if seg.caused != "none":
            return True
        start = seg.start_ms - self.cfg["echo_lead_ms"]
        return any(s.kind == "content" and s.t_end_ms >= start and s.t_start_ms <= end for s in self.played)

    def _question_tail(self, seg: Seg, end: float) -> bool:
        content = [s for s in self.played if s.kind == "content"]
        at = [s for s in content if s.t_start_ms <= seg.start_ms <= s.t_end_ms]
        recent = [s for s in content if 0 <= seg.start_ms - s.t_end_ms <= self.cfg["tail_ms"]]
        tail = at[-1] if at else (max(recent, key=lambda s: s.t_end_ms) if recent else None)
        if tail is None or not tail.text.rstrip().endswith("?"):
            return False
        return not any(s.piece != tail.piece and tail.t_start_ms < s.t_start_ms <= end for s in content)

    # -- the gate --------------------------------------------------------------------------------
    def _hold_on(self, seg: Seg, cause: str, reason: str, stop_tentative: bool = False) -> None:
        """Add seg to the pause set; the first entry sets cause and since. Closes an open gate, and
        a tentative one when `stop_tentative` (a detector or interim pause, not a new segment)."""
        r = self.reply
        if seg.g in r.pause_on:
            return
        if not r.pause_on:
            r.cause, r.since_ms, r.episode_paused = cause, self.now, False
        r.pause_on.add(seg.g)
        if cause == "hold":
            r.holds.add(seg.g)
        if r.gate == "open" or (r.gate == "tentative" and stop_tentative):
            self._close_gate(cause, reason, g=seg.g)
        else:
            self._floor("hold" if cause == "hold" else "pause_add", reason, g=seg.g)

    def _close_gate(self, trigger: str, reason: str, g: Optional[int] = None) -> None:
        r = self.reply
        if r.gate == "tentative":
            r.since_ms = self.now                   # audio stops again now: the rewind counts from here
        r.gate = "paused"
        self.out.append(Pause(r.rid))
        for s in self.played:
            if s.rid == r.rid:
                s.open = False
        for name, p in r.pieces.items():
            if p.kind == "keepalive" and p.played_ms < p.dur_ms:
                p.dur_ms = p.played_ms              # rule 8: a paused keepalive is dropped
                self.out.append(Drop(r.rid, name))
        self._floor("pause", reason, g=g, trigger=trigger)
        # Rule 1: a hold is never a barge-in, even when its tentative play reached content: a
        # pause by a held segment gets no metrics, no guard and no limit count. A barge-in on a new
        # blip or segment over heard content counts, whatever else is still held. One episode emits
        # one pause: a stop after its tentative play is not a second one.
        if trigger == "barge_in" and r.heard == "content" and not r.episode_paused:
            r.episode_paused = True
            self._emit("barge_in_detected", r.label, reason=reason)
            self._emit("playback_paused", r.label, reason=reason)

    def _is_echo(self, score: Optional[float]) -> bool:
        return score is not None and score >= self.cfg["echo_corr"]

    def _echo_seg(self, seg: Seg) -> None:
        """Rules 1-2: the segment is the reply's echo. It leaves the pause set and ducked_on and
        holds nothing from now on; its final is no words."""
        if not seg.echo:
            seg.echo = True
            r = self.reply
            if r is not None:
                r.pause_on.discard(seg.g)
                r.ducked_on.discard(seg.g)
            self._floor("echo", "echo_score", g=seg.g)

    def _quiet(self, seg: Seg) -> bool:
        """Rule 4: an interim with no words, or a turn_end with no interim words. A blip has
        neither, so it waits out short_bargein_ms and leaves the set."""
        if seg.interim_words:
            return False
        return seg.interim_seen_ms is not None or seg.status != "open"

    def _gate(self, r: Reply) -> None:
        if r.gate == "open":
            self._cancel_timer("cap")
            return
        if not r.pause_on:
            self._free_gate(r)
            return
        if r.gate == "tentative":
            self._cancel_timer("cap")
            return
        cap = max(self.segs[g].start_ms for g in r.pause_on) + self.cfg["pause_max_ms"]
        if self.now < cap:
            self._set_timer("cap", cap)
        elif all(self._quiet(self.segs[g]) for g in r.pause_on):
            self._cancel_timer("cap")
            r.gate = "tentative"
            r.play_ms = self.now
            self.out.append(Play(r.rid, self._rewind(r)))
            self._floor("play", "cap_tentative", on=sorted(r.pause_on))
            r.sound_ms = self.now
        else:
            self._cancel_timer("cap")               # waits for an interim or turn_end

    def _free_gate(self, r: Reply) -> None:
        """The pause set emptied: open the gate, and count a pause that ended with no words."""
        was = r.gate
        # A member that resolved with words always ended the reply (join changes the question,
        # or interrupt), so an episode still open here ended with no words.
        if r.episode_paused:
            r.nowords_pauses += 1
            r.resumed_ms = self.now
            r.speech_since_resume = False
            self._emit("playback_resumed", r.label, nowords_pauses=r.nowords_pauses)
        rewind = self._rewind(r)
        r.gate, r.cause, r.since_ms = "open", None, None
        self._cancel_timer("cap")
        if was == "paused":
            r.play_ms = self.now
            self.out.append(Play(r.rid, rewind))
            r.sound_ms = self.now                   # rule 8: the keepalive timer restarts on play
        self._floor("play" if was == "paused" else "open", "pause_set_empty")

    def _rewind(self, r: Reply) -> float:
        if r.offset is None or r.since_ms is None:
            return 0.0
        length, into = self.now - r.since_ms, r.offset[1]
        for under, ms in self.cfg["rewind"]:
            if length < under:
                return float(min(ms, into))
        return float(min(into, self.cfg["rewind_max_ms"]))

    def _no_words(self, seg: Seg, reason: str) -> None:
        """Rule 3, no words: the segment or blip leaves the utterance, the pause set and ducked_on."""
        if seg.g in self.utt.segs:
            self.utt.segs.remove(seg.g)
        r = self.reply
        if r is not None:
            r.pause_on.discard(seg.g)
            r.ducked_on.discard(seg.g)
        self._floor("no_words", reason, g=seg.g)

    # -- join, interrupt, start, end -------------------------------------------------------------
    def _question(self) -> str:
        return " ".join(self.segs[g].text for g in sorted(self.utt.segs)
                        if self.segs[g].status == "final" and self.segs[g].text)

    def _join(self, seg: Seg, reason: str) -> None:
        u = self.utt
        if seg.g not in u.segs:
            u.segs.append(seg.g)
        newest = max(g for g in u.segs if self.segs[g].status == "final" and self.segs[g].text)
        u.last_word_end_ms = self.segs[newest].speech_end_ms
        if u.label is None:
            first = self.segs[min(g for g in u.segs if self.segs[g].text)]
            u.label = self._new_label(first.hint)
        r = self.reply
        if r is not None:
            r.pause_on.discard(seg.g)
            r.ducked_on.discard(seg.g)
        self._floor("join", reason, g=seg.g, label=u.label)
        question = self._question()
        if r is None:
            context, self.last_context = self.last_context, None
            self._start(question, context)
        elif question != r.question:
            # Join restarts under the same label (rule 6: only when the words change). What was
            # heard goes along as context.
            context = Interrupted(self._spoken(r), r.question) if r.heard == "content" else r.context
            self._end_reply("restart")
            self._start(question, context)

    def _interrupt(self, seg: Seg) -> None:
        r = self.reply
        if seg.g in self.utt.segs:
            self.utt.segs.remove(seg.g)             # g starts the next utterance, not this one
        if r is not None:
            self.last_context = Interrupted(self._spoken(r), r.question)
            self._end_reply("interrupt")
        elif self.last_context is None and self.last_heard is not None:
            self.last_context = self.last_heard     # over heard content of an ended reply (ordering 22)
        self._close_utterance()
        self._floor("interrupt", "words", g=seg.g, label=None)
        self._join(seg, "interrupt")

    def _new_label(self, hint) -> str:
        if hint is not None:
            if str(hint).isdigit():
                self.counter = max(self.counter, int(hint))
            return str(hint)
        self.counter += 1
        return str(self.counter)

    def _start(self, question: str, context: Optional[Interrupted]) -> None:
        u = self.utt
        carries = [a for a in self.announcements if a.state == "queued"]
        self._new_reply(u.label, "answer", question, context, carries, set(u.segs) | set(self._live_blips()))
        if self.question_by_label.get(u.label) != question:
            self.question_by_label[u.label] = question
            self._emit("final_transcript", u.label, text=question)
        r = self.reply
        # Rule 5: the gate starts held on every unresolved segment and live blip.
        for g in sorted(r.segs):
            if self.segs[g].status != "final":
                self._hold_on(self.segs[g], "hold", "start_unresolved")

    def _new_reply(self, label, kind, question, context, carries, segs) -> None:
        self.rid_counter += 1
        r = Reply(f"r{self.rid_counter}", label, kind, question, context, [a.id for a in carries], segs,
                  self.now, sound_ms=self.now)
        for a in carries:
            a.state, a.rid = "carried", r.rid
        self.reply = r
        self.out.append(StartReply(r.rid, label, question, context,
                                   [{"id": a.id, "prompt": a.prompt, "tries": a.tries} for a in carries], kind))
        self._floor("start", kind, label=label, question=question)

    def _end_reply(self, reason: str, stop: bool = True) -> None:
        r = self.reply
        if stop:
            self.out.append(StopReply(r.rid))
            if r.heard != "none":
                self._emit("playback_stopped", r.label, reason=reason)
        if self.ducked:
            self.ducked = False
            self.out.append(Unduck(r.rid))
        if reason == "restart":
            for a in self.announcements:            # the restarted reply carries them on, same tries
                if a.state == "carried" and a.rid == r.rid:
                    a.state = "queued"
        else:
            self._settle_announcements(r)
        self._floor("end", reason, spoken=self._spoken(r))   # the one spoken text (transcript, logs)
        self.last_heard = Interrupted(self._spoken(r), r.question) if r.kind == "answer" else None
        self.reply = None
        self._cancel_timer("cap")
        self._cancel_timer("keepalive")

    def _close_utterance(self) -> None:
        """The utterance ends (its reply ended, or words interrupted it): its label gets its
        turn_end_detected if no content played, and every unresolved segment carries over into the
        next utterance. Callers: the reply-end handlers and _interrupt; never a join restart."""
        u = self.utt
        if u.label is not None and u.label not in self.ted_labels:
            self._turn_end_detected(u)
        self.utt = Utterance(segs=[g for g in u.segs if self.segs[g].status != "final"])

    def _turn_end_detected(self, u: Utterance) -> None:
        words = [g for g in u.segs if self.segs[g].text]
        if not words:
            return
        self.ted_labels.add(u.label)
        self._emit("turn_end_detected", u.label, t_ms=self.segs[max(words)].turn_end_ms)

    def _first_content(self, r: Reply, t_ms: float) -> None:
        if r.label not in self.first_audio_labels:
            self.first_audio_labels.add(r.label)
            self._emit("first_reply_audio", r.label, filler=False, t_ms=t_ms)
        if r.kind == "answer" and r.label not in self.ted_labels and r.label == self.utt.label:
            self._turn_end_detected(self.utt)

    def _spoken(self, r: Reply) -> str:
        out = []
        for p in r.pieces.values():
            if p.kind != "content" or p.played_ms <= 0:
                continue
            words = p.text.split()
            n = len(words) if p.played_ms >= p.dur_ms else round(len(words) * p.played_ms / p.dur_ms)
            out.extend(words[:n])
        return " ".join(out)

    # -- announcements (rule 7) and keepalive (rule 8) -------------------------------------------
    def _settle_announcements(self, r: Reply) -> None:
        content = [p for p in r.pieces.values() if p.kind == "content"]
        total = sum(p.dur_ms for p in content)
        frac = sum(min(p.played_ms, p.dur_ms) for p in content) / total if total else 0.0
        heard = r.brain == "done" and r.tts == "done" and frac >= self.cfg["heard_frac"]
        for a in self.announcements:
            if a.state != "carried" or a.rid != r.rid:
                continue
            if heard:
                a.state = "heard"
                self._floor("announce_heard", f"played {frac:.2f}", id=a.id, label=a.label)
            else:
                a.state, a.tries = "queued", a.tries + 1
                a.silent_tries += r.first_heard_ms is None or r.brain == "failed"
                if a.silent_tries >= self.cfg["max_silent_tries"]:
                    a.state = "dropped"
                    self._emit("x_announce_dropped", a.label, id=a.id, prompt=a.prompt, tries=a.tries)
                else:
                    self._floor("announce_requeued", f"played {frac:.2f}", id=a.id, label=a.label)

    def _floor_free(self) -> bool:
        return self.reply is None and all(s.status == "final" for s in self.segs.values())

    def _announce(self) -> None:
        queued = [a for a in self.announcements if a.state == "queued"]
        if not queued or not self._floor_free():
            return
        a = queued[0]
        self._new_reply(a.label, "announce", a.prompt, None, [a], set())

    def _keepalive_due(self) -> None:
        r = self.reply
        if r is None or r.brain != "running" or r.gate != "open":
            return
        due = self._keepalive_at(r)
        if self.now >= due:
            r.ka_sent_ms = self.now
            self.out.append(Keepalive(r.rid))
            self._floor("keepalive", "silent")

    def _keepalive_at(self, r: Reply) -> float:
        due = r.sound_ms + self.cfg["keepalive_after_ms"]
        if r.ka_sent_ms is not None and r.ka_sent_ms >= r.sound_ms:
            due = r.ka_sent_ms + self.cfg["keepalive_every_ms"]
        return due

    # -- after every event -----------------------------------------------------------------------
    def _settle(self) -> None:
        r = self.reply
        if r is not None:
            self._gate(r)
            self._maybe_end(r)                       # a pause that resolved can end a drained reply
            r = self.reply
        if r is not None:
            if r.brain == "running" and r.gate == "open":
                # Move the timer only earlier (content after a keepalive restarts the 4.5 s); a
                # later due is re-read when it fires, so content audio sets no timer per frame.
                due = max(self.now, self._keepalive_at(r))
                if "keepalive" not in self.timers or due < self.timers["keepalive"]:
                    self._set_timer("keepalive", due)
            else:
                self._cancel_timer("keepalive")
        if self._floor_free() and any(a.state == "queued" for a in self.announcements):
            if "announce" not in self.timers:
                self._set_timer("announce", self.now + self.cfg["announce_quiet_ms"])
        else:
            self._cancel_timer("announce")
        on = r is not None and (r.heard == "content" or (r.gate != "paused" and not r.drained))
        if on != self.floor_on:
            self.floor_on = on
            self.out.append(SetFloor(on))
        duck = r is not None and not self.speakers and bool(r.ducked_on or r.detector_ducked)
        if duck != self.ducked:
            self.ducked = duck
            self.out.append(Duck(r.rid) if duck else Unduck(r.rid))

    def _current(self, rid: str) -> Optional[Reply]:
        r = self.reply
        return r if r is not None and r.rid == rid else None

    def _set_timer(self, name: str, at: float) -> None:
        if self.timers.get(name) != at:
            self.timers[name] = at
            self.out.append(SetTimer(name, at))

    def _cancel_timer(self, name: str) -> None:
        if self.timers.pop(name, None) is not None:
            self.out.append(CancelTimer(name))
