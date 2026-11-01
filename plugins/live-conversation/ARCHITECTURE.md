# Architecture

The live conversation mode is a streaming pipeline daemon in Python asyncio
(`live/`), started by the `/live` command of the mod (`hooks/register.ts`).
The brain is always Claude Code itself (`claude -p`, persistent, stream-json).

```
 mic ──► audio_io ──frames──► orchestrator ──► stt ──text──► turntaking ──► brain ──deltas──► tts ──pcm──► audio_io sink ──► speaker
                                 │  ▲                                                                         ▲
                                 │  └── endpointing (speech_start / turn_end)                                 │
                                 └───── bargein (user talks over the reply) ──── flush ───────────────────────┘
```

## Rules for builders

- One module per piece. A builder edits **only its own module** and its own
  tests (`tests/test_<module>.py`), plus its section of the config. The
  orchestrator is the only module that imports the others.
- Interfaces below are the contract. Keep them, or change them together with
  the orchestrator owner. New optional methods and config keys are fine.
- Audio everywhere is 24 kHz mono int16 numpy arrays. Mic frames are 20 ms
  (480 samples). Times are ms on the mic sample clock (`live/events.py`
  `Clock.ms`), the clock bench/SPEC.md uses.
- Nothing slow runs in the mic loop. Network, subprocesses and models run in
  tasks or threads (`asyncio.to_thread`).
- Every module takes a `cfg: dict | None` with defaults, so the bench can try
  settings with `--config cfg.json` (`{"endpointing": {...}, "stt": {...}, ...}`).
- Python deps go only into `plugins/live-conversation/.venv`
  (`.venv/bin/pip install -r requirements.txt`), and into `requirements.txt`.
- Run `.venv/bin/python -m unittest discover -s tests` before handing back.

## Modules

| Piece | File | Owns | v0 |
| --- | --- | --- | --- |
| audio_io | `live/audio_io.py` | mic, speaker sink, session clock, file mic for the bench | sox `rec`/`play`; FileAudio plays a scenario in real time |
| endpointing | `live/endpointing.py` | speech start, end of the user's turn | energy VAD, 600 ms silence |
| stt | `live/stt.py` | speech to text per utterance | Azure gpt-4o-mini-transcribe (batch, keep-alive HTTPS), or local whisper |
| brain | `live/brain.py` | the reply text, from Claude Code | persistent `claude -p --input-format stream-json`, haiku, warm-up turn |
| tts | `live/tts.py` | text to speech, and when to speak (chunker) | macOS `say` per sentence |
| bargein | `live/bargein.py` | is the user interrupting the reply? | energy over 200 ms while speaking |
| turntaking | `live/turntaking.py` | respond or not, fillers, merging a resumed turn | respond to any non-empty text |
| orchestrator | `live/orchestrator.py` | wiring, state, system-side SPEC events, transcript | |
| tasks | `live/tasks.py`, `bin/live-task` | handing coding work to the Claude Code session, speaking the result | run folder files, polled |
| mod | `hooks/register.ts` | `/live` start/stop/status, status line, submits handed-off tasks, returns their answers | |
| (shared) | `live/events.py` | `Clock`, `EventLog`, `SR`, `FRAME`, `dbfs` | |

### audio_io

```python
audio.clock                                 # Clock, advanced only by mic frames
async for frame in audio.frames(): ...      # 20 ms int16 frames, real time; ends when audio.done
audio.sink.write(pcm, turn=n, filler=False) -> float   # start ms; placed at max(now, end of queue)
audio.sink.flush(spoken_text=None)          # barge-in: drop queued audio
audio.sink.is_playing() -> bool
audio.turn_hint() -> int | None             # bench turn being fed (FileAudio), None live
audio.done: bool
FileAudio(scenario_dir, log, realtime=True).save(out_dir)   # mic.wav, assistant.wav, timeline.wav
LiveAudio(log)
```
The sink emits `first_reply_audio`, `reply_audio_end`, `playback_stopped`.
FileAudio emits the harness events (`session_start`, `user_speech_start`,
`user_speech_end`, `x_no_reply`, `x_reply_shorter_than_offset`) and follows the
SPEC turn start rules by watching what the sink plays.

### endpointing

```python
ep = Endpointer(cfg)
ep.feed(frame, t_ms) -> list[Decision]      # Decision(kind="speech_start"|"turn_end", t_ms, source)
ep.on_transcript(text, final)               # hint from stt, for semantic endpointing
ep.reset()
```
At most one `turn_end` per `speech_start`. The orchestrator logs each
`turn_end` as `turn_end_detected` with its `source`.

### stt

```python
stt = make_stt(cfg)                         # cfg["kind"]: "azure" | "whisper"
await stt.start()                           # warm connections/models
stt.feed(frame)                             # every frame, always
stt.begin()                                 # utterance starts (keeps preroll_ms before)
stt.on_interim = fn(text)                   # optional, streaming STT
text = await stt.finalize()                 # final text since begin()
await stt.close()
```

### brain

```python
brain = Brain(cfg)                          # model, cwd, tools, system_prompt, warm
await brain.start()
async for delta in brain.respond(text, interrupted=Interrupted(spoken_text) | None): ...
await brain.cancel()                        # stream-json interrupt control request
await brain.close()
```
Conversation history lives in the claude process. Replies must come from
Claude Code; other models may not write reply text.

### tts

```python
Chunker().push(delta) -> list[str]; .flush() -> list[str]   # what to speak now
tts = make_tts(cfg)
await tts.start()
async for pcm in tts.synthesize(text): ...  # 24 kHz int16 chunks; concurrent calls allowed
await tts.close()
```

### bargein

```python
bi = BargeIn(cfg)
bi.feed(frame, t_ms, assistant_speaking) -> "barge_in" | "duck" | None
bi.on_transcript(text)
bi.reset()
```
`barge_in`: the orchestrator flushes the sink with the spoken text so far,
cancels the reply, and tells the brain what the user heard. `duck` is reserved
for backchannels (v0 never returns it).

### turntaking

```python
tt = TurnTaking(cfg)
tt.decide(TurnContext(transcript, assistant_was_speaking, last_reply)) -> Action(kind="respond"|"ignore", filler=None)
tt.on_user_resumed(ctx) -> "merge" | "new"
```

### orchestrator

```python
orch = Orchestrator(audio, log, build(cfg), cfg)
await orch.start(); await orch.run(); orch.transcript(scripts); await orch.close()
```
Writes `turn_end_detected`, `final_transcript`, `first_reply_text`,
`barge_in_detected`. Events of a reply carry the user turn it answers. If the
user starts speaking again before any reply audio, the reply is cancelled and
the two parts are merged into one turn.

e2e wiring (see the module docstring): `build()` applies `E2E_OVERLAY` under each
module section (a key in the config still wins), today the on-device endpointer
text probe. The filler the policy would pick is predicted from interim text and
synthesized while the user talks, then played at the turn end before the final
transcript (`x_filler` source `early`); a filler that is only heard does not
count as content, so talking over it merges the turn. The brain is told which
filler opened the reply. Content onset after a filler is logged as a second
`first_reply_audio` with `filler: false`. `audio.reply_active` tells FileAudio a
reply is still in flight, so `assistant_audio_end` waits for it, as for the bar.

### tasks and the mod

The voice brain is small and fast. Real coding work goes to the person's own
Claude Code session, and the result is spoken when it is done:

```
brain (Bash) ── bin/live-task "task" ──► <run>/tasks/<n>.request.json
daemon (TaskBridge.poll) ── stderr event task_handoff ──► mod ── $.prompt.submit ──► session turn
session turn ends ── mod (turn.complete) ──► <run>/tasks/<n>.result.json
daemon ── orch.announce(result_prompt) ──► brain speaks the outcome
```

```python
tasks.request(run_dir, text) -> int         # what bin/live-task does
tasks.TaskBridge(run_dir, log).run(on_result)
tasks.handoff_prompt(cmd)                   # brain cfg "prompt_extra" (only with --run-dir)
orch.announce(prompt)                       # a brain turn played once the conversation is free
```

`python -m live --run-dir DIR` turns this on (the mod passes
`$TMPDIR/claude-live-conversation/<session id>`), writes `DIR/daemon.pid`, and
stops cleanly on SIGTERM. The mod binds a task to its turn by the
`[live voice task N]` mark in the prompt. Mod tests: `claude plugin test .`.

### recorder and review

`live/recorder.py` keeps every live session: `events.jsonl` (each event with its wall-clock time),
`audio.wav` (stereo, left mic, right speaker, sample-aligned with the event clock) and `session.json`.
The orchestrator logs each barge-in decision as `x_bargein` (decision, rule, onset) and each finished or
cut reply as `x_reply_text`. `/live feedback` appends notes to the session's `feedback.jsonl`.
`bin/live-review` turns a session into `report.md` and `clips/`: per barge-in the rule, mic and speaker
level, echo correlation and what the user said after (nothing, or a backchannel, marks it as probably
false); per turn the wait to the first sound and to the answer; the fillers; and each note with the
events before it.

## Bench

| File | What it does |
| --- | --- |
| `bench/run_ours.py` | Runs ours: `--mode e2e` (scenario WAVs through the whole pipeline in real time), `--mode brain` (texts into the brain), `--mode tts` (texts into the TTS). Writes SPEC run folders under `bench/runs/`. |
| `bench/piece_metrics.py` | Metrics per piece from a SPEC run folder; the same code for both sides. |
| `bench/blind.py` | `--piece <key> --round <n> --ours-label A\|B --out <dir> --mapping-out <file>`: runs ours for the piece, finds the cached reference, writes anonymized `A/` and `B/` with `metrics.json` each, and the mapping only to `--mapping-out`. |
| `bench/score.py` | End-to-end scoring (`score.json`). |

Piece to run mode and scenarios: endpointing, stt, e2e: e2e on all 13;
bargein: e2e on s08, s09, s13; turntaking: e2e on s07, s08, s09, s10, s11,
s13; brain: brain on all; tts: tts on all texts.
