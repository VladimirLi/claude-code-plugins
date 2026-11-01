"""orchestrator: wires audio_io, endpointing, stt, brain, tts, bargein and turntaking.

It owns the conversation state and writes the system-side SPEC events
(turn_end_detected, final_transcript, first_reply_text, barge_in_detected).
The mic loop never awaits slow work: STT, brain and TTS run in tasks.

    orch = Orchestrator(audio, log, Components(...), cfg)
    await orch.start()      start/warm every component (before the session clock runs)
    await orch.run()        until audio.done
    orch.transcript()       SPEC transcript.json content
    await orch.close()

What the wiring does for a live feel (the "e2e" piece):

- The first audio of a reply is ready the moment the turn ends. The filler the
  turn-taking policy would pick is predicted from the interim transcript while
  the user is still talking (on a copy of the policy, so nothing is used up),
  and its audio is synthesized ahead. At the turn end the real decision is
  taken; if it wants a filler of the same kind and the predicted one is ready,
  the predicted one plays (and the policy copy that picked it becomes the
  policy). Otherwise the filler is spoken in two pieces: its interjection
  ("Hmm,", warmed at start, cached by the TTS) at once, and the rest behind it.
- Audio of a filler is not content. If the user starts talking again while the
  reply has only played a filler (a false turn end, or the user adds something
  right away), the filler is stopped and the two parts become one turn, as
  before any audio.
- While a reply is in flight and has started (filler playing, or the short
  silence between filler and content), the assistant holds the floor for the
  barge-in detector, so a backchannel there does not start a new turn.
- Interim transcripts go to the endpointer (text_probe "hint") and the turn
  taking policy learns the real wait for content (observe_content_latency).
"""
from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .bargein import BargeIn
from .brain import Brain, Interrupted
from .endpointing import Endpointer
from .events import SR, EventLog
from .stt import BaseSTT
from .tts import Chunker
from .turntaking import TurnContext, TurnTaking

KEEPALIVE = ["Still on it.", "Hang on.", "Almost there.", "Still going.", "Bear with me.", "Nearly done."]

DEFAULTS = {
    "predict_filler": True,      # synthesize the likely filler while the user talks
    "split_filler": True,        # if the filler is not ready: interjection first, the rest behind it
    "warm_interjections": True,  # synthesize the fillers' interjections at start (TTS caches short pieces)
    "learn_content_latency": True,
    "tell_brain_filler": True,
    "early_filler": True,
    # a short turn (speech + pause within probe_window_ms) goes to the brain on the endpointer's
    # on-device transcript at the turn end, not ~300 ms later on the final (Azure) transcript
    "brain_on_probe": True,
    "probe_window_ms": 3000.0,
    # while the brain works (a tool call, a test run) and nothing is heard for this long, say a
    # short holding line, then again after keepalive_every_ms; 0 = off
    "keepalive_after_ms": 4500.0,
    "keepalive_every_ms": 9000.0,
    # announce(): speak only after the conversation was free (no user speech, no reply) this long
    "announce_quiet_ms": 800.0,        # play the predicted filler at the turn end, not after the final transcript   # the brain hears which filler opened the reply, so it does not acknowledge twice
}


@dataclass
class Components:
    endpointer: Endpointer
    stt: BaseSTT
    brain: Brain
    tts: object
    bargein: BargeIn
    turntaking: TurnTaking


@dataclass
class Reply:
    turn: Optional[int]
    user_text: str
    text_generated: str = ""
    writes: list = field(default_factory=list)      # (sentence_idx, start_ms, dur_ms)
    sentences: list = field(default_factory=list)
    interrupted: bool = False
    text_spoken: Optional[str] = None
    audio_started: bool = False                    # any audio, filler included
    content_started: bool = False                  # audio from the brain's text
    fillers: list = field(default_factory=list)
    decided_ms: float = 0.0                        # session clock when the reply was decided
    after_interrupt: bool = False
    announce: bool = False                         # not an answer to a user turn (announce())


@dataclass
class _Prediction:
    kind: str
    filler: str
    policy: object
    text: str


class Orchestrator:
    def __init__(self, audio, log: EventLog, c: Components, cfg: Optional[dict] = None) -> None:
        self.audio = audio
        self.log = log
        self.c = c
        self.cfg = cfg or {}
        self.ocfg = {**DEFAULTS, **(self.cfg.get("orchestrator") or {})}
        self.clock = log.clock
        self.sink = audio.sink
        self.counter = 0
        self.label: Optional[int] = None
        self.gen = 0
        self.carry = ""
        self.reply: Optional[Reply] = None
        self.reply_task: Optional[asyncio.Task] = None
        self.pending_interrupt: Optional[Interrupted] = None
        self.tasks: set = set()
        self.user_turns: dict = {}
        self.replies: list[Reply] = []
        self.in_user_turn = False
        self.turn_start_ms = 0.0
        self.pcm: dict[str, asyncio.Task] = {}          # text -> task with the whole synthesized audio
        self.prediction: Optional[_Prediction] = None
        self.early: Optional[dict] = None               # filler played at the turn end, before the final text
        self.stats: list[dict] = []                    # per reply: how the first audio was obtained (debug)
        try:
            self.c.stt.on_interim = self._on_interim
        except Exception:
            pass
        try:
            audio.reply_active = self._reply_in_flight      # the bench harness waits for a reply to finish
        except Exception:
            pass

    def _reply_in_flight(self) -> bool:
        return self.early is not None or bool(self.reply_task is not None and not self.reply_task.done())

    async def start(self) -> None:
        async def safe(coro, name):
            try:
                await coro
            except Exception as e:  # warm-up failures must not kill the session
                self.log.emit("x_warmup_failed", None, t_ms=0.0, what=name, error=str(e)[:200])
        await asyncio.gather(safe(self.c.brain.start(), "brain"), safe(self.c.stt.start(), "stt"),
                             safe(self._start_tts(), "tts"))

    async def _start_tts(self) -> None:
        await self.c.tts.start()
        if not self.ocfg["warm_interjections"]:
            return
        heard: list = []

        async def synth_heads():
            for head in self._interjections():
                async for pcm in self.c.tts.synthesize(head):
                    heard.append(pcm)

        async def warm_probe():
            # The endpointer's text probe answers in ~50 ms, but its first call while the TTS is
            # synthesizing takes 300-400 ms (measured). Take that hit here, on speech the TTS made.
            probe = getattr(self.c.endpointer, "text", None)
            if probe is None or not hasattr(probe, "transcribe"):
                return
            for _ in range(2):
                while not heard:
                    await asyncio.sleep(0.02)
                try:
                    await asyncio.to_thread(probe.transcribe, np.concatenate(heard[-8:]))
                except Exception:
                    return
        await asyncio.gather(synth_heads(), warm_probe())

    def _interjections(self) -> list[str]:
        return self._filler_heads() + (KEEPALIVE if self.ocfg["keepalive_after_ms"] else [])

    def _filler_heads(self) -> list[str]:
        from . import turntaking as ttm
        heads = []
        for xs in getattr(ttm, "_INTERJ", {}).values():
            for i in xs:
                h = i.strip(", ") + ","
                if h not in heads:
                    heads.append(h)
        return heads

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        for t in self.pcm.values():
            t.cancel()
        await self.c.brain.close()
        await self.c.stt.close()
        await self.c.tts.close()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    # -- mic loop ------------------------------------------------------------
    def _assistant_holds_floor(self) -> bool:
        if self.sink.is_playing():
            return True
        r = self.reply
        return bool(r is not None and r.audio_started and self.reply_task and not self.reply_task.done())

    async def run(self) -> None:
        async for frame in self.audio.frames():
            t = self.clock.ms
            self.c.stt.feed(frame)
            holds = self._assistant_holds_floor()
            bd = self.c.bargein.feed(frame, t, holds)
            if bd is not None:
                last = getattr(self.c.bargein, "last", None) or {}
                self.log.emit("x_bargein", self.label, decision=bd, reason=last.get("reason"),
                              onset_ms=last.get("onset_ms"), speech_ms=last.get("speech_ms"),
                              assistant_speaking=holds, playing=self.sink.is_playing())
            if bd == "barge_in":
                self._barge_in()
            for d in self.c.endpointer.feed(frame, t):
                if d.kind == "speech_start":
                    self._on_speech_start()
                elif d.kind == "turn_end":
                    self.in_user_turn = False
                    self.log.emit("turn_end_detected", self.label, source=d.source)
                    self._log_endpoint()
                    self._early_filler(self.label)
                    self._spawn(self._handle_turn(self.label, self.gen))
        # let a reply in flight finish its bookkeeping
        await asyncio.sleep(0)

    def _log_endpoint(self) -> None:
        tr = getattr(self.c.endpointer, "trace", None)
        if tr and isinstance(tr[-1], dict) and "pause_start_ms" in tr[-1]:
            x = tr[-1]
            self.log.emit("x_endpoint", self.label, pause_start_ms=x.get("pause_start_ms"), cls=x.get("class"),
                          text_ms=x.get("text_ms"), smart_turn=x.get("smart_turn"))

    def _on_speech_start(self) -> None:
        self.gen += 1
        self.turn_start_ms = self.clock.ms
        self.in_user_turn = True
        self.prediction = None
        hint = self.audio.turn_hint()
        r = self.reply
        if r is not None and r.announce and not r.audio_started:
            self._cancel_reply(keep_record=False)   # announce() tries again once the user is done
            r = None
        if r is not None and not r.audio_started and self.reply_task and not self.reply_task.done():
            # User kept talking before any reply audio: merge into one turn.
            if self.c.turntaking.on_user_resumed(TurnContext(r.user_text)) == "merge":
                self.carry = r.user_text
                self._cancel_reply(keep_record=False)
                self.label = hint if hint is not None else self.label
                self.c.stt.begin()
                return
        if not self.carry:
            self.counter += 1
            self.label = hint if hint is not None else self.counter
        elif hint is not None:
            self.label = hint
        self.c.stt.begin()

    # -- interim text: endpointer hint + filler prediction -----------------------
    def _on_interim(self, text: str) -> None:
        try:
            self.c.endpointer.on_transcript(text, False)
        except Exception:
            pass
        if not self.in_user_turn or not self.ocfg["predict_filler"] or self._assistant_holds_floor():
            return                      # (a prefetch would queue in front of the reply's next sentence)
        full = (self.carry + " " + text).strip() if self.carry else text
        try:
            policy = copy.deepcopy(self.c.turntaking)
            a = policy.decide(self._ctx(full))
        except Exception:
            return
        if a.kind != "respond" or not a.filler:
            return
        p = self.prediction
        if p is not None and p.kind == a.reason and p.filler in self.pcm and not self.pcm[p.filler].cancelled():
            return                      # a filler of this kind is ready or on its way
        if p is not None:
            self._drop_prefetch(p.filler)
        self.prediction = _Prediction(a.reason, a.filler, policy, full)
        self._prefetch(a.filler)

    def _ctx(self, text: str) -> TurnContext:
        return TurnContext(text, assistant_was_speaking=self.sink.is_playing(),
                           last_reply=self.replies[-1].text_generated if self.replies else "")

    def _prefetch(self, text: str) -> asyncio.Task:
        t = self.pcm.get(text)
        if t is None:
            async def synth():
                out = []
                async for pcm in self.c.tts.synthesize(text):
                    out.append(pcm)
                return out
            t = asyncio.ensure_future(synth())
            self.pcm[text] = t
            if len(self.pcm) > 64:
                for k in list(self.pcm)[:16]:
                    if self.pcm[k].done():
                        del self.pcm[k]
        return t

    def _drop_prefetch(self, text: str) -> None:
        """Cancel a prefetch that is not done (a queued one leaves the TTS queue at no cost)."""
        t = self.pcm.get(text)
        if t is not None and not t.done():
            t.cancel()
            del self.pcm[text]

    def _ready(self, text: str) -> Optional[list]:
        t = self.pcm.get(text)
        if t is None or not t.done() or t.cancelled() or t.exception() is not None:
            return None
        return t.result()

    def _decide(self, full: str):
        """The policy's action for the final text; prefers a filler whose audio is already made."""
        tt = self.c.turntaking
        ctx = self._ctx(full)
        p, self.prediction = self.prediction, None
        if p is None:
            return tt.decide(ctx), "none"
        try:
            fresh = copy.deepcopy(tt)
        except Exception:
            return tt.decide(ctx), "none"
        a = fresh.decide(ctx)
        if a.kind == "respond" and a.filler and a.reason == p.kind and self._ready(p.filler) is not None:
            self.c.turntaking = p.policy            # the copy that picked (and used up) the ready filler
            a.filler = p.filler
            return a, "predicted"
        self._drop_prefetch(p.filler)               # it would hold the TTS in front of this turn's audio
        self.c.turntaking = fresh
        return a, "miss"

    def _early_filler(self, label: Optional[int]) -> None:
        """At the turn end, start the filler at once, before the final transcript: the words
        so far (interim text + the endpointer's probe text of the turn's end) pick it. The
        predicted filler plays whole if it fits; otherwise its interjection (cached) plays
        at once and the rest follows. The final transcript and the brain follow in _handle_turn."""
        if not self.ocfg["early_filler"] or self.sink.is_playing():
            return
        tr = getattr(self.c.endpointer, "trace", None)
        tail = (tr[-1].get("text") or "") if tr and isinstance(tr[-1], dict) else ""
        p = self.prediction
        prov = ((p.text + " ") if p is not None else "") + tail
        if not prov.strip():
            return
        try:
            if p is not None and self._ready(p.filler) is not None:
                a = copy.deepcopy(p.policy).decide(self._ctx(prov))
                if a.kind == "respond" and a.reason == p.kind:
                    self.prediction = None
                    writes = []
                    for pcm in self._ready(p.filler):
                        writes.append((0, self.sink.write(pcm, turn=label, filler=True), len(pcm) * 1000.0 / SR))
                    self.early = {"label": label, "filler": p.filler, "policy": p.policy, "writes": writes,
                                  "flushed": False, "t_ms": self.clock.ms, "task": None, "source": "early",
                                  "probe": self._probe_covers(tail)}
                    return
            policy = copy.deepcopy(self.c.turntaking)
            a = policy.decide(self._ctx(prov))
        except Exception:
            return
        if a.kind != "respond" or not a.filler:
            return
        if p is not None:
            self._drop_prefetch(p.filler)
        self.prediction = None
        e = {"label": label, "filler": a.filler, "policy": policy, "writes": [], "flushed": False,
             "t_ms": self.clock.ms, "task": None, "source": "early_split", "probe": self._probe_covers(tail)}
        e["task"] = self._spawn(self._play_early(e))
        self.early = e

    def _probe_covers(self, tail: str) -> Optional[str]:
        """The probe text, if it holds the whole utterance (the probe sends the last 3.5 s)."""
        if not tail.strip() or self.clock.ms - self.turn_start_ms > float(self.ocfg["probe_window_ms"]):
            return None
        return tail.strip()

    async def _play_early(self, e: dict) -> None:
        f = e["filler"]
        m = re.match(r"\s*([^,]+,)\s+(.+)$", f) if self.ocfg["split_filler"] else None
        for part in ([m.group(1), m.group(2)] if m else [f]):
            async for pcm in self.c.tts.synthesize(part):
                if e["flushed"]:
                    return
                e["writes"].append((0, self.sink.write(pcm, turn=e["label"], filler=True), len(pcm) * 1000.0 / SR))

    def _stop_early(self, e: dict) -> None:
        e["flushed"] = True
        if e.get("task") is not None:
            e["task"].cancel()
        if self.sink.is_playing():
            self.sink.flush(spoken_text="")

    def _take_early(self, label: Optional[int]) -> Optional[dict]:
        e = self.early
        if e is None or e["label"] != label:
            return None
        self.early = None
        return e

    async def _final_later(self, label: Optional[int], carry: str, probe_full: str) -> None:
        text = await self.c.stt.finalize()
        full = (carry + " " + text).strip() if carry else text
        self.log.emit("final_transcript", label, text=full)
        self.c.endpointer.on_transcript(full, True)
        self.c.bargein.on_transcript(full)
        if self.user_turns.get(label) == probe_full:
            self.user_turns[label] = full
        elif self.carry == probe_full:
            self.carry = full

    def _decide_early(self, full: str, early: dict):
        """The final decision when a filler already played: respond or not on the final text,
        with the policy state that picked the filler."""
        self.prediction = None
        try:
            a = copy.deepcopy(early["policy"]).decide(self._ctx(full))
        except Exception:
            a = self.c.turntaking.decide(self._ctx(full))
        self.c.turntaking = early["policy"]
        a.filler = early["filler"] if a.kind == "respond" else None
        return a, early.get("source", "early")

    async def _handle_turn(self, label: Optional[int], gen: int) -> None:
        e = self.early if self.early is not None and self.early["label"] == label else None
        if e is not None and e.get("probe") and self.ocfg["brain_on_probe"] and not e["flushed"]:
            # Short turn: the endpointer's on-device transcript already holds all of it. The brain
            # starts on it now; the final transcript (Azure, ~300 ms later) is logged when it lands.
            text = e["probe"]
            full = (self.carry + " " + text).strip() if self.carry else text
            self._spawn(self._final_later(label, self.carry, full))
        else:
            text = await self.c.stt.finalize()
            full = (self.carry + " " + text).strip() if self.carry else text
            self.log.emit("final_transcript", label, text=full)
            self.c.endpointer.on_transcript(full, True)
            self.c.bargein.on_transcript(full)
        early = self._take_early(label)
        if gen != self.gen or (early is not None and early["flushed"]):
            if early is not None and not early["flushed"]:
                self._stop_early(early)
            self.carry = full          # user is speaking again; merge on next turn end
            return
        self.carry = ""
        self.user_turns[label] = full
        if early is not None:
            action, how = self._decide_early(full, early)
        else:
            action, how = self._decide(full)
        if action.kind != "respond":
            if early is not None:
                self._stop_early(early)
            return
        if self.reply_task and not self.reply_task.done():
            self._cancel_reply(keep_record=True)
        r = Reply(label, full, decided_ms=early["t_ms"] if early else self.clock.ms)
        if early is not None:
            r.fillers.append(early["filler"])
            r.sentences.append(early["filler"])
            r.writes = early["writes"]                  # the split filler may still be writing
            r.audio_started = True
        self.reply = r
        self.replies.append(r)
        interrupted, self.pending_interrupt = self.pending_interrupt, None
        r.after_interrupt = interrupted is not None
        self.stats.append({"turn": label, "filler": action.filler, "filler_source": how})
        if action.filler:
            self.log.emit("x_filler", label, text=action.filler, source=how,
                          ready=self._ready(action.filler) is not None)
        self.reply_task = self._spawn(self._reply(r, interrupted, None if early else action.filler,
                                                  pre=early["task"] if early else None))

    # -- reply pipeline --------------------------------------------------------
    async def _reply(self, r: Reply, interrupted: Optional[Interrupted], filler: Optional[str],
                     pre: Optional[asyncio.Task] = None) -> None:
        order: asyncio.Queue = asyncio.Queue()
        synth_tasks: list[asyncio.Task] = []

        def speak(text: str, is_filler: bool = False, ready: Optional[list] = None) -> None:
            q: asyncio.Queue = asyncio.Queue()
            idx = len(r.sentences)
            r.sentences.append(text)
            if ready is not None:
                for pcm in ready:
                    q.put_nowait(pcm)
                q.put_nowait(None)
            else:
                async def fill():
                    try:
                        async for pcm in self.c.tts.synthesize(text):
                            q.put_nowait(pcm)
                    finally:
                        q.put_nowait(None)
                synth_tasks.append(self._spawn(fill()))
            order.put_nowait((idx, q, is_filler))

        opener = filler or (r.fillers[0] if r.fillers else None)
        said = opener if (opener and self.ocfg["tell_brain_filler"]) else None
        prompt = (f'[Already spoken aloud as the first words of this reply: "{said}" Do not repeat, echo or '
                  f'rephrase those words, and do not acknowledge again; otherwise reply as you normally '
                  f'would.] {r.user_text}') if said else r.user_text

        async def produce():
            chunker = Chunker()
            first = True
            async for delta in self.c.brain.respond(prompt, interrupted):
                if first:
                    self.log.emit("first_reply_text", r.turn, text=delta)
                    first = False
                r.text_generated += delta
                for s in chunker.push(delta):
                    speak(s)
            for s in chunker.flush():
                speak(s)
            order.put_nowait(None)

        if filler:
            r.fillers.append(filler)
            self._speak_filler(filler, speak)
        prod = self._spawn(produce())
        keep = self._spawn(self._keepalive(r, order)) if self.ocfg["keepalive_after_ms"] else None
        try:
            if pre is not None:
                await asyncio.wait({pre})                 # the early filler's audio goes first
            while True:
                item = await order.get()
                if item is None:
                    break
                idx, q, is_filler = item
                while True:
                    pcm = await q.get()
                    if pcm is None:
                        break
                    start = self.sink.write(pcm, turn=r.turn, filler=is_filler)
                    if not is_filler and not r.content_started:
                        r.content_started = True
                        self._learn(r, start)
                        if r.fillers:
                            # SPEC: content onset after a filler, for v2v_content_ms
                            self.log.emit("first_reply_audio", r.turn, t_ms=start, filler=False)
                    r.audio_started = True
                    r.writes.append((idx, start, len(pcm) * 1000.0 / SR))
            await prod
            if keep is not None:
                keep.cancel()
            self.log.emit("x_reply_text", r.turn, text=" ".join(r.sentences), interrupted=False)
        except asyncio.CancelledError:
            self.log.emit("x_reply_text", r.turn, text=" ".join(r.sentences), interrupted=True,
                          spoken=r.text_spoken)
            prod.cancel()
            if pre is not None:
                pre.cancel()
            if keep is not None:
                keep.cancel()
            for t in synth_tasks:
                t.cancel()
            raise

    async def _keepalive(self, r: Reply, order: asyncio.Queue) -> None:
        """Long silence inside a reply (the brain is running a tool): a short holding line."""
        after = float(self.ocfg["keepalive_after_ms"])
        every = float(self.ocfg["keepalive_every_ms"])
        quiet_since = self.clock.ms
        last_said = -1e9
        while True:
            await asyncio.sleep(0.1)
            now = self.clock.ms
            if self.sink.is_playing() or not order.empty():
                quiet_since = max(now, self.sink.queued_end_ms)
                continue
            if now - quiet_since < after or now - last_said < every:
                continue
            used = getattr(self, "_keepalive_used", 0)
            line = KEEPALIVE[used % len(KEEPALIVE)]
            self._keepalive_used = used + 1
            idx = len(r.sentences)
            r.sentences.append(line)
            async for pcm in self.c.tts.synthesize(line):
                start = self.sink.write(pcm, turn=r.turn, filler=True)
                r.audio_started = True
                r.writes.append((idx, start, len(pcm) * 1000.0 / SR))
            self.log.emit("x_keepalive", r.turn, text=line)
            last_said = self.clock.ms
            quiet_since = self.sink.queued_end_ms

    def _speak_filler(self, filler: str, speak) -> None:
        ready = self._ready(filler)
        if ready is not None:
            speak(filler, True, ready)
            return
        m = re.match(r"\s*([^,]+,)\s+(.+)$", filler)
        if self.ocfg["split_filler"] and m:
            speak(m.group(1), True)
            speak(m.group(2), True)
        else:
            speak(filler, True)

    def _learn(self, r: Reply, start_ms: float) -> None:
        if not self.ocfg["learn_content_latency"] or r.announce:
            return
        f = getattr(self.c.turntaking, "observe_content_latency", None)
        if f is None:
            return
        try:
            f(max(0.0, start_ms - r.decided_ms), after_interrupt=r.after_interrupt)
        except Exception:
            pass

    def _spoken_so_far(self, r: Reply) -> str:
        now = self.clock.ms
        out = []
        for idx, s in enumerate(r.sentences):
            ws = [w for w in r.writes if w[0] == idx]
            if not ws:
                break
            total = sum(d for _, _, d in ws)
            played = sum(max(0.0, min(d, now - st)) for _, st, d in ws)
            words = s.split()
            if played >= total:
                out.append(s)
                continue
            k = int(len(words) * played / total) if total else 0
            if k:
                out.append(" ".join(words[:k]))
            break
        return " ".join(out)

    def _cancel_reply(self, keep_record: bool) -> None:
        r = self.reply
        if self.reply_task and not self.reply_task.done():
            self.reply_task.cancel()
        self._spawn(self.c.brain.cancel())
        if r is not None and not keep_record and not r.audio_started and r in self.replies:
            self.replies.remove(r)
        self.reply = None

    def _barge_in(self) -> None:
        r = self.reply
        e = self.early
        if e is not None and not e["flushed"] and self.sink.is_playing():
            # the user talks over a filler played before the final text: still their turn
            self.log.emit("barge_in_detected", e["label"])
            self._stop_early(e)
            return
        if r is None:
            return
        hint = self.audio.turn_hint()
        self.log.emit("barge_in_detected", hint if hint is not None else self.counter + 1)
        spoken = self._spoken_so_far(r)
        self.sink.flush(spoken_text=spoken)
        r.interrupted = True
        r.text_spoken = spoken
        if not r.content_started and not r.announce and self.reply_task and not self.reply_task.done():
            # Only a filler was heard: the user is still on their turn. One turn, as before any audio.
            self.carry = (self.carry + " " + r.user_text).strip() if self.carry else r.user_text
            self._cancel_reply(keep_record=True)
            return
        self.pending_interrupt = Interrupted(spoken)
        self._cancel_reply(keep_record=True)

    # -- announcements: speaking without a user turn ---------------------------
    def announce(self, prompt: str) -> asyncio.Task:
        """Speak a reply nobody asked for just now (a task handed to Claude Code finished).

        The brain gets `prompt` as a turn of its own once the conversation is free: the user
        is not talking, no reply is in flight, nothing plays, for announce_quiet_ms. The user
        can talk over it like over any reply. If the user starts talking before any of its
        audio played, it is dropped and tried again when they are done."""
        return self._spawn(self._announce(prompt))

    def _free(self) -> bool:
        return not self.in_user_turn and not self._reply_in_flight() and not self.sink.is_playing()

    async def _announce(self, prompt: str) -> None:
        quiet_s = float(self.ocfg["announce_quiet_ms"]) / 1000.0
        while True:
            free_since = None
            while True:
                now = asyncio.get_running_loop().time()
                if not self._free():
                    free_since = None
                elif free_since is None:
                    free_since = now
                elif now - free_since >= quiet_s:
                    break
                await asyncio.sleep(0.05)
            r = Reply(None, prompt, decided_ms=self.clock.ms, announce=True)
            self.reply = r
            self.replies.append(r)
            interrupted, self.pending_interrupt = self.pending_interrupt, None
            r.after_interrupt = interrupted is not None
            self.log.emit("x_announce", None, text=prompt[:200])
            task = self._spawn(self._reply(r, interrupted, None))
            self.reply_task = task
            await asyncio.wait({task})
            if r.audio_started or not task.cancelled():
                return

    # -- output --------------------------------------------------------------
    def transcript(self, scripts: Optional[dict] = None) -> dict:
        turns = []
        labels = sorted(set(self.user_turns) | set(scripts or {}), key=lambda x: (x is None, x))
        for n in labels:
            turns.append({"role": "user", "turn": n, "script": (scripts or {}).get(n, ""),
                          "asr_text": self.user_turns.get(n, "")})
            for r in self.replies:
                if r.turn == n:
                    turns.append({"role": "assistant", "reply_to": n, "text_generated": r.text_generated,
                                  "text_spoken": r.text_spoken if r.interrupted else r.text_generated,
                                  "interrupted": r.interrupted, "fillers": r.fillers})
        return {"turns": turns}


# Settings the whole pipeline needs on top of each module's own defaults. A module section in
# the config still wins key by key.
E2E_OVERLAY = {
    "endpointing": {
        # The text probe (how does the turn end: "?", a dangling "if", a period) runs on the
        # device: Whisper base.en on the GPU answers in ~50 ms, Azure in 320-600 ms. A question
        # then ends the turn ~150 ms after the user stops, not ~450 ms.
        "text_probe": "local",
        "text_probe_at_ms": 40.0,
    },
}


def with_overlay(cfg: Optional[dict]) -> dict:
    cfg = dict(cfg or {})
    for sec, vals in E2E_OVERLAY.items():
        if isinstance(cfg.get(sec, {}), dict) and "silence_ms" not in (cfg.get(sec) or {}):
            cfg[sec] = {**vals, **(cfg.get(sec) or {})}
    return cfg


def build(cfg: Optional[dict] = None):
    """Default components for a config dict with optional per-module sections."""
    from .stt import make_stt
    from .tts import make_tts
    cfg = with_overlay(cfg)
    return Components(
        endpointer=Endpointer(cfg.get("endpointing")),
        stt=make_stt(cfg.get("stt")),
        brain=Brain(cfg.get("brain")),
        tts=make_tts(cfg.get("tts")),
        bargein=BargeIn(cfg.get("bargein")),
        turntaking=TurnTaking(cfg.get("turntaking")),
    )
