#!/usr/bin/env python3
"""Record the bar: the OpenAI realtime voice model on Azure, on every bench scenario.

Usage (from the plugin folder):
  .venv/bin/python bench/reference_runner.py [--scenarios s01_quick_fact,...] [--runs 3]
      [--first-run 1] [--parallel 3] [--vad semantic_vad|server_vad] [--out bench/reference]

Needs AZURE_OPENAI_API_KEY and AZURE_OPENAI_RESOURCE_NAME. Python deps: websockets, numpy.

Output: <out>/<scenario>/run<k>/ in the run-folder format of bench/SPEC.md
(assistant.wav, mic.wav, timeline.wav, events.jsonl, transcript.json), plus
server_events.log (every server event except audio payloads, with wall time and
session clock; dropped by blind.py because it names the model). It also writes
bench/runs/<date>-bar-rep<k>/ (run.json + one symlink per scenario) so score.py
and blind.py can use the bar like any other run.

How it measures (see SPEC "Session clock"):
- The mic is fed in real time: 20 ms chunks of 480 samples at 24 kHz. Chunk k
  holds samples [480k, 480k+480) and is sent at wall time t0 + (k+1)*20 ms, as a
  real microphone delivers it only once it is captured. Session clock
  t_ms = (wall - t0) * 1000, which equals the mic sample clock.
- Assistant audio goes to a simulated speaker: each delta is written at
  max(clock at arrival, end of audio already queued). On barge-in
  (input_audio_buffer.speech_started while reply audio is queued) the speaker is
  flushed and conversation.item.truncate is sent, as a real client does.
- Voice-to-voice latency is then measured acoustically by score.py on
  assistant.wav from the ground-truth speech end sample. Network time to Azure
  and the 20 ms mic chunking are both inside the number.
"""
import argparse, asyncio, base64, datetime, json, os, statistics, subprocess, sys, time, wave
import numpy as np
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 24000
CHUNK = 480                      # 20 ms
FRAME = 240                      # 10 ms analysis frame
ONSET_DB, QUIET_DB = -45.0, -50.0
API_VERSION = "v1"               # GA realtime API: /openai/v1/realtime?model=<deployment>
DEPLOYMENTS = ["gpt-live-1", "gpt-realtime"]


def read_wav(path):
    with wave.open(path) as w:
        assert w.getframerate() == SR and w.getsampwidth() == 2 and w.getnchannels() == 1, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(np.asarray(x, dtype="<i2").tobytes())


def frame_db(x):
    m = len(x) // FRAME
    if m == 0:
        return np.zeros(0)
    f = x[: m * FRAME].astype(np.float64).reshape(m, FRAME) / 32768.0
    return 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-12)


async def connect():
    """Open a realtime WebSocket, trying gpt-live-1 first. Returns (ws, deployment, session.created)."""
    res, key = os.environ["AZURE_OPENAI_RESOURCE_NAME"], os.environ["AZURE_OPENAI_API_KEY"]
    errors = []
    for dep in DEPLOYMENTS:
        url = f"wss://{res}.openai.azure.com/openai/v1/realtime?model={dep}"
        try:
            ws = await websockets.connect(url, additional_headers={"api-key": key}, open_timeout=15,
                                          max_size=None, ping_interval=20)
        except websockets.InvalidStatus as e:
            errors.append(f"{dep}: HTTP {e.response.status_code} {bytes(e.response.body or b'')[:200]!r}")
            continue
        first = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if first.get("type") == "session.created":
            return ws, dep, first
        errors.append(f"{dep}: {first}")
        await ws.close()
    raise RuntimeError("no realtime deployment worked: " + "; ".join(errors))


class Session:
    def __init__(self, scen_dir, sid, vad, out_dir):
        self.sid, self.vad, self.out = sid, vad, out_dir
        self.sc = json.load(open(os.path.join(scen_dir, sid, "scenario.json")))
        self.noise = read_wav(os.path.join(scen_dir, "noise_floor_24k.wav"))
        self.turns = []
        for t in self.sc["turns"]:
            self.turns.append({**t, "pcm": read_wav(os.path.join(scen_dir, sid, t["audio"]["24k"]["file"])),
                               "start_sample": None})
        cap = 240 * SR                                   # hard cap: 4 minutes
        self.mic = np.zeros(cap, dtype=np.int16)
        self.spk = np.zeros(cap + 120 * SR, dtype=np.int16)
        self.fed = 0                                     # mic samples sent
        self.queued_end = 0                              # speaker: end of queued audio (samples)
        self.events, self.raw = [], []
        self.responses = {}                              # response id -> dict
        self.resp_order = []
        self.active_responses = set()
        self.items_turn = {}                             # input item id -> turn
        self.asr = {}                                    # input item id -> text
        self.vad_speaking = False
        self.speech_stopped_turns = set()
        self.max_send_lag_ms = 0.0
        self.t0 = None
        self.done = asyncio.Event()

    # ---- clock -------------------------------------------------------------
    def now_ms(self):
        return (time.perf_counter() - self.t0) * 1000.0

    def now_sample(self):
        return int(self.now_ms() * SR / 1000)

    def ev(self, t_ms, name, turn, **extra):
        self.events.append({"t_ms": round(t_ms, 2), "event": name, "turn": turn, **extra})

    def current_turn(self):
        cur = 1
        for t in self.turns:
            if t["start_sample"] is not None and t["start_sample"] <= self.fed:
                cur = t["turn"]
        return cur

    def speech_end_sample(self, t):
        return t["start_sample"] + t["audio"]["24k"]["speech_end_sample"]

    # ---- turn scheduling (SPEC "Turn start rules") ------------------------
    def schedule(self):
        nxt = next((t for t in self.turns if t["start_sample"] is None), None)
        if nxt is None:
            return
        st = nxt["start"]
        now = self.fed
        if st["anchor"] == "session_start":
            nxt["start_sample"] = int(st["offset_ms"] * SR / 1000)
            return
        ref = self.turns[st["turn"] - 1]
        if ref["start_sample"] is None or now < self.speech_end_sample(ref):
            return
        se = self.speech_end_sample(ref)
        off = int(st["offset_ms"] * SR / 1000)
        upto = max(self.queued_end, now)
        db = frame_db(self.spk[:upto])
        f0 = se // FRAME + 1
        loud = np.nonzero(db[f0:] > ONSET_DB)[0]
        timeout = now > se + 15 * SR
        if st["anchor"] == "assistant_audio_start":
            if len(loud):
                onset = (f0 + int(loud[0])) * FRAME
                nxt["start_sample"] = max(onset + off, now)
                nxt["x_anchor_sample"] = onset
            elif timeout:
                nxt["start_sample"] = now
                self.ev(now * 1000 / SR, "x_no_reply", ref["turn"])
            return
        # assistant_audio_end: reply heard, nothing more queued or generating, 300 ms below -50 dBFS.
        if len(loud) and not self.active_responses and self.queued_end <= now:
            dbn = db[: now // FRAME]
            last_loud = f0 + int(loud[-1])
            above_quiet = np.nonzero(dbn[f0:] > QUIET_DB)[0]
            quiet_from = (f0 + int(above_quiet[-1]) + 1) * FRAME if len(above_quiet) else se
            if now - quiet_from >= int(0.3 * SR):
                end = (last_loud + 1) * FRAME
                nxt["start_sample"] = max(end + off, now)
                nxt["x_anchor_sample"] = end
        elif not len(loud) and timeout:
            nxt["start_sample"] = now
            self.ev(now * 1000 / SR, "x_no_reply", ref["turn"])

    def mic_chunk(self, s0):
        out = self.noise[np.arange(s0, s0 + CHUNK) % len(self.noise)].copy()
        for t in self.turns:
            a = t["start_sample"]
            if a is None:
                continue
            n = len(t["pcm"])
            lo, hi = max(s0, a), min(s0 + CHUNK, a + n)
            if lo < hi:
                out[lo - s0:hi - s0] = t["pcm"][lo - a:hi - a]
                if lo == a:
                    self.on_turn_start(t)
        return out

    def on_turn_start(self, t):
        a = t["start_sample"]
        au = t["audio"]["24k"]
        self.ev((a + au["speech_start_sample"]) * 1000 / SR, "user_speech_start", t["turn"])
        self.ev((a + au["speech_end_sample"]) * 1000 / SR, "user_speech_end", t["turn"])
        if t["start"]["anchor"] == "assistant_audio_start":
            ref = self.turns[t["start"]["turn"] - 1]
            # Reply ended before the offset: SPEC says log it (barge-in test failed, re-run).
            reply_ended = not self.active_responses and self.queued_end <= a and \
                any(r["turn"] == ref["turn"] for r in self.responses.values())
            if reply_ended:
                self.ev(a * 1000 / SR, "x_reply_shorter_than_offset", ref["turn"])

    def end_reached(self):
        if any(t["start_sample"] is None or self.fed < t["start_sample"] + len(t["pcm"]) for t in self.turns):
            return False
        last = self.turns[-1]
        se = self.speech_end_sample(last)
        if self.fed > se + 60 * SR:
            return True
        if self.vad_speaking or self.active_responses or self.queued_end > self.fed:
            return False
        if last["turn"] not in self.speech_stopped_turns:
            return False                        # turn detection still holding the last turn
        db = frame_db(self.spk[: self.fed])
        loud = np.nonzero(db > ONSET_DB)[0]
        last_audible = (int(loud[-1]) + 1) * FRAME if len(loud) else 0
        return self.fed >= max(last_audible, se) + 3 * SR

    # ---- speaker -----------------------------------------------------------
    def play(self, rid, pcm, arrive):
        r = self.responses.get(rid)
        if r is None:
            return
        r["recv_samples"] = r.get("recv_samples", 0) + len(pcm)
        if r["flushed"]:
            return
        pos = max(arrive, self.queued_end)
        n = len(pcm)
        self.spk[pos:pos + n] = pcm
        self.queued_end = pos + n
        if r["first_pos"] is None:
            r["first_pos"] = pos
            self.ev(pos * 1000 / SR, "first_reply_audio", r["turn"], filler=False)
        r["segments"].append((pos, n))
        r["end_pos"] = pos + n

    @staticmethod
    def spoken(r):
        """Words heard before the stop, estimated from the share of generated audio that was played."""
        words = (r["transcript"] or "").split()
        frac = r["played_samples"] / max(1, r.get("recv_samples", 0))
        return " ".join(words[:int(round(len(words) * min(1.0, frac)))])

    def flush(self, at):
        """Barge-in: drop everything queued after `at` and tell the server how much was heard."""
        if self.queued_end <= at:
            return
        self.spk[at:self.queued_end] = 0
        for rid in self.resp_order:
            r = self.responses[rid]
            if r["first_pos"] is None or r["flushed"] or r["end_pos"] <= at:
                continue
            played = sum(max(0, min(n, at - p)) for p, n in r["segments"])
            total = sum(n for _, n in r["segments"])
            r["flushed"] = True
            r["played_samples"] = played
            r["queued_samples_at_stop"] = total
            r["spoken_est"] = self.spoken(r)
            r["stop_pos"] = at
            self.ev(at * 1000 / SR, "playback_stopped", r["turn"], spoken_text=r["spoken_est"])
            if r.get("item_id"):
                self.pending_truncate.append({"type": "conversation.item.truncate", "item_id": r["item_id"],
                                              "content_index": 0, "audio_end_ms": int(played * 1000 / SR)})
        self.queued_end = at

    # ---- main --------------------------------------------------------------
    async def run(self):
        ws, dep, created = await connect()
        self.deployment = dep
        self.pending_truncate = []
        sess = created["session"]
        td = {"type": "semantic_vad", "eagerness": "auto"} if self.vad == "semantic_vad" else \
             {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300, "silence_duration_ms": 500}
        await ws.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime",
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": SR},
                                "transcription": {"model": "gpt-4o-mini-transcribe"},
                                "turn_detection": td},
                      "output": {"format": {"type": "audio/pcm", "rate": SR}}}}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m["type"] == "session.updated":
                self.session_cfg = m["session"]
                break
            if m["type"] == "error":
                raise RuntimeError(f"session.update failed: {m}")
        self.default_instructions = sess.get("instructions")
        self.t0 = time.perf_counter()
        self.t0_unix = time.time()
        self.ev(0.0, "session_start", 1)
        recv = asyncio.create_task(self.receiver(ws))
        try:
            await self.feeder(ws)
        finally:
            recv.cancel()
            await ws.close()
        self.write()

    async def feeder(self, ws):
        k = 0
        while True:
            self.schedule()
            s0 = k * CHUNK
            chunk = self.mic_chunk(s0)
            target = self.t0 + (k + 1) * CHUNK / SR
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            lag = (time.perf_counter() - target) * 1000
            self.max_send_lag_ms = max(self.max_send_lag_ms, lag)
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(chunk.tobytes()).decode()}))
            self.mic[s0:s0 + CHUNK] = chunk
            self.fed = s0 + CHUNK
            while self.pending_truncate:
                await ws.send(json.dumps(self.pending_truncate.pop(0)))
            k += 1
            if self.end_reached() or self.fed + CHUNK >= len(self.mic):
                return

    async def receiver(self, ws):
        async for msg in ws:
            t = self.now_ms()
            m = json.loads(msg)
            ty = m.get("type", "")
            if ty not in ("response.output_audio.delta", "response.audio.delta"):
                self.raw.append({"wall_unix": round(self.t0_unix + t / 1000, 4), "t_ms": round(t, 2), **m})
            else:
                self.raw.append({"t_ms": round(t, 2), "type": ty, "response_id": m.get("response_id"),
                                 "bytes": len(m.get("delta", "")) * 3 // 4})
            turn = self.current_turn()
            arrive = int(t * SR / 1000)
            if ty == "input_audio_buffer.speech_started":
                self.vad_speaking = True
                self.ev(t, "x_vad_speech_started", turn, audio_start_ms=m.get("audio_start_ms"))
                if self.queued_end > arrive:
                    self.ev(t, "barge_in_detected", turn)
                    self.flush(arrive)
            elif ty == "input_audio_buffer.speech_stopped":
                self.vad_speaking = False
                self.speech_stopped_turns.add(turn)
                self.ev(t, "turn_end_detected", turn, source=self.vad, audio_end_ms=m.get("audio_end_ms"))
            elif ty == "input_audio_buffer.committed":
                self.items_turn[m.get("item_id")] = turn
                self.ev(t, "x_committed", turn)
            elif ty == "conversation.item.input_audio_transcription.completed":
                iid = m.get("item_id")
                self.asr[iid] = m.get("transcript", "")
                self.ev(t, "final_transcript", self.items_turn.get(iid, turn), text=m.get("transcript", ""))
            elif ty == "conversation.item.input_audio_transcription.delta":
                pass
            elif ty == "response.created":
                rid = m["response"]["id"]
                self.responses[rid] = {"turn": turn, "first_pos": None, "end_pos": None, "segments": [],
                                       "transcript": "", "flushed": False, "audio_done": False,
                                       "status": None, "item_id": None, "created_ms": t, "first_text": False}
                self.resp_order.append(rid)
                self.active_responses.add(rid)
                self.ev(t, "x_response_created", turn)
            elif ty == "response.output_item.added":
                r = self.responses.get(m.get("response_id"))
                if r is not None and r["item_id"] is None:
                    r["item_id"] = m.get("item", {}).get("id")
            elif ty in ("response.output_audio.delta", "response.audio.delta"):
                pcm = np.frombuffer(base64.b64decode(m["delta"]), dtype="<i2")
                self.play(m.get("response_id"), pcm, arrive)
            elif ty in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                r = self.responses.get(m.get("response_id"))
                if r is not None:
                    r["transcript"] += m.get("delta", "")
                    if not r["first_text"]:
                        r["first_text"] = True
                        self.ev(t, "first_reply_text", r["turn"], text=m.get("delta", ""))
            elif ty in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                r = self.responses.get(m.get("response_id"))
                if r is not None:
                    r["transcript"] = m.get("transcript", r["transcript"])
            elif ty in ("response.output_audio.done", "response.audio.done"):
                r = self.responses.get(m.get("response_id"))
                if r is not None:
                    r["audio_done"] = True
            elif ty == "response.done":
                rid = m["response"]["id"]
                r = self.responses.get(rid)
                self.active_responses.discard(rid)
                if r is not None:
                    r["status"] = m["response"].get("status")
                    if r["end_pos"] is not None and not r["flushed"]:
                        self.ev(r["end_pos"] * 1000 / SR, "reply_audio_end", r["turn"])
                    if r["status"] != "completed":
                        self.ev(t, "x_response_" + str(r["status"]), r["turn"])
            elif ty == "error":
                self.ev(t, "x_server_error", turn, message=m.get("error", {}).get("message"))

    def write(self):
        os.makedirs(self.out, exist_ok=True)
        n = self.fed
        mic = self.mic[:n]
        spk = self.spk[:n]
        write_wav(os.path.join(self.out, "mic.wav"), mic)
        write_wav(os.path.join(self.out, "assistant.wav"), spk)
        write_wav(os.path.join(self.out, "timeline.wav"),
                  ((mic.astype(np.int32) + spk.astype(np.int32)) // 2).astype(np.int16))
        self.events.sort(key=lambda e: e["t_ms"])
        with open(os.path.join(self.out, "events.jsonl"), "w") as f:
            for e in self.events:
                f.write(json.dumps(e) + "\n")
        with open(os.path.join(self.out, "server_events.log"), "w") as f:
            for e in self.raw:
                f.write(json.dumps(e) + "\n")
        tr = []
        for t in self.turns:
            asr = " ".join(self.asr[i] for i, tn in self.items_turn.items() if tn == t["turn"] and i in self.asr)
            tr.append({"role": "user", "turn": t["turn"], "script": t["text"], "asr_text": asr.strip()})
            for rid in self.resp_order:
                r = self.responses[rid]
                if r["turn"] != t["turn"]:
                    continue
                if r["first_pos"] is None and not r["transcript"]:
                    continue                    # cancelled before any output
                tr.append({"role": "assistant", "reply_to": t["turn"], "text_generated": r["transcript"],
                           "text_spoken": self.spoken(r) if r["flushed"] else r["transcript"],
                           "interrupted": r["flushed"] or r["status"] == "cancelled", "fillers": []})
        json.dump({"turns": tr}, open(os.path.join(self.out, "transcript.json"), "w"), indent=2)
        meta = {"deployment": self.deployment, "api_version": API_VERSION, "vad": self.vad,
                "t0_unix": self.t0_unix, "max_send_lag_ms": round(self.max_send_lag_ms, 2),
                "session_ms": round(n * 1000 / SR, 1), "instructions": self.default_instructions,
                "session": self.session_cfg}
        json.dump(meta, open(os.path.join(self.out, "run_meta.log"), "w"), indent=2)


def run_one(args, sid, k):
    out = os.path.join(args.out, sid, f"run{k}")
    for attempt in range(3):
        try:
            s = Session(args.scenarios_dir, sid, args.vad, out)
            asyncio.run(s.run())
            bad = [e for e in s.events if e["event"] in ("x_reply_shorter_than_offset", "x_server_error")]
            print(f"{sid} run{k}: ok, {len(s.events)} events, send lag max {s.max_send_lag_ms:.1f} ms"
                  + (f", flags {[e['event'] for e in bad]}" if bad else ""), flush=True)
            return
        except Exception as e:
            print(f"{sid} run{k}: attempt {attempt + 1} failed: {e!r}", flush=True)
            time.sleep(3)


def link_runs(args, runs, sids):
    """bench/runs/<date>-bar-rep<k>/ with run.json and per-scenario symlinks, for score.py/blind.py."""
    date = datetime.date.today().isoformat()
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
    except Exception:
        commit = None
    tag = "bar" if args.vad == "semantic_vad" else "bar-servervad"
    for k in runs:
        d = os.path.join(HERE, "runs", f"{date}-{tag}-rep{k}")
        os.makedirs(d, exist_ok=True)
        meta = None
        for sid in sids:
            src = os.path.join(os.path.abspath(args.out), sid, f"run{k}")
            if not os.path.isdir(src):
                continue
            dst = os.path.join(d, sid)
            if os.path.islink(dst):
                os.unlink(dst)
            os.symlink(os.path.relpath(src, d), dst)
            if meta is None:
                meta = json.load(open(os.path.join(src, "run_meta.log")))
        json.dump({"system": "bar", "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "git_commit": commit,
                   "config": {k2: meta[k2] for k2 in ("deployment", "api_version", "vad", "instructions")} if meta else {}},
                  open(os.path.join(d, "run.json"), "w"), indent=2)


def summarize(args):
    """Score every run with score.py and write <out>/SUMMARY.md (median/p90 per scenario)."""
    sys.path.insert(0, HERE)
    import score
    sids = sorted(d for d in os.listdir(args.out) if os.path.isdir(os.path.join(args.out, d)))
    rows, all_v2v, per_run, meta = [], [], {}, None
    for sid in sids:
        sc = json.load(open(os.path.join(args.scenarios_dir, sid, "scenario.json")))
        runs = sorted(d for d in os.listdir(os.path.join(args.out, sid)) if d.startswith("run"))
        v2v, ep, stt, stop, cut, neg, flags, replies, ovl = [], [], [], [], 0, 0, [], [], 0
        for rk in runs:
            d = os.path.join(args.out, sid, rk)
            if not os.path.exists(os.path.join(d, "events.jsonl")):
                continue
            meta = meta or json.load(open(os.path.join(d, "run_meta.log")))
            res = score.score_scenario(d, sc)
            ev = [json.loads(l) for l in open(os.path.join(d, "events.jsonl"))]
            for t in res.get("turns", []):
                if t.get("false_cutoffs"):
                    cut += t["false_cutoffs"]
                if t["backchannel"]:
                    continue
                if t.get("endpoint_delay_ms") is not None:
                    ep.append(t["endpoint_delay_ms"])
                if t.get("stt_delay_ms") is not None:
                    stt.append(t["stt_delay_ms"])
                v = t.get("v2v_latency_ms")
                if v is None:
                    continue
                if v < 0:
                    neg += 1
                    continue
                v2v.append(v)
                if not t["barge_in"]:
                    all_v2v.append(v)
                    per_run.setdefault(rk, []).append(v)
            us = {e["turn"]: e["t_ms"] for e in ev if e["event"] == "user_speech_start"}
            for t in res.get("turns", []):
                ovl += t.get("overlap_ms", 0) if not (t["barge_in"] or t["backchannel"]) else 0
            for e in ev:
                if e["event"] == "playback_stopped":
                    before = [(x, n) for n, x in us.items() if x <= e["t_ms"]]
                    if before:
                        x, n = max(before)
                        if sc["turns"][n - 1]["barge_in"] or sc["turns"][n - 1]["backchannel"]:
                            stop.append(e["t_ms"] - x)
                        else:
                            flags.append(f"{rk}:stopped_by_own_continuation")
                if e["event"] in ("x_reply_shorter_than_offset", "x_no_reply", "x_server_error"):
                    flags.append(f"{rk}:{e['event']}")
            tr = json.load(open(os.path.join(d, "transcript.json")))
            replies.append(len(" ".join(x.get("text_generated", "") for x in tr["turns"] if x["role"] == "assistant").split()))
        med = lambda xs: f"{statistics.median(xs):.0f}" if xs else "-"
        p90 = lambda xs: f"{np.percentile(xs, 90):.0f}" if xs else "-"
        rows.append(f"| {sid} | {len(runs)} | {med(v2v)} | {p90(v2v)} | {med(ep)} | {med(stt)} | "
                    f"{med(stop)} | {cut} | {neg} | {ovl} | {med(replies)} | {', '.join(flags) or '-'} |")
    run_p50 = [statistics.median(v) for v in per_run.values() if v]
    head = [
        "# Reference recordings: the bar",
        "",
        f"Deployment `{meta['deployment']}` over the Azure OpenAI GA realtime WebSocket "
        f"(`/openai/v1/realtime?model={meta['deployment']}`, api-version `{meta['api_version']}`). "
        "`gpt-live-1` exists on the resource but rejects the realtime protocol "
        "(HTTP 400 `OperationNotSupported`), and the preview `api-version=...-preview` path returns "
        "`realtime_beta_access_required`.",
        f"Turn detection `{meta['vad']}`, input transcription `gpt-4o-mini-transcribe`, default voice "
        f"`{meta['session']['audio']['output']['voice']}`, server default instructions (no custom prompt). "
        "Mic fed in real time in 20 ms chunks; each chunk is sent when its last sample is captured.",
        "",
        f"Generated by `bench/reference_runner.py --summarize` on {datetime.date.today().isoformat()}. "
        "Runs live in `<scenario>/run<k>/`; `bench/runs/<date>-bar-rep<k>/` links them for `score.py` and `blind.py`.",
        "",
        "## Headline (non-barge-in, non-backchannel turns, acoustic, from ground-truth speech end)",
        "",
        f"- Voice-to-voice p50 (pooled, {len(all_v2v)} turns): **{statistics.median(all_v2v):.0f} ms**; "
        f"p90: **{np.percentile(all_v2v, 90):.0f} ms**",
        f"- Median of per-run p50s ({len(run_p50)} runs): **{statistics.median(run_p50):.0f} ms** "
        f"(per run: {', '.join(f'{x:.0f}' for x in run_p50)})",
        "",
        "## Per scenario",
        "",
        "v2v columns include barge-in turns (measured after the interrupted reply stops). "
        "`endpoint` = `speech_stopped` arrival minus ground-truth speech end. `stt` = transcription "
        "completed minus speech end. `stop` = interrupting `user_speech_start` to the client flush "
        "(time the server's `speech_started` arrived), median. `cutoffs` = turn-end decisions inside "
        "the user's speech, summed over runs. `neg` = replies that began before the user finished. "
        "`overlap ms` = assistant audio above -45 dBFS while the user speaks in a non-barge-in turn, summed over runs "
        "(talking over the user after a false cut-off). `stopped_by_own_continuation` = the user's resumed speech "
        "flushed a reply that started at a false cut-off. A negative endpoint p50 means the first turn-end decision "
        "was a false cut-off. "
        "`words` = median words the assistant generated in the whole scenario.",
        "",
        "| scenario | runs | v2v p50 | v2v p90 | endpoint p50 | stt p50 | stop p50 | cutoffs | neg | overlap ms | words | flags |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    notes = os.path.join(args.out, "NOTES.md")          # hand-written observations, appended verbatim
    if os.path.exists(notes):
        rows += ["", open(notes).read().rstrip()]
    open(os.path.join(args.out, "SUMMARY.md"), "w").write("\n".join(head + rows) + "\n")
    print("\n".join(head + rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summarize", action="store_true", help="only write <out>/SUMMARY.md from existing runs")
    ap.add_argument("--scenarios-dir", default=os.path.join(HERE, "scenarios"))
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--first-run", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--vad", default="semantic_vad", choices=["semantic_vad", "server_vad"])
    ap.add_argument("--out", default=os.path.join(HERE, "reference"))
    args = ap.parse_args()
    if args.summarize:
        return summarize(args)
    sids = [s["id"] for s in json.load(open(os.path.join(args.scenarios_dir, "index.json")))]
    if args.scenarios:
        sids = [s for s in sids if s in args.scenarios.split(",")]
    runs = list(range(args.first_run, args.first_run + args.runs))
    jobs = [(sid, k) for k in runs for sid in sids]
    # One process per session so timing of one session never waits on another's work.
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=args.parallel) as ex:
        list(ex.map(run_one, [args] * len(jobs), [j[0] for j in jobs], [j[1] for j in jobs]))
    link_runs(args, runs, sids)


if __name__ == "__main__":
    main()
