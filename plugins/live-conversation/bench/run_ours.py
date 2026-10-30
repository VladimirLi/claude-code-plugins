#!/usr/bin/env python3
"""Run OUR system on bench scenarios and write SPEC run folders.

Usage (from the plugin folder, with the plugin venv):
  .venv/bin/python bench/run_ours.py [--mode e2e|brain|tts] [--scenarios s01_quick_fact,s07_followup_chain]
                                     [--run-id ID] [--runs-dir bench/runs] [--config cfg.json] [--fast]

Modes:
  e2e    scenario WAVs streamed as the mic in real time through the whole pipeline
         (used by the endpointing, stt, bargein, turntaking and end-to-end pieces)
  brain  user turn texts straight into the brain (brain piece)
  tts    texts from bench/tts_texts.json into the TTS, all at once (<id>/) and
         streamed at 4 words/s (<id>_stream/) (tts piece)

Prints the run folder path on the last line.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time

import numpy as np

BENCH = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(BENCH)
REPO = os.path.dirname(os.path.dirname(PLUGIN))
sys.path.insert(0, PLUGIN)

from live.audio_io import FileAudio, write_wav  # noqa: E402
from live.brain import Brain, Interrupted  # noqa: E402
from live.events import SR, Clock, EventLog  # noqa: E402
from live.orchestrator import Orchestrator, build, with_overlay  # noqa: E402
from live.tts import Chunker, make_tts  # noqa: E402

SCEN = os.path.join(BENCH, "scenarios")
WORDS_PER_S_CUT = 2.7


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PLUGIN, capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def all_scenarios() -> list[str]:
    return [s["id"] for s in json.load(open(os.path.join(SCEN, "index.json")))]


# -- e2e -------------------------------------------------------------------------
class SharedTTS:
    """One TTS for every scenario of a run, as the live daemon keeps one for a whole session.

    Loading Kokoro and warming it takes ~20 s; doing that per scenario made a 13-scenario run
    take ~13 min (bench/blind.py timed out). Each scenario still starts from the state of a
    freshly loaded TTS: its cache is reset to the snapshot taken when the first scenario's
    start (model load, then the orchestrator's interjection warm-up) finished, which is the
    state every cold start ends in. close() is a no-op for the orchestrator; run_ours closes
    the real TTS at the end."""

    def __init__(self, tts) -> None:
        self.tts = tts
        self.snapshot = None

    def __getattr__(self, name):
        return getattr(self.tts, name)

    async def start(self) -> None:
        await self.tts.start()
        cache = getattr(self.tts, "cache", None)
        if cache is not None and self.snapshot is not None:
            cache.clear()
            cache.update(self.snapshot)

    def mark_warm(self) -> None:
        """Called once the first scenario's orchestrator start (model load + interjection
        warm-up) is done: that cache is what every cold start ends with."""
        cache = getattr(self.tts, "cache", None)
        if cache is not None and self.snapshot is None:
            self.snapshot = list(cache.items())

    def synthesize(self, text: str):
        return self.tts.synthesize(text)

    async def close(self) -> None:
        pass

    async def aclose(self) -> None:
        await self.tts.close()


SCENARIO_WALL_CAP_S = 300.0          # one stuck scenario must not hang a whole bench run


def _scenario_cfg(cfg: dict) -> dict:
    cfg = json.loads(json.dumps(cfg))
    cfg.setdefault("brain", {}).setdefault("cwd", REPO)
    return cfg


class BrainPrestart:
    """Starts the next scenario's brain (a fresh conversation) while the current scenario only
    waits out its tail: every turn fed, the last reply fully generated and queued, so nothing
    measured is left but audio already placed on the clock. Saves the ~5 s brain warm-up per
    scenario. The next scenario gets the prestarted brain, so its own start() only waits for it."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.brain: Brain | None = None
        self.task: asyncio.Task | None = None

    def fire(self) -> None:
        if self.task is None:
            self.brain = Brain(with_overlay(_scenario_cfg(self.cfg)).get("brain"))
            self.task = asyncio.ensure_future(self.brain.start())

    def take(self) -> Brain | None:
        if self.task is None:
            return None
        b, t = self.brain, self.task
        self.brain = self.task = None

        async def started() -> None:
            await t
        b.start = started
        return b

    async def discard(self) -> None:
        b = self.take()
        if b is not None:
            try:
                await b.start()
            except Exception:
                pass
            await b.close()


def _only_tail_left(audio) -> bool:
    """All turns fed, the last one got reply audio, and no reply is generating any more."""
    try:
        if audio._next < len(audio.turns) or audio._cur is not None:
            return False
        tr = audio._trackers.get(audio.turns[-1]["turn"])
        return tr is not None and tr.start is not None and not audio._reply_active()
    except Exception:
        return False


async def _watch_tail(audio, prestart: BrainPrestart) -> None:
    while not audio.done:
        if _only_tail_left(audio):
            prestart.fire()
            return
        await asyncio.sleep(0.05)


async def run_e2e(sid: str, out: str, cfg: dict, realtime: bool = True, tts: SharedTTS | None = None,
                  brain: Brain | None = None, prestart_next: BrainPrestart | None = None) -> dict:
    """One scenario. `tts`: shared across scenarios (else a fresh one). `brain`: already started
    by a BrainPrestart (else a fresh one). `prestart_next`: fired once only the tail is left."""
    os.makedirs(out, exist_ok=True)
    log = EventLog(Clock())
    audio = FileAudio(os.path.join(SCEN, sid), log, realtime=realtime)
    cfg = _scenario_cfg(cfg)
    comps = build(cfg)
    if tts is not None:
        comps.tts = tts
    if brain is not None:
        comps.brain = brain
    orch = Orchestrator(audio, log, comps, cfg)
    t = time.monotonic()
    await orch.start()
    t_start = time.monotonic() - t
    if tts is not None:
        tts.mark_warm()
    watch = asyncio.ensure_future(_watch_tail(audio, prestart_next)) if prestart_next is not None else None
    try:
        await asyncio.wait_for(orch.run(), SCENARIO_WALL_CAP_S)
    except asyncio.TimeoutError:
        log.emit("x_bench_timeout", None, cap_s=SCENARIO_WALL_CAP_S)
        print(f"{sid}: stopped after {SCENARIO_WALL_CAP_S:.0f} s", file=sys.stderr, flush=True)
    finally:
        if watch is not None:
            watch.cancel()
        await orch.close()
    audio.save(out)
    log.write(os.path.join(out, "events.jsonl"))
    scripts = {t["turn"]: t["text"] for t in audio.turns}
    json.dump(orch.transcript(scripts), open(os.path.join(out, "transcript.json"), "w"), indent=2)
    return {"events": len(log.events), "start_s": round(t_start, 1), "session_s": round(audio.clock.ms / 1000.0, 1)}


# -- brain piece -------------------------------------------------------------------
async def run_brain(sid: str, out: str, cfg: dict) -> dict:
    os.makedirs(os.path.join(out, "piece"), exist_ok=True)
    sc = json.load(open(os.path.join(SCEN, sid, "scenario.json")))
    bcfg = {**cfg.get("brain", {})}
    bcfg.setdefault("cwd", REPO)
    brain = Brain(bcfg)
    await brain.start()
    clock_t0 = time.monotonic()
    ms = lambda: (time.monotonic() - clock_t0) * 1000.0
    events, turns, per_turn = [], [], []
    prev_reply = ""
    try:
        for t in sc["turns"]:
            n = t["turn"]
            interrupted = None
            if t.get("barge_in") and prev_reply:
                k = int(t["start"]["offset_ms"] / 1000.0 * WORDS_PER_S_CUT)
                spoken = " ".join(prev_reply.split()[:k])
                interrupted = Interrupted(spoken)
                turns[-1]["text_spoken"] = spoken
                turns[-1]["interrupted"] = True
            t_sub = ms()
            events.append({"t_ms": round(t_sub, 2), "event": "x_turn_submitted", "turn": n})
            turns.append({"role": "user", "turn": n, "script": t["text"], "asr_text": t["text"]})
            ch, text, first, first_sent = Chunker(), "", None, None
            async for d in brain.respond(t["text"], interrupted):
                if first is None:
                    first = ms()
                    events.append({"t_ms": round(first, 2), "event": "first_reply_text", "turn": n, "text": d})
                text += d
                if first_sent is None and ch.push(d):
                    first_sent = ms()
            if first_sent is None and text.strip():
                first_sent = ms()
            done = ms()
            if first_sent is not None:
                events.append({"t_ms": round(first_sent, 2), "event": "x_first_sentence", "turn": n})
            events.append({"t_ms": round(done, 2), "event": "x_reply_done", "turn": n})
            turns.append({"role": "assistant", "reply_to": n, "text_generated": text, "text_spoken": text,
                          "interrupted": False, "fillers": []})
            per_turn.append({"turn": n, "time_to_first_sentence_ms": None if first_sent is None else round(first_sent - t_sub, 1)})
            prev_reply = text
    finally:
        await brain.close()
    with open(os.path.join(out, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    json.dump({"turns": turns}, open(os.path.join(out, "transcript.json"), "w"), indent=2)
    json.dump({"turns": per_turn}, open(os.path.join(out, "piece", "brain.json"), "w"), indent=2)
    return {}


# -- tts piece ---------------------------------------------------------------------
async def _speak_timed(tts, pieces_source, out: str) -> None:
    """pieces_source: async iterator of text deltas. Audio placed on a wall clock sink."""
    os.makedirs(out, exist_ok=True)
    t0 = time.monotonic()
    ms = lambda: (time.monotonic() - t0) * 1000.0
    buf = np.zeros(SR * 60, np.int16)
    end = 0
    events = [{"t_ms": 0.0, "event": "x_text_available", "turn": 1}]
    order: asyncio.Queue = asyncio.Queue()

    def speak(s):
        q: asyncio.Queue = asyncio.Queue()
        async def fill():
            try:
                async for pcm in tts.synthesize(s):
                    q.put_nowait(pcm)
            finally:
                q.put_nowait(None)
        asyncio.create_task(fill())
        order.put_nowait(q)

    async def produce():
        ch = Chunker()
        async for d in pieces_source:
            for s in ch.push(d):
                speak(s)
        for s in ch.flush():
            speak(s)
        order.put_nowait(None)

    prod = asyncio.create_task(produce())
    first = None
    while True:
        q = await order.get()
        if q is None:
            break
        while True:
            pcm = await q.get()
            if pcm is None:
                break
            start = max(int(ms() * SR / 1000), end)
            if start + len(pcm) > len(buf):
                buf = np.concatenate([buf, np.zeros(start + len(pcm) - len(buf) + SR, np.int16)])
            buf[start:start + len(pcm)] = pcm
            end = start + len(pcm)
            if first is None:
                first = start * 1000.0 / SR
                events.append({"t_ms": round(first, 2), "event": "first_reply_audio", "turn": 1, "filler": False})
    await prod
    events.append({"t_ms": round(end * 1000.0 / SR, 2), "event": "reply_audio_end", "turn": 1})
    write_wav(os.path.join(out, "assistant.wav"), buf[: end + SR // 2])
    with open(os.path.join(out, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


async def run_tts(out_root: str, cfg: dict, ids: list[str] | None) -> None:
    texts = json.load(open(os.path.join(BENCH, "tts_texts.json")))["texts"]
    tts = make_tts(cfg.get("tts"))
    await tts.start()
    for item in texts:
        if ids and item["id"] not in ids:
            continue
        text = item["text"]

        async def whole():
            yield text

        async def streamed():
            words = text.split(" ")
            t0 = time.monotonic()
            for i, w in enumerate(words):
                delay = t0 + i / 4.0 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                yield (w if i == 0 else " " + w)

        for suffix, src in (("", whole()), ("_stream", streamed())):
            d = os.path.join(out_root, item["id"] + suffix)
            await _speak_timed(tts, src, d)
            json.dump({"turns": [{"role": "assistant", "reply_to": 1, "text_generated": text, "text_spoken": text,
                                  "interrupted": False, "fillers": []}]},
                      open(os.path.join(d, "transcript.json"), "w"), indent=2)
    await tts.close()


async def amain() -> str:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="e2e", choices=["e2e", "brain", "tts"])
    ap.add_argument("--scenarios", help="comma list of scenario ids (or tts text ids); default all")
    ap.add_argument("--run-id")
    ap.add_argument("--runs-dir", default=os.path.join(BENCH, "runs"))
    ap.add_argument("--config", help="JSON file with per-module config sections")
    ap.add_argument("--fast", action="store_true", help="feed audio faster than real time (debug only; invalid for latency)")
    a = ap.parse_args()
    cfg = json.load(open(a.config)) if a.config else {}
    run_id = a.run_id or f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-ours-{a.mode}"
    run = os.path.join(a.runs_dir, run_id)
    os.makedirs(run, exist_ok=True)
    json.dump({"system": "ours", "created": dt.datetime.now(dt.timezone.utc).isoformat(), "git_commit": git_commit(),
               "config": {"mode": a.mode, **cfg}}, open(os.path.join(run, "run.json"), "w"), indent=2)
    ids = a.scenarios.split(",") if a.scenarios else None
    if a.mode == "tts":
        await run_tts(run, cfg, ids)
    else:
        tts = SharedTTS(make_tts(cfg.get("tts"))) if a.mode == "e2e" else None
        prestart = BrainPrestart(cfg) if a.mode == "e2e" else None
        sids = ids or all_scenarios()
        t_run = time.monotonic()
        try:
            for i, sid in enumerate(sids):
                t = time.monotonic()
                info = {}
                if a.mode == "e2e":
                    info = await run_e2e(sid, os.path.join(run, sid), cfg, realtime=not a.fast, tts=tts,
                                         brain=prestart.take(),
                                         prestart_next=prestart if i + 1 < len(sids) else None)
                else:
                    await run_brain(sid, os.path.join(run, sid), cfg)
                extra = "".join(f" {k}={info[k]}" for k in ("start_s", "session_s") if k in info)
                print(f"{sid}: {time.monotonic() - t:.1f}s{extra} (run {time.monotonic() - t_run:.0f}s)",
                      file=sys.stderr, flush=True)
        finally:
            if prestart is not None:
                await prestart.discard()
            if tts is not None:
                await tts.aclose()
    return run


if __name__ == "__main__":
    print(asyncio.run(amain()))
