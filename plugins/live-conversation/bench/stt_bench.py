#!/usr/bin/env python3
"""STT-only bench: every scenario turn through the endpointer + STT in real time, no brain, no TTS.

Usage: .venv/bin/python bench/stt_bench.py [--config cfg.json] [--scenarios s01_quick_fact,...]
                                           [--out results.json] [--compare bench/runs/<bar-run>]

Per turn: noise floor 1 s, the turn WAV, noise 1.6 s, fed as 20 ms frames on the real clock.
The endpointer (default config unless --config has "endpointing") gives speech_start and turn_end,
like the orchestrator; a turn_end inside the turn is merged with the rest (the orchestrator's
carry). Reports WER vs the script (bench/piece_metrics.wer, the SPEC metric), the delay from
ground-truth speech end to the final transcript (stt_delay_ms, as the SPEC defines it), and
`finalize_ms` (turn_end to final text, the part the STT owns) and `text_ready_ms` (speech end to the
moment the final text was back from the service, whatever the endpointer does). With --compare it prints the
same metrics for a bar run next to ours.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import numpy as np

BENCH = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(BENCH)
sys.path.insert(0, PLUGIN)
sys.path.insert(0, BENCH)

import piece_metrics as pm  # noqa: E402
from live.audio_io import read_wav  # noqa: E402
from live.endpointing import Endpointer  # noqa: E402
from live.events import FRAME, SR  # noqa: E402
from live.stt import make_stt  # noqa: E402

LEAD_S, TAIL_S = 1.0, 1.6


async def run_turn(stt, ep_cfg, wav, noise, speech_end_s):
    ep = Endpointer(ep_cfg)
    lead = noise[: int(LEAD_S * SR)]
    tail = noise[int(LEAD_S * SR): int((LEAD_S + TAIL_S) * SR)]
    x = np.concatenate([lead, wav, tail])
    x = x[: len(x) // FRAME * FRAME]
    se_ms = (LEAD_S + speech_end_s) * 1000.0
    interims: list = []
    finals: list = []
    tasks = []
    clock = {"ms": 0.0}
    stt.on_interim = lambda t: interims.append((round(clock["ms"], 1), t))

    parts: dict = {}

    async def handle(idx, ted):
        # like the orchestrator's carry: every part of a turn that was cut at a pause is kept, in order
        t0 = time.monotonic()
        parts[idx] = await stt.finalize()
        finals.append({"t_ms": round(clock["ms"], 1), "turn_end_ms": ted, "finalize_ms": round((time.monotonic() - t0) * 1000, 1),
                       "text": " ".join(parts[k] for k in sorted(parts) if parts[k]).strip()})

    t0 = time.monotonic()
    for i in range(0, len(x), FRAME):
        due = t0 + (i + FRAME) / SR
        d = due - time.monotonic()
        if d > 0:
            await asyncio.sleep(d)
        f = x[i:i + FRAME]
        clock["ms"] = (i + FRAME) * 1000.0 / SR
        stt.feed(f)
        for dec in ep.feed(f, clock["ms"]):
            if dec.kind == "speech_start":
                stt.begin()
            else:
                tasks.append(asyncio.create_task(handle(len(tasks), clock["ms"])))
    # wait for outstanding finals (real time keeps ticking for the stamp)
    while tasks and not all(t.done() for t in tasks):
        await asyncio.sleep(0.002)
        clock["ms"] = (time.monotonic() - t0) * 1000.0
    st = getattr(stt, "stats", [])
    ready_ms = round((st[-1]["ready_mono"] - t0) * 1000 - se_ms, 1) if finals and st and "ready_mono" in st[-1] else None
    fin = max(finals, key=lambda f: f["t_ms"]) if finals else None
    if fin is not None:
        fin = {**fin, "text": " ".join(parts[k] for k in sorted(parts) if parts[k]).strip()}
    return {"speech_end_ms": round(se_ms, 1), "final": fin, "finals": finals, "interims": interims,
            "stt_delay_ms": round(fin["t_ms"] - se_ms, 1) if fin else None,
            "text_ready_ms": ready_ms,
            "turn_ends": len(finals)}


async def amain():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--scenarios")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs="*", default=[])
    a = ap.parse_args()
    cfg = json.load(open(a.config)) if a.config else {}
    noise = read_wav(os.path.join(pm.SCEN, "noise_floor_24k.wav"))
    ids = a.scenarios.split(",") if a.scenarios else [s["id"] for s in json.load(open(os.path.join(pm.SCEN, "index.json")))]
    stt = make_stt(cfg.get("stt"))
    await stt.start()
    rows = []
    try:
        for sid in ids:
            sc = pm.scen(sid)
            d = os.path.join(pm.SCEN, sid)
            for t in sc["turns"]:
                wav = read_wav(os.path.join(d, t["audio"]["24k"]["file"]))
                r = await run_turn(stt, cfg.get("endpointing"), wav, noise, t["audio"]["24k"]["speech_end_sample"] / SR)
                hyp = r["final"]["text"] if r["final"] else ""
                w, e, n = pm.wer(t["text"], hyp)
                r.update(sid=sid, turn=t["turn"], ref=t["text"], hyp=hyp, wer=round(w, 4), errors=e, ref_words=n,
                         stats=getattr(stt, "stats", [])[-r["turn_ends"]:] if r["turn_ends"] else [])
                rows.append(r)
                fz = r["final"]["finalize_ms"] if r["final"] else None
                print(f"{sid} t{t['turn']}: wer={w:.3f} delay={r['stt_delay_ms']} finalize={fz} "
                      f"ready={r['text_ready_ms']} ends={r['turn_ends']} interims={len(r['interims'])} {r['stats']} | {hyp}", flush=True)
    finally:
        await stt.close()
    E, N = sum(r["errors"] for r in rows), sum(r["ref_words"] for r in rows)
    summ = {"turns": len(rows), "wer_pooled": round(E / N, 4) if N else None,
            "stt_delay_ms_median": pm.med([r["stt_delay_ms"] for r in rows]),
            "text_ready_ms_median": pm.med([r["text_ready_ms"] for r in rows]),
            "text_ready_ms_max": max([r["text_ready_ms"] for r in rows if r["text_ready_ms"] is not None] or [0]),
            "finalize_ms_median": pm.med([r["final"]["finalize_ms"] for r in rows if r["final"]]),
            "finalize_ms_max": max([r["final"]["finalize_ms"] for r in rows if r["final"]] or [0]),
            "interims_total": sum(len(r["interims"]) for r in rows)}
    calls = getattr(stt, "calls", [])
    if calls:
        ok = [c[1] for c in calls if c[0] == 200]
        summ["http"] = {"requests": len(calls), "non_200": [c for c in calls if c[0] != 200][:20],
                        "ms_p50": pm.med(ok), "ms_p90": float(np.percentile(ok, 90)) if ok else None}
    print("OURS", json.dumps(summ))
    for c in a.compare:
        m = pm.compute("stt", c, use_whisper=False, only=sorted(set(ids)))
        print("BAR ", os.path.basename(c), json.dumps(m["summary"]))
    if a.out:
        json.dump({"summary": summ, "turns": rows}, open(a.out, "w"), indent=2)


if __name__ == "__main__":
    asyncio.run(amain())
