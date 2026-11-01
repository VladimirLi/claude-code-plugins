#!/usr/bin/env python3
"""Record the bar for the brain piece: the realtime voice model, text turns in, audio replies out.

Usage (from the plugin folder):
  .venv/bin/python bench/brain_reference.py [--scenarios s01_quick_fact,...] [--reps 1] [--runs-dir bench/runs]

Needs AZURE_OPENAI_API_KEY and AZURE_OPENAI_RESOURCE_NAME. Writes
bench/runs/<date>-bar-brain-rep<k>/ (run.json with "system": "bar") in the brain
run-folder format of bench/SPEC.md, so blind.py --piece brain finds it.

Per bench/SPEC.md "brain": every user turn of the scenario goes in as text
(conversation.item.create input_text + response.create), with the model's own
earlier replies as history. The reply text is the transcript of its audio
reply. Session instructions are the server default, as in the e2e reference.
For a barge-in turn the previous reply is truncated at the barge-in offset
(conversation.item.truncate, audio_end_ms = offset), the way a real client
does it; transcript.json records the same 2.7 words/s cut ours gets.

Events (t_ms from the scenario start):
  x_turn_submitted   response.create sent
  first_reply_text   first transcript delta
  x_first_sentence   first complete sentence of the transcript (same Chunker as ours)
  x_first_audio      first audio delta (extra, dropped by blind.py)
  x_reply_done       response.done
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time

BENCH = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(BENCH)
sys.path.insert(0, BENCH)
sys.path.insert(0, PLUGIN)
from reference_runner import connect  # noqa: E402
from live.tts import Chunker  # noqa: E402

SCEN = os.path.join(BENCH, "scenarios")
WORDS_PER_S_CUT = 2.7


async def recv_until(ws, kinds, timeout=60):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if m["type"] == "error":
            raise RuntimeError(m)
        if m["type"] in kinds:
            return m


async def run_scenario(sid: str, out: str) -> None:
    sc = json.load(open(os.path.join(SCEN, sid, "scenario.json")))
    os.makedirs(out, exist_ok=True)
    ws, dep, created = await connect()
    await ws.send(json.dumps({"type": "session.update", "session": {
        "type": "realtime", "output_modalities": ["audio"],
        "audio": {"input": {"turn_detection": None}, "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}}))
    await recv_until(ws, {"session.updated"}, 15)
    t0 = time.monotonic()
    ms = lambda: round((time.monotonic() - t0) * 1000.0, 2)
    events, turns = [], []
    prev = None                       # (item_id, text) of the previous reply
    try:
        for t in sc["turns"]:
            n = t["turn"]
            if t.get("barge_in") and prev:
                off = int(t["start"]["offset_ms"])
                k = int(off / 1000.0 * WORDS_PER_S_CUT)
                turns[-1]["text_spoken"] = " ".join(prev[1].split()[:k])
                turns[-1]["interrupted"] = True
                await ws.send(json.dumps({"type": "conversation.item.truncate", "item_id": prev[0],
                                          "content_index": 0, "audio_end_ms": off}))
                await recv_until(ws, {"conversation.item.truncated"}, 15)
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": t["text"]}]}}))
            await recv_until(ws, {"conversation.item.added", "conversation.item.created"}, 15)
            await ws.send(json.dumps({"type": "response.create"}))
            events.append({"t_ms": ms(), "event": "x_turn_submitted", "turn": n})
            turns.append({"role": "user", "turn": n, "script": t["text"], "asr_text": t["text"]})
            ch, text, item_id = Chunker(), "", None
            got_first = got_sent = got_audio = False
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 120))
                ty = m["type"]
                if ty == "error":
                    raise RuntimeError(m)
                if ty == "response.output_item.added":
                    item_id = m["item"]["id"]
                elif ty == "response.output_audio_transcript.delta":
                    d = m["delta"]
                    if not got_first:
                        got_first = True
                        events.append({"t_ms": ms(), "event": "first_reply_text", "turn": n, "text": d})
                    text += d
                    if not got_sent and ch.push(d):
                        got_sent = True
                        events.append({"t_ms": ms(), "event": "x_first_sentence", "turn": n})
                elif ty == "response.output_audio.delta" and not got_audio:
                    got_audio = True
                    events.append({"t_ms": ms(), "event": "x_first_audio", "turn": n})
                elif ty == "response.done":
                    if not got_sent and text.strip():
                        events.append({"t_ms": ms(), "event": "x_first_sentence", "turn": n})
                    events.append({"t_ms": ms(), "event": "x_reply_done", "turn": n})
                    break
            turns.append({"role": "assistant", "reply_to": n, "text_generated": text, "text_spoken": text,
                          "interrupted": False, "fillers": []})
            prev = (item_id, text)
    finally:
        await ws.close()
    events.sort(key=lambda e: e["t_ms"])
    with open(os.path.join(out, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    json.dump({"turns": turns}, open(os.path.join(out, "transcript.json"), "w"), indent=2)


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--first-rep", type=int, default=1)
    ap.add_argument("--runs-dir", default=os.path.join(BENCH, "runs"))
    a = ap.parse_args()
    ids = a.scenarios.split(",") if a.scenarios else [s["id"] for s in json.load(open(os.path.join(SCEN, "index.json")))]
    for k in range(a.first_rep, a.first_rep + a.reps):
        run = os.path.join(a.runs_dir, f"{dt.date.today().isoformat()}-bar-brain-rep{k}")
        os.makedirs(run, exist_ok=True)
        json.dump({"system": "bar", "created": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "config": {"mode": "brain", "deployment": "gpt-realtime", "instructions": "server default",
                              "input": "text turns", "output": "audio (reply text = audio transcript)"}},
                  open(os.path.join(run, "run.json"), "w"), indent=2)
        for sid in ids:
            t = time.monotonic()
            await run_scenario(sid, os.path.join(run, sid))
            print(f"rep{k} {sid}: {time.monotonic() - t:.1f}s", file=sys.stderr, flush=True)
        print(run)


if __name__ == "__main__":
    asyncio.run(amain())
