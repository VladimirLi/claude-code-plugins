# Live conversation benchmark

This file is the contract between builders, the harness and critics. Every
run of either system ("ours" or "the bar") must produce the folder format below,
so one scorer and one blind procedure serve every piece.

- The bar: OpenAI's live voice model through the Azure OpenAI realtime API
  (deployment `gpt-live-1` if it speaks the realtime protocol, else
  `gpt-realtime`), default voice, no custom instructions, `semantic_vad`
  turn detection, input transcription `gpt-4o-mini-transcribe`.
- Ours: the live-conversation plugin. The brain is always Claude Code
  (`claude -p`).
- Target: voice-to-voice latency p50 under 500 ms, and the critic picks ours
  blind on each piece.

## Files in `bench/`

| File | What it is |
| --- | --- |
| `scenarios.src.json` | Source of truth: scenario text, pauses, turn start rules. Edit this, then rebuild. |
| `make_scenarios.py` | Rebuilds `scenarios/` with macOS `say` + sox. Deterministic. `python3 bench/make_scenarios.py` |
| `scenarios/<id>/scenario.json` | Built scenario: per-turn text, ground-truth sample indices, start rule. |
| `scenarios/<id>/turn_NN_24k.wav`, `turn_NN_16k.wav` | User audio for turn NN, PCM16 mono. |
| `scenarios/noise_floor_{24k,16k}.wav` | 10 s of the same mic noise floor (-65 dBFS RMS) that is in the turn WAVs. Loop it to fill gaps between turns. |
| `scenarios/index.json` | List of scenarios with turn counts and speech lengths. |
| `tts_texts.json` | Fixed reply texts for the `tts` piece. |
| `score.py` | Validates a run folder and computes end-to-end metrics into `score.json`. `python3 bench/score.py <run_dir>` |
| `blind.py` | Makes a blind A/B folder for one piece: runs ours, finds the cached bar run, anonymizes both, writes `metrics.json` per side. `blind.py --piece <key> --round <n> --ours-label A\|B --out <dir> --mapping-out <file>` (see "Piece run folders") |
| `run_ours.py` | Runs ours (`--mode e2e\|brain\|tts`) into `bench/runs/<run_id>/`. |
| `piece_metrics.py` | Metrics per piece from a run folder, same code for both sides. `python3 bench/piece_metrics.py <piece> <run_dir>` |

The user voice is macOS `Samantha` (s06 uses `Daniel`) at 180 wpm. No
Enhanced or Premium voice is installed on this Mac; if one is installed later,
set `default_voice` in `scenarios.src.json` and rebuild. A rebuild changes the
ground truth, so never compare runs made on different builds.

## Scenarios

13 scenarios. "Next turn starts" says when the harness starts feeding that turn.

| id | kind | User turns | Next turn starts | Stresses |
| --- | --- | --- | --- | --- |
| s01_quick_fact | quick_fact | "What's the capital of Australia?" | - | Raw latency, short answer |
| s02_chitchat | chitchat | "Hey, how's it going? I just got back from a really long walk and I'm kind of wiped out." | - | Register, warmth, brevity |
| s03_repo_what | claude_code_request | "So what does this repo actually do?" | - | Tool use while staying fast and spoken |
| s04_run_tests | claude_code_request | "Can you run the tests and tell me if anything's broken?" | - | Instant acknowledgement, slow action, spoken report |
| s05_um_pause | mid_utterance_pause | "I want to, um, [1230 ms] change the way the, uh, [927 ms] the login page handles errors." | - | No cut-in at filler pauses |
| s06_long_ramble | long_turn | ~26 s ramble, 5 clause pauses of 410-730 ms, ends "What would you do?" | - | Long-turn endpointing and STT, concise reply |
| s07_followup_chain | multi_turn | "What's the difference between a process and a thread?" / "Okay, so which one is cheaper to create?" / "Huh. And why is that?" | 600 ms after previous reply audio ends | Context, turn gaps |
| s08_bargein_early | barge_in | "Tell me about the history of the Roman Empire." / "Actually, just give me the short version." | turn 2: 1500 ms after reply 1 audio starts | Fast stop, follow new request |
| s09_bargein_late | barge_in | "Can you explain how git rebase works?" / "Wait, wait, how is that different from merge?" | turn 2: 4000 ms after reply 1 audio starts | Stop, then build on what was already said |
| s10_correction | correction | "How do I undo my last commit?" / "No, I meant undo it but keep the changes." | 500 ms after reply 1 audio ends | Drop the wrong reading, no long apology |
| s11_silence_yeah | silence_then_short | "I'm thinking about renaming the main branch to trunk. Is that a bad idea?" / "Yeah." | 3500 ms after reply 1 audio ends | No nagging in silence, one-word turn |
| s12_incomplete_pause | mid_utterance_pause | "I was wondering if [1530 ms] you could check what the readme says about installing." | - | Semantic endpointing, no filler cue |
| s13_backchannel | backchannel | "Walk me through how a hash map works." / "Mm-hmm." | turn 2: 2500 ms after reply 1 audio starts | Backchannel is not an interruption |

Exact pause lengths are in each `scenario.json` (`pauses[].silence_ms`); they
are slightly longer than the scripted values because the synthesiser's decay
is trimmed at the speech threshold.

### scenario.json

```jsonc
{
  "id": "s05_um_pause", "kind": "mid_utterance_pause", "description": "...",
  "turns": [{
    "turn": 1,
    "text": "I want to, um, change the way the, uh, the login page handles errors.",  // STT reference
    "segments": [{"text": "I want to, um,", "speech_start_sample": 4800, "speech_end_sample": 35881, "speech_start_ms": 200.0, "speech_end_ms": 1495.042}, ...],
    "pauses":   [{"after_segment": 0, "silence_start_ms": 1495.042, "silence_end_ms": 2725.042, "silence_ms": 1230.0, ...}, ...],
    "start": {"anchor": "session_start", "offset_ms": 1000},
    "barge_in": false, "backchannel": false,
    "audio": {
      "24k": {"file": "turn_01_24k.wav", "sample_rate": 24000, "num_samples": 174960, "speech_start_sample": 4800, "speech_end_sample": 167040},
      "16k": {"file": "turn_01_16k.wav", "sample_rate": 16000, "num_samples": 116640, "speech_start_sample": 3200, "speech_end_sample": 111360}
    },
    "speech_start_ms": 200.0, "speech_end_ms": 6960.0   // relative to the start of the turn WAV
  }]
}
```

Ground truth "user stopped speaking" = `audio.<rate>.speech_end_sample`: one
past the last sample of the clean speech whose magnitude exceeds -48 dBFS,
measured before the noise floor is added. The 16 kHz index is the 24 kHz index
scaled by 2/3 (checked: within 2.3 ms of a re-measurement on the 16 kHz file).
Every turn WAV has 200 ms of noise floor before the first word and 300 ms after
the last.

### Turn start rules (`start.anchor`)

| anchor | Turn N starts feeding at |
| --- | --- |
| `session_start` | `offset_ms` after session clock 0. |
| `assistant_audio_end` | `offset_ms` after the reply to turn `turn` stops being audible (last frame above -45 dBFS, then 300 ms below -50 dBFS). If no reply is audible within 15 s of the user's speech end, start anyway and log `x_no_reply`. |
| `assistant_audio_start` | `offset_ms` after the first audible sample of the reply to turn `turn`. Used for barge-in (`barge_in: true`) and backchannel (`backchannel: true`). If the reply ends before the offset, play the turn at the offset anyway and log `x_reply_shorter_than_offset`; the critic treats the scenario as a failed barge-in test for that side and it must be re-run. |

## Session clock

One clock for everything: the **mic sample clock**. `t_ms = mic samples fed
since session start / 24000 * 1000`. The harness feeds the mic stream in real
time, in 20 ms chunks, continuously from session start to session end: turn
WAVs where a turn is playing, looped `noise_floor` elsewhere. Neither system
gets audio faster than real time.

Assistant audio is placed on the same clock by a shared playback sink (the
"speaker"): a chunk is written at `max(clock when the chunk arrived, end of
audio already queued)`. Flushing the sink (barge-in) drops queued audio and
logs `playback_stopped`. Both systems use the same sink, so device latency is
equal and is not counted. Network time to Azure is part of the bar.

The session ends 3 s after the last reply stops being audible, or 60 s after the
last user turn ends, whichever comes first.

## Run folder format

```
bench/runs/<run_id>/
  run.json                    # {"system": "ours"|"bar", "created": ISO8601, "git_commit": "...", "config": {...}}
  <scenario_id>/
    assistant.wav             # 24 kHz mono PCM16. Only assistant audio, full session length, sample 0 = clock 0.
    mic.wav                   # 24 kHz mono PCM16. Exactly what was fed as the mic, same length.
    timeline.wav              # 24 kHz mono PCM16. (mic + assistant) / 2, same length. For listening.
    events.jsonl              # one JSON object per line, see below
    transcript.json           # see below
    piece/                    # only for piece-level runs, see "Piece-level evaluation"
```

`bench/runs/` is not committed. A latency claim needs at least 3 runs of
every scenario (`<run_id>` = `<date>-<system>-rep<k>`); report the median of
the per-run p50s.

### events.jsonl

Each line: `{"t_ms": <float, session clock>, "event": "<name>", "turn": <user turn number the event belongs to>, ...extra}`.

| event | Written by | When | Extra fields |
| --- | --- | --- | --- |
| `session_start` | harness | clock 0 | - |
| `user_speech_start` | harness | turn feed start + `speech_start_sample` | - |
| `user_speech_end` | harness | turn feed start + `speech_end_sample` (ground truth) | - |
| `turn_end_detected` | system | the system decides the user's turn is over (every decision, also wrong ones) | `source` (e.g. `vad`, `semantic`) |
| `final_transcript` | system | final STT text for the turn is available | `text` |
| `first_reply_text` | system | first text of the reply exists (first token from the brain; for the bar, first transcript delta) | `text` |
| `first_reply_audio` | sink | first reply audio chunk written to the sink | `filler: bool` (true if canned/filler audio not from the brain) |
| `reply_audio_end` | sink | last chunk of the reply played out | - |
| `barge_in_detected` | system | the system decides the user is interrupting | - |
| `playback_stopped` | sink | the sink was flushed | `spoken_text`: words heard before the stop, if known |

Custom events are allowed if their name starts with `x_`. Events must not carry
identity fields (`system`, `model`, `provider`, `deployment`, `voice`,
`engine`); `blind.py` strips them anyway.

### transcript.json

```jsonc
{"turns": [
  {"role": "user", "turn": 1, "script": "...", "asr_text": "..."},
  {"role": "assistant", "reply_to": 1, "text_generated": "full reply text",
   "text_spoken": "text actually played before any stop", "interrupted": false,
   "fillers": ["Hmm,"]}
]}
```

## Voice-to-voice latency

For every user turn with `barge_in: false` and `backchannel: false`:

```
v2v_latency_ms = first_reply_audio - user_speech_end
```

- `user_speech_end` = the ground-truth event from the harness.
- `first_reply_audio` is measured **acoustically** on `assistant.wav`: the first
  10 ms frame after `user_speech_end` with RMS above -45 dBFS, refined to the
  first sample in that frame above -45 dBFS. The `first_reply_audio` event is a
  cross-check only; if it differs by more than 20 ms, the acoustic value wins
  and `score.py` warns.
- For a barge-in turn, the search starts after the interrupted reply has
  stopped (`max(user_speech_end, stop)`).
- A negative value means the assistant spoke before the user finished. It is
  not a fast reply: it is a cut-off, and it is counted in
  `replies_before_user_finished`, not in the percentiles.
- Filler audio counts as first audio, because the user hears it. `score.py`
  reports `v2v_latency_ms`; the `turntaking` critic also reports
  `v2v_content_ms` (first audio that is not a filler, from `first_reply_audio`
  events with `filler: false`) and judges whether fillers sound canned.
- Headline numbers: p50 and p90 over all scored turns, across >= 3 runs.

## Blind A/B procedure (every piece)

1. Builder produces a run of ours; the bar run for the same piece and build is
   cached in `bench/runs/` (re-run the bar if the scenarios were rebuilt).
2. Orchestrator runs `bench/blind.py --piece <piece> --round <N> --ours-label <A|B> --out bench/blind/<piece>-r<N> --mapping-out bench/blind/<piece>-r<N>.key.json`.
   The key goes only to the `--mapping-out` file. The critic must not open
   any `*.key.json`, `run.json`, or anything outside its blind folder.
3. Critic (fresh context) measures A and B with the metrics of the piece below,
   writes `bench/blind/<piece>-r<N>/verdict.json`:
   ```json
   {"piece": "tts", "round": 3, "winner": "A|B|tie",
    "metrics": {"A": {...}, "B": {...}},
    "why": "one or two sentences on what decided it",
    "biggest_gap": "the single biggest thing the loser does worse, concrete and measurable"}
   ```
4. Orchestrator unblinds. Ours wins only if the critic named ours as `winner`. A
   tie is a loss. `biggest_gap` goes back to the builder as the next target.

Critics cannot listen; they judge by measurement (timings, transcripts,
acoustic analysis with numpy/sox/ffmpeg, whisper transcription). The user
confirms wins by ear later. Critic rules: no praise, compare numbers, a
metric that is worse than the bar on any scenario must be named.

## Piece-level evaluation

Each piece can be run and judged alone. A piece run uses the same run folder,
plus `piece/<piece>.json` per scenario with the numbers below. Both sides get
exactly the same input.

### endpointing

- Input: each scenario's turn WAVs streamed in real time (16 kHz or 24 kHz).
- Output: `turn_end_detected` events (every decision, including wrong ones).
- Metrics per user turn:
  - `endpoint_delay_ms` = first `turn_end_detected` at or after
    `user_speech_start` minus `user_speech_end`. Lower is better.
  - `false_cutoffs` = decisions between `user_speech_start` and
    `user_speech_end` (inside the turn, mostly at the scripted pauses in s05,
    s06, s12). Any false cut-off is worse than any delay.
  - `missed` = no decision within 3 s after `user_speech_end`.
- Reference: the realtime model's turn detection on the same audio, with
  `semantic_vad` (eagerness `auto`) as the bar. The decision time is the clock
  time when `input_audio_buffer.speech_stopped` arrives. Also run
  `server_vad` (default 500 ms silence) and report it for context.
- Decides: fewer false cut-offs first, then lower median delay.

### stt

- Input: the same turn WAVs, streamed in real time.
- Output: `final_transcript` events (and optional `x_interim_transcript`).
- Metrics per turn:
  - `wer` against `turns[].text` in `scenario.json`. Normalise both sides:
    lowercase, strip punctuation except apostrophes, numbers as words, drop
    fillers (`um`, `uh`, `mm-hmm`, `mhm`, `hmm`, `huh`).
  - `stt_delay_ms` = `final_transcript` minus `user_speech_end`.
- Reference: the realtime session's input transcription
  (`conversation.item.input_audio_transcription.completed` arrival time) on the
  same audio.
- Decides: WER (pooled over all turns) first if the gap is > 2 points, else
  median delay.

### brain

- Input: the user turn texts as text (no audio), with the side's own earlier
  replies as history. For barge-in turns, the history contains the earlier reply
  cut to the words that fit in the barge-in offset at 2.7 words/s, marked as
  interrupted. Same cut for both sides.
- Ours: Claude Code headless in this repository. Bar: the realtime model with
  audio output; its reply text is the `response.audio_transcript` of the reply.
- Output: `transcript.json` and `piece/brain.json` with
  `time_to_first_sentence_ms` (turn submitted to the first complete sentence of
  reply text), `words`, `sentences`, `markdown_or_list_marks` (count of `*`,
  `#`, backticks, `1.`, `-` bullets, URLs and file paths read out as symbols),
  `asks_follow_up` (bool), `uses_context` (bool, for turns 2+).
- Critic reads both transcripts blind and decides which reads more like a
  natural live conversation partner: length fit to the question, spoken
  register, no lists or markdown, sensible follow-up, uses context, gets the
  facts right. Time to first sentence is the tiebreaker and must be named if it
  is worse.

### tts

- Input: every text in `tts_texts.json`, given all at once, and once streamed
  word by word at 4 words/s (to test speaking before the sentence is complete).
- Bar: the realtime model told to say the text verbatim
  (`response.create` with instructions "Say exactly this, nothing else: ...");
  re-run if its transcript has WER > 5% against the text.
- Output: `assistant.wav` per text, `piece/tts.json` per text.
- Metrics per text:
  - `first_audio_ms`: text available to first audible sample.
  - `internal_gaps_ms`: silences > 250 ms inside the reply; flag those not at a
    sentence boundary.
  - Prosody: F0 standard deviation in semitones over voiced frames, and frame
    energy standard deviation in dB. Flat = robotic.
  - `speaking_rate_wpm`: words / voiced duration.
  - Artifacts: fraction of clipped samples (|x| >= 32767), clicks (sample jumps
    > 0.5 full scale), DC offset.
  - Intelligibility: whisper (`base.en`) WER against the text.
- Decides: intelligibility and artifacts are gates (worse than the bar = loss);
  then prosody variation and first-audio latency.

### bargein

- Scenarios: s08, s09 (interruptions), s13 (backchannel).
- Metrics:
  - `stop_delay_ms` = interrupting turn's `user_speech_start` to the time
    assistant audio stays below -50 dBFS for 100 ms (acoustic, from
    `assistant.wav`). Lower is better.
  - Next reply: does it follow the new request (s08: short version; s09:
    rebase vs merge) and avoid repeating what was already spoken
    (`text_spoken` of the interrupted reply)? Does it acknowledge the
    interruption naturally (not "Sure! As I was saying...")?
  - s13: the backchannel must not end the reply. A stop within 1500 ms of the
    "Mm-hmm" followed by a new reply is a false barge-in. A short pause and
    resume is allowed and the pause length is reported.
- Decides: false barge-in on s13 is a loss; then stop delay; then the quality of
  the reply after the interruption.

### turntaking

- Scenarios: all multi-turn ones (s07, s08, s09, s10, s11, s13), run end to end.
- Metrics:
  - Distribution of gaps between user speech end and assistant audio start
    (`v2v_latency_ms`, and `v2v_content_ms` without fillers).
  - Dead air: silent stretches > 1 s inside an assistant reply
    (`score.py` `dead_air_ms`).
  - Overlaps: assistant audio while the user is speaking that is not an
    interrupted reply (`overlap_ms`).
  - s11: anything the assistant says during the 3.5 s silence is nagging and is
    named.
  - Fillers: list every filler; the same filler text or identical waveform
    reused across turns counts as canned.
- Decides: the critic weighs these into "which one feels more like a live
  conversation" and names the single biggest gap.

## End-to-end evaluation

All 13 scenarios, both systems, >= 3 runs each, scored with `score.py`.
The critic gets the blind pair and judges the whole conversation with all
metrics above. Ours must beat the bar blind, and v2v p50 must be under 500 ms.

## Piece run folders

Both sides write the same layout so `blind.py` can pair them. A bar run is any
`bench/runs/<run_id>/` whose `run.json` has `"system": "bar"`; `blind.py` picks
the newest one that has the needed folders.

| Mode | Used by pieces | Folder per | Files |
| --- | --- | --- | --- |
| e2e | endpointing, stt, bargein, turntaking, e2e | scenario id | the full run folder format above |
| brain | brain | scenario id | `transcript.json` (no `mic.wav`), `events.jsonl` with `x_turn_submitted`, `first_reply_text`, `x_first_sentence` (first complete sentence), `x_reply_done`; `t_ms` on any clock that starts per scenario |
| tts | tts | `<text_id>` (all at once) and `<text_id>_stream` (4 words/s) | `assistant.wav` (24 kHz, sample 0 = text available), `events.jsonl` with `x_text_available` at 0 and `first_reply_audio` |

`blind.py` keeps only the SPEC events, the `x_` events named in this table
plus `x_interim_transcript`, `x_no_reply` and `x_reply_shorter_than_offset`,
and only the fields `t_ms`, `event`, `turn`, `text`, `filler`, `spoken_text`.
WAVs are rewritten as plain 24 kHz mono PCM16. Vendor, model and voice names
and file paths in texts become `[redacted]` / `[path]`. If no bar run has the
folders, `blind.py` writes nothing to `--out`, puts ours' metrics in
`--mapping-out` with `"reference_missing": true`, and exits with code 3.
