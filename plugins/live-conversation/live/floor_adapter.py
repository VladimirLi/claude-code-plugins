"""The orchestrator on the floor controller (FLOOR-SPEC.md, plan step 4): `orchestrator.floor: true`.

    orch = Orchestrator(audio, log, Components(...), cfg)   # returns a FloorAdapter under the flag
    await orch.start(); await orch.run(); orch.transcript(); await orch.close()

The adapter owns no floor decision. It turns mic frames, endpointer decisions, STT interims and
finals, BargeIn decisions, brain text, synthesized pieces and sink positions into floor events,
and carries out the actions live/floor.py returns:

- mic loop: stt.feed, bargein.feed with the controller's `floor` flag (every decision is logged as
  x_bargein and sent as a `detector` event), endpointer decisions (speech_start begins an STT
  utterance; turn_end asks for that utterance's final, which comes back as a `final` event in any
  order), then the sink's position (`played`, `drained`) and the controller's timers, on clock.ms.
- replies: `start_reply` runs the brain and a TTS pipeline; each synthesized sentence is a piece
  (`tts_piece`), kept whole so a pause can play it again from where it stopped. While the gate is
  paused pieces are buffered, not written. `play` writes again from the pause point less the
  controller's rewind, moved back to a word start; `duck` lowers the sink's gain.
- stale callbacks: anything from a reply that is not the current one is dropped before it reaches
  the controller or the sink.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import floor as F
from .brain import Interrupted
from .events import SR, EventLog
from .tts import Chunker

KEEPALIVE = ["Still on it.", "Hang on.", "Almost there.", "Still going.", "Bear with me.", "Nearly done."]

# announce(): sent before the prompt when an earlier try was cut before the user heard any of it
ANNOUNCE_RETRY_NOTE = ('[Not heard yet: the message below reached you before, but the user started talking '
                       'before any of your answer to it was spoken, so they have not heard it. It is the same '
                       'single message, not a new or second one. Tell them now as if for the first time; '
                       'don\'t say "again", "still", "same" or "came back".]')

# a user-turn reply while an announcement was cut before the user heard it: deliver it in this reply
ANNOUNCE_CARRY_NOTE = ('[Not told yet: the user started talking before you said anything about the message '
                       'below, so they have not heard it. It is the same single message as before, not a new '
                       'one. In this reply, answer what the user says now and tell them that outcome briefly, '
                       'once, as news; don\'t say "again", "still", "same" or "came back".]')

# orchestrator settings the controller takes over (FLOOR-SPEC.md "Rules")
_FLOOR_KEYS = ("keepalive_after_ms", "keepalive_every_ms", "announce_quiet_ms", "short_bargein_ms")


@dataclass
class _Piece:
    name: str
    kind: str                    # content | keepalive
    text: str
    pcm: np.ndarray


@dataclass
class _Reply:
    rid: str
    label: Optional[str]
    kind: str
    question: str
    pieces: list = field(default_factory=list)     # _Piece, in arrival order
    written: int = 0             # pieces[:written] are in the sink (or were played)
    paused: bool = False
    resume: tuple = (0, 0)       # (piece index, sample) where playback goes on after a pause
    drained_sent: bool = True
    text_generated: str = ""
    interrupted: bool = False
    spoken: Optional[str] = None                    # the controller's, when the reply ended
    tasks: list = field(default_factory=list)


class FloorAdapter:
    def __init__(self, audio, log: EventLog, c, cfg: Optional[dict] = None) -> None:
        self.audio = audio
        self.log = log
        self.c = c
        self.cfg = cfg or {}
        ocfg = self.cfg.get("orchestrator") or {}
        fcfg = dict(self.cfg.get("floor") or {})
        for k in _FLOOR_KEYS:
            if k in ocfg:
                fcfg.setdefault(k, ocfg[k])
        self.duck_db = float(fcfg.pop("duck_db", -12.0))      # the sink's gain while ducked
        self.warm_tts = bool(ocfg.get("warm_tts", True))
        self.floor = F.Floor(fcfg)
        self.clock = log.clock
        self.sink = audio.sink
        self.sink.log_first_audio = False          # the controller emits first_reply_audio
        self.floor_on = False
        self.heard_to = 0                          # sink samples already reported as played
        self.timers: dict = {}
        self.g = 0
        self.utt_of: dict = {}                     # endpointer segment -> STT utterance id
        self.reply: Optional[_Reply] = None
        self.records: list = []                    # every reply, for transcript()
        self.user_turns: dict = {}
        self.tasks: set = set()
        self.ka_used = 0
        self.task_n = 0
        try:
            self.c.stt.on_interim_utt = self._on_interim
            self.c.stt.on_event = lambda name, fields: self.log.emit(name, None, **fields)
        except Exception:
            pass
        try:
            audio.reply_active = lambda: self.floor.reply is not None   # the bench waits on it
        except Exception:
            pass

    # -- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> None:
        async def safe(coro, name):
            try:
                await coro
            except Exception as e:  # warm-up failures must not kill the session
                self.log.emit("x_warmup_failed", None, t_ms=0.0, what=name, error=str(e)[:200])

        async def tts():
            await self.c.tts.start()
            for line in KEEPALIVE:                 # cached by the TTS: a keepalive is ready at once
                async for _ in self.c.tts.synthesize(line):
                    pass
        await asyncio.gather(safe(self.c.brain.start(), "brain"), safe(self.c.stt.start(), "stt"),
                             safe(tts(), "tts"))

    async def close(self) -> None:
        if self.reply is not None or self.floor.reply is not None:
            self._on(F.End())                      # the session ended mid-reply (wall cap, Ctrl-C)
        for t in list(self.tasks):
            t.cancel()
        await self.c.brain.close()
        await self.c.stt.close()
        await self.c.tts.close()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self.tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            e = t.exception()
            self.log.emit("x_task_error", None, error=f"{type(e).__name__}: {e}"[:300])

    # -- events in -------------------------------------------------------------------------------
    def _on(self, ev) -> None:
        """Send one event to the controller and carry out its actions. Callbacks of a reply that
        is not the current one never get here."""
        rid = getattr(ev, "rid", None)
        if rid is not None and (self.reply is None or self.reply.rid != rid):
            return
        ev.t_ms = self.clock.ms
        try:
            acts = self.floor.on(ev)
            for a in acts:
                self._do(a)
        except Exception as e:
            # a controller or adapter bug: logged here, since STT callbacks swallow exceptions
            self.log.emit("x_task_error", None, error=f"{type(e).__name__}: {e}"[:300], on=type(ev).__name__)
            raise

    async def run(self) -> None:
        async for frame in self.audio.frames():
            t = self.clock.ms
            self._sink_events()          # first: this frame's reply audio was heard before anything acts on it
            self.c.stt.feed(frame)
            bd = self.c.bargein.feed(frame, t, self.floor_on)
            if bd is not None:
                last = getattr(self.c.bargein, "last", None) or {}
                self.log.emit("x_bargein", None, decision=bd, reason=last.get("reason"),
                              onset_ms=last.get("onset_ms"), speech_ms=last.get("speech_ms"),
                              assistant_speaking=self.floor_on)
                self._on(F.Detector(bd, last.get("reason") or ""))
            for d in self.c.endpointer.feed(frame, t):
                if d.kind == "speech_start":
                    self.g += 1
                    utt = self.c.stt.begin()
                    self.utt_of[self.g] = utt
                    hint = self.audio.turn_hint()
                    self._on(F.SpeechStart(self.g, str(utt), None if hint is None else str(hint)))
                elif d.kind == "turn_end":
                    self._log_endpoint()
                    self._on(F.TurnEnd(self.g, d.pause_start_ms))
                    self._warm_tts()
                    self._spawn(self._final(self.utt_of.get(self.g)))
            for name, at in sorted(self.timers.items(), key=lambda kv: kv[1]):
                if at <= self.clock.ms and self.timers.get(name) == at:
                    del self.timers[name]
                    self._on(F.Timer(name))
        await asyncio.sleep(0)

    def _log_endpoint(self) -> None:
        """x_endpoint, as the old path logs it: why the endpointer ended the turn (live-review)."""
        tr = getattr(self.c.endpointer, "trace", None)
        if tr and isinstance(tr[-1], dict) and "pause_start_ms" in tr[-1]:
            x = tr[-1]
            self.log.emit("x_endpoint", None, pause_start_ms=x.get("pause_start_ms"), cls=x.get("class"),
                          text_ms=x.get("text_ms"), smart_turn=x.get("smart_turn"))

    def _warm_tts(self) -> None:
        """Turn end: get the TTS model hot while the brain thinks (tts.warm), unless a reply is
        making or playing audio (then it is hot, and a warm-up would queue in front)."""
        warm = getattr(self.c.tts, "warm", None)
        if warm is None or not self.warm_tts or self.reply is not None or self.sink.is_playing():
            return
        self._spawn(warm())

    def _on_interim(self, utt: int, text: str) -> None:
        try:
            self.c.endpointer.on_transcript(text, False)
        except Exception:
            pass
        self._on(F.Interim(str(utt), text))

    async def _final(self, utt: Optional[int]) -> None:
        if utt is None:
            return
        try:
            text, how = await self.c.stt.final_for(utt)
        except Exception as e:
            self.log.emit("x_stt_failed", None, error=str(e)[:200])
            text, how = "", "failed"
        try:
            self.c.endpointer.on_transcript(text, True)
        except Exception:
            pass
        self._on(F.Final(str(utt), text, how))

    def _sink_events(self) -> None:
        """After each frame: what of the current reply was heard (played) and whether it is out."""
        r = self.reply
        if r is None:
            return
        now = self.clock.samples
        if not r.paused:
            # every piece this frame reached, in order (one can end mid-frame and the next begin)
            heard = [(a, b, tag) for a, b, tag in self.sink.tags if a < now and b > self.heard_to]
            for a, b, (rid, name, off) in heard:
                if rid != r.rid or self.reply is not r:
                    continue
                ms = (off + min(b, now) - a) * 1000.0 / SR
                # when this stretch was first heard: first_reply_audio is the write's start, not
                # the end of the frame that reported it
                start = max(a, self.heard_to) * 1000.0 / SR
                self._on(F.Played(rid, name, ms, heard_from_ms=start))
        self.heard_to = now
        if self.reply is not r:
            return
        left = (r.resume[0] < len(r.pieces)) if r.paused else \
            (self.sink.queued_end > now or r.written < len(r.pieces))
        if not left and not r.drained_sent:
            r.drained_sent = True
            self._on(F.Drained(r.rid))

    def announce(self, prompt: str) -> None:
        """A task result: the controller tells it when the floor is free (rule 7)."""
        self.task_n += 1
        self._on(F.TaskResult(f"t{self.task_n}", prompt))

    # -- actions out -----------------------------------------------------------------------------
    def _turn(self, label: Optional[str]):
        return int(label) if label is not None and label.isdigit() else None

    def _do(self, a) -> None:
        r = self.reply
        if isinstance(a, F.StartReply):
            self._start(a)
        elif isinstance(a, F.StopReply):
            self._stop(a.rid)
        elif isinstance(a, F.Pause) and r is not None and r.rid == a.rid:
            self._pause(r)
        elif isinstance(a, F.Play) and r is not None and r.rid == a.rid:
            self._play(r, a.rewind_ms)
        elif isinstance(a, F.Drop) and r is not None and r.rid == a.rid:
            self._drop(r, a.piece)
        elif isinstance(a, F.Keepalive) and r is not None and r.rid == a.rid:
            r.tasks.append(self._spawn(self._keepalive(r)))
        elif isinstance(a, F.ResetDetector):
            reset = getattr(self.c.bargein, "reset", None)
            if reset is not None:
                reset()
        elif isinstance(a, F.SetFloor):
            self.floor_on = a.assistant
        elif isinstance(a, F.SetTimer):
            self.timers[a.name] = a.at_ms
        elif isinstance(a, F.CancelTimer):
            self.timers.pop(a.name, None)
        elif isinstance(a, F.Emit):
            self._emit(a)
        elif isinstance(a, F.Duck):
            self.sink.gain_db = self.duck_db
        elif isinstance(a, F.Unduck):
            self.sink.gain_db = 0.0

    def _emit(self, a) -> None:
        fields = dict(a.fields)
        turn = self._turn(a.label)
        if a.label is not None and turn is None:
            fields.setdefault("label", a.label)
        t_ms = fields.pop("t_ms", None)
        self.log.emit(a.name, turn, t_ms=t_ms, **fields)
        if a.name == "x_floor" and fields.get("act") == "end" and self.reply is not None \
                and self.reply.rid == fields.get("rid"):
            self._ended(self.reply, fields.get("reason"), fields.get("spoken", ""))

    def _start(self, a) -> None:
        r = _Reply(a.rid, a.label, a.kind, a.question)
        self.reply = r
        self.records.append(r)
        if a.kind == "answer":
            self.user_turns[self._turn(a.label) if self._turn(a.label) is not None else a.label] = a.question
            prompt = a.question
            if a.carries:
                prompt = " ".join([ANNOUNCE_CARRY_NOTE] + [c["prompt"] for c in a.carries]
                                  + ["[What the user says now:]", prompt])
        else:
            tries = a.carries[0]["tries"] if a.carries else 0
            prompt = a.question if tries == 0 else ANNOUNCE_RETRY_NOTE + " " + a.question
            self.log.emit("x_announce", None, text=a.question[:200], attempt=tries + 1)
        ctx = None if a.context is None else Interrupted(a.context.spoken, a.context.question)
        r.tasks.append(self._spawn(self._pipeline(r, prompt, ctx)))

    def _live(self, r: _Reply) -> bool:
        return self.reply is r

    async def _pipeline(self, r: _Reply, prompt: str, ctx: Optional[Interrupted]) -> None:
        """Brain text -> sentences -> one synthesized piece each, in order."""
        sentences: asyncio.Queue = asyncio.Queue()
        synth = self._spawn(self._synth(r, sentences))
        r.tasks.append(synth)
        chunker = Chunker()
        stream = self.c.brain.respond(prompt, ctx).__aiter__()
        first = True
        while True:
            try:
                delta = await stream.__anext__()
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:                 # the brain failed; a controller bug is not this
                self.log.emit("x_brain_error", self._turn(r.label), error=str(e)[:200])
                synth.cancel()
                self._on(F.BrainError(r.rid))
                return
            if not self._live(r):
                return
            if first:
                first = False
                self.log.emit("first_reply_text", self._turn(r.label), text=delta)
                timing = getattr(self.c.brain, "last_timing", None)
                if timing:
                    self.log.emit("x_brain", self._turn(r.label), **timing)
            r.text_generated += delta
            self._on(F.BrainText(r.rid, delta))
            for s in chunker.push(delta):
                sentences.put_nowait(s)
        for s in chunker.flush():
            sentences.put_nowait(s)
        self._on(F.BrainDone(r.rid))
        sentences.put_nowait(None)
        if await synth:
            self._on(F.TtsDone(r.rid))

    async def _synth(self, r: _Reply, sentences: asyncio.Queue) -> bool:
        """True when every sentence was synthesized. A TTS failure ends the reply as a brain
        error would: nothing else would ever end it."""
        n = 0
        while True:
            text = await sentences.get()
            if text is None:
                return True
            try:
                pcm = await self._synthesize(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.emit("x_tts_error", self._turn(r.label), error=str(e)[:200], text=text[:80])
                self._on(F.BrainError(r.rid))
                return False
            if not self._live(r):
                return False
            if len(pcm):
                self._piece(r, _Piece(f"p{n}", "content", text, pcm))
                n += 1

    async def _synthesize(self, text: str) -> np.ndarray:
        parts = [p async for p in self.c.tts.synthesize(text)]
        return np.concatenate(parts).astype(np.int16) if parts else np.zeros(0, np.int16)

    async def _keepalive(self, r: _Reply) -> None:
        line = KEEPALIVE[self.ka_used % len(KEEPALIVE)]
        self.ka_used += 1
        content = sum(p.kind == "content" for p in r.pieces)
        pcm = await self._synthesize(line)
        if sum(p.kind == "content" for p in r.pieces) != content:
            return                     # the TTS queued it behind a sentence: content came first
        if self._live(r) and len(pcm):
            self.log.emit("x_keepalive", self._turn(r.label), text=line)
            self._piece(r, _Piece(f"k{self.ka_used}", "keepalive", line, pcm))

    def _piece(self, r: _Reply, p: _Piece) -> None:
        r.pieces.append(p)
        self._on(F.TtsPiece(r.rid, p.name, p.kind, p.text, len(p.pcm) * 1000.0 / SR))
        if self._live(r) and not r.paused:
            self._write(r, r.written, 0)

    # -- the gate --------------------------------------------------------------------------------
    def _write(self, r: _Reply, idx: int, offset: int) -> None:
        """Write pieces[idx:] to the sink, the first from `offset` samples into it."""
        for i in range(idx, len(r.pieces)):
            p = r.pieces[i]
            pcm = p.pcm[offset:]
            if len(pcm):
                self.sink.write(pcm, turn=self._turn(r.label), tag=(r.rid, p.name, offset))
                r.drained_sent = False
            offset = 0
        r.written = len(r.pieces)

    def _pause(self, r: _Reply) -> None:
        if r.paused:
            return
        hit = self.sink.tag_at(self.clock.samples)
        names = [p.name for p in r.pieces]
        if hit is not None and hit[0][0] == r.rid and hit[0][1] in names:
            (_, name, off), into = hit
            r.resume = (names.index(name), off + into)
        else:
            r.resume = (r.written, 0)
        self.sink.pause()
        r.paused = True
        r.written = r.resume[0]

    def _play(self, r: _Reply, rewind_ms: float) -> None:
        if not r.paused:
            return
        idx, smp = r.resume
        smp = max(0, smp - self._rewind_samples(r, idx, smp, rewind_ms))
        self.sink.resume()
        r.paused = False
        self._write(r, idx, smp)
        reset = getattr(self.c.bargein, "reset", None)
        if reset is not None:
            reset()

    def _rewind_samples(self, r: _Reply, idx: int, smp: int, rewind_ms: float) -> int:
        """How far back to start: `rewind_ms` before the pause point, moved back to a word start.
        The TTS gives no word timings, so words are spread evenly over the piece (as the old
        _spoken_so_far did)."""
        if rewind_ms <= 0 or idx >= len(r.pieces):
            return 0
        p = r.pieces[idx]
        target = max(0, smp - int(rewind_ms * SR / 1000))
        n = max(1, len(p.text.split()))
        word = len(p.pcm) / n
        start = int(int(target / word) * word)
        return smp - start

    def _drop(self, r: _Reply, name: str) -> None:
        i = next((i for i, p in enumerate(r.pieces) if p.name == name), None)
        if i is None:
            return
        del r.pieces[i]
        idx, smp = r.resume
        r.resume = (idx - 1, smp) if i < idx else (idx, 0) if i == idx else (idx, smp)
        if i < r.written:
            r.written -= 1

    def _stop(self, rid: str) -> None:
        r = self.reply
        if r is None or r.rid != rid:
            return
        r.interrupted = True
        for t in r.tasks:
            t.cancel()
        self._spawn(self.c.brain.cancel())
        self.sink.drop()                           # nothing of this reply plays again
        # the controller's x_floor end follows in the same actions and ends the record (_ended)

    def _ended(self, r: _Reply, reason: Optional[str], spoken: str) -> None:
        if self.reply is not r:
            return
        self.reply = None
        r.spoken = spoken
        if self.sink.paused:
            self.sink.resume()                     # never leave the sink paused (FileAudio waits on it)
        self.log.emit("x_reply_text", self._turn(r.label), text=" ".join(p.text for p in r.pieces
                                                                         if p.kind == "content"),
                      interrupted=r.interrupted, spoken=spoken, reason=reason)

    # -- output ----------------------------------------------------------------------------------
    def transcript(self, scripts: Optional[dict] = None) -> dict:
        turns = []
        labels = sorted(set(self.user_turns) | set(scripts or {}),
                        key=lambda x: (x is None, not isinstance(x, int), x if isinstance(x, int) else 0, str(x)))
        for n in labels:
            turns.append({"role": "user", "turn": n, "script": (scripts or {}).get(n, ""),
                          "asr_text": self.user_turns.get(n, "")})
            for r in self.records:
                if r.kind == "answer" and self._turn(r.label) == n:
                    turns.append({"role": "assistant", "reply_to": n, "text_generated": r.text_generated,
                                  "text_spoken": (r.spoken or "") if r.interrupted else r.text_generated,
                                  "interrupted": r.interrupted, "fillers": []})
        return {"turns": turns}
