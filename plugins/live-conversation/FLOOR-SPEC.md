# Floor controller: spec

Status: v6, ready for approval. It went through five rounds from two critics each: a bug walk of the
sweep bugs and invented event orderings, and a design review against the code. Round 5 found every
sweep bug prevented; its rule-interaction gaps are fixed here. Interactions finer than these are left
to the fuzzer, which finds them more reliably than prose review (see "Review history"). Nothing is
built yet.

## Why

The orchestrator decides who has the floor through flags on itself (`carry`, `pending_interrupt`,
`cut_released`, `early`, `prediction`, `finals_pending`, `in_user_turn`, ...) and hold fields on
each `Reply` (`held`, `held_gen`, `held_since`, `held_words`, `released_gen`, `released_ms`,
`content_started`, ...). Three async sources change them: mic frames, STT finals that complete late
and out of order, and the brain and TTS tasks. Delayed checks run as coroutines that poll or wait
(`_unheld`, `_reask_if_no_speech`, `_announce`).

Four agent sweeps confirmed 25 bugs. 19 are races in this floor logic. The other 6 are in
brain.py (6, 7), turntaking.py (9), test_stt.py (13), audio_io.py (18) and stt.py (19). Each floor
fix added a flag, and the next sweep found races between the new flags and the old ones.
`orchestrator.py` grew from 763 lines (commit 1a676d5) to 1,076 in one day.

## What changes

One new module, `live/floor.py`: a pure, synchronous state machine.

    actions = floor.on(event)        # no I/O, no asyncio, no clock reads; each event carries t_ms

- It owns every decision about who has the floor: start, hold, pause, play and stop a reply; turn
  labels; announcements; and the floor-related SPEC events.
- The orchestrator becomes an adapter. It turns mic, endpointer, STT, brain, TTS and sink callbacks
  into events, carries out the actions, and drops every callback whose `rid` is not the current
  reply's (late text or audio of a stopped reply).
- Timers are events. The adapter fires them from the frame loop on `clock.ms`, the mic sample
  clock (events.py:30-41), so they run on the same time base as every other event.
- Two pure helpers are injected, so the controller has no hidden policy and no policy state:
  - `backchannel(text) -> bool`: `turntaking.is_backchannel(normalize(text))`
    (turntaking.py:184-201), with its run logic ("okay, right"). Step 2 widens it: `normalize`
    folds `m+`, `mm+h+m+` and `h+m+` spellings ("Mmmm.", "Hmmm.") to one form, "aha" joins
    `_BACKCHANNELS` (turntaking.py:86-92), and the run cap rises from 4 to 6 words.
    `bargein.is_backchannel` (regex at bargein.py:89-91) and the old path's use at
    orchestrator.py:565 call it too, so there is one test everywhere. test_bargein.py:247-252 must
    still pass.
  - `meaningful(text, played) -> text`: see "Words". Empty means no words. This text goes into the
    question.
- Changes outside the orchestrator, named here so none is a surprise:
  - **STT**: `begin()` returns an utterance id. Interims carry that id. `finalize(utt=None)` returns
    `(text, how)`, with `how` passed through as today (`spec`, `spec_wait`, `new`, `short`,
    `timeout`, `failed`; stt.py:461-508). With `utt` given, `_take` never drops a frozen
    utterance; a slow final is still the right final. With `utt=None` it behaves as today, drop
    included (stt.py:436-446), so the old path keeps working until step 6. `BaseSTT` and
    `WhisperSTT` (stt.py:133, 147) get a small per-utterance store for this. Classes and callers to
    update: `BaseSTT`, `WhisperSTT`, `SpeculativeSTT`, `AzureSTT` (stt.py:172, 553), `FakeSTT`,
    `SlowSTT`, `SeqSTT`, `SlowFirstSTT`, `DelaySTT` (test_orchestrator.py:30-41, 140, 316, 463, 654),
    test_stt.py, test_modules.py:65, and `bench/stt_bench.py:52-74`.
  - **Hallucination list**: a new constant `STT_HALLUCINATIONS` in `live/stt.py`. None exists today.
  - **Brain**: `Interrupted` gets a `question` field next to `spoken_text` (brain.py:137).
    `_message` (brain.py:665-672) then says: `[You were answering: "<question>". The user heard
    only: "<spoken>", then interrupted.]`.
  - **Endpointer**: `Decision` (endpointing.py:63-66) gets `pause_start_ms` for `turn_end`; today it
    is only in `trace` (endpointing.py:456). The adapter feeds `endpointer.on_transcript` with each
    interim and final, as orchestrator.py:334 and 541 do today.
  - **Sink**: pause, play, a gain stage for duck, a `paused` flag, and piece tags (see "Sink").
  - **`first_reply_audio`** moves from `Sink.write` (audio_io.py:105-107) to the controller when the
    floor flag is on (a `Sink` flag turns off its own emit). Update test_modules.py:81 and the copy
    in bench/run_ours.py:300.
  - **Metrics**: `bench/score.py`, `bench/piece_metrics.py` and `bin/live-review` (see "Labels and
    metrics").

## Terms

- **Segment**: one stretch of user sound, from the endpointer's `speech_start` to its `turn_end`.
  Segments and blips share one counter, `g = 1, 2, ...`, in start order. Each segment stores its STT
  utterance id, so interims and finals bind by id, never by call order. Its **speech end** is
  `pause_start_ms` on its `turn_end`.
- **Blip**: a detector `barge_in` while no segment is open and nothing is in `ducked_on`. The
  detector fires about 40 ms after onset and the endpointer's `speech_start` needs 60 ms
  (endpointing.py:70-74, bargein.py:64-65), so most interruptions start as a blip. A blip can only
  happen while `floor(true)` (bargein.py:262-272). If a `speech_start` comes within
  `short_bargein_ms` (800 ms), that segment absorbs the blip and inherits everything the blip did
  (its pause, its place in `ducked_on`). Otherwise the blip resolves with no words. Blips never
  belong to an utterance.
- **Resolved**: a segment with its final, or a blip past `short_bargein_ms`.
- **Words** (`meaningful`), applied to finals and interims alike. Tokens are `normalize(text).split()`.
  1. **Echo**: remove tokens of the reply text played from 0.5 s before the segment's start to now,
     where they match as a run of at least 3 tokens in order, or where the whole text equals one
     played piece ("Sure.", "Hold on."). When `speakers` is true, a text whose tokens all appear in
     that played window is echo too, whatever its length. For the replay of an old recording, use
     the recorded `x_reply_text` of replies whose span (from their `first_reply_audio` to their
     stop or end; orchestrator.py:771 logs no span) overlaps the window.
  2. Remove it if it is in `STT_HALLUCINATIONS` ("You", "Thank you.", "Thanks for watching.",
     "We'll see you next time.", ...), unless an earlier interim of the segment had other words.
  3. Remove hum and hesitation tokens (`_HUMS`, which gains `mmmm` through the same `normalize`
     folding, and `is_hesitation`).
- **Over heard content**: fixed per segment at its `turn_end`. A segment is over heard content if
  any of these holds:
  - reply content audio played between 0.5 s before its start and its speech end,
  - it, or a blip it absorbed, paused or ducked the gate.

  One exception, the **question tail**: the piece playing (or last played, within 1 s) at the
  segment's start ends in "?", and no further content played before the segment's speech end. Then
  the segment is not over heard content. So "Yes." to "Shall I run the tests?" is a new turn even
  when the user starts over the last word, and a rhetorical "Why? Because..." that goes on playing
  does not count as a tail.
- **Not an interruption**: words over heard content for which `backchannel(text)` is true
  ("mm-hmm", "yeah", "go ahead", "makes sense"). They count as no words. Outside heard content, a
  backchannel is words.
- **Utterance**: the user's question that is not yet answered. It is a list of segments, and its
  text is each segment's `meaningful` final, joined in **segment order**. It is cleared when its
  reply ends.
- **Reply**: one brain call and its audio, made for one version of an utterance. It **ends** on
  `drained` after `brain_done` and `tts_done`, on `stop_reply`, or on `brain_error`. Then
  `reply = None`, and every unresolved segment and live blip carries over into the next utterance.
- **Heard content**: reply content audio that has played (not only queued), tentative included.
  Keepalive lines are not content.

## State

All of the controller's state, and nothing else:

    segments:  g -> {kind: segment | blip, utt_id, hint, status: open | ended | final,
                     start_ms, speech_end_ms, text, over_heard: bool | None,
                     caused: none | pause | duck, interim_seen_ms, interim_words, last_interim}
    utterance: {label | None, segs: [g, ...], last_word_end_ms}
    reply:     None | Reply
    last_context: None | Interrupted(spoken, question)       # for the next reply after a stop or error
    played:    [(rid, piece, kind, t_start_ms, t_end_ms, text)]   # echo window, tail, over_heard
    announcements: [{id, prompt, label, state: queued | carried(rid) | heard, tries}]
    timers:    name -> due_ms
    counter, announce_counter                                     # labels are never reused
    speakers:  bool                                               # config, see rule 2

    Reply:
      rid, label, kind: answer | announce, question, context, carries, segs
      started_ms, brain: running | done | failed, tts: running | done
      pieces: [(piece, kind: content | keepalive, text, dur_ms)]
      first_heard_ms | None, heard: none | keepalive | content
      gate:   open
            | paused(on: {g, ...}, cause: hold | barge_in, since_ms)
            | tentative(on: {g, ...})
      ducked_on: {g, ...}, detector_ducked: bool, resumed_ms, nowords_pauses: int
      offset: (piece, ms into piece) of the last played audio

`cause` and `since_ms` are set by the first entry into the pause set and kept until it empties.
"The pause set" below means the set in `paused(on)` or `tentative(on)`. An utterance's label comes
from the `hint` of its first word segment if the bench gave one.

## Events (inputs)

| Event | From | Note |
|---|---|---|
| `speech_start(g, utt_id, hint)` | endpointer + `stt.begin()` | `hint` = `turn_hint()` from the bench, or None |
| `interim(utt_id, text)` | STT | dropped if that segment already has its final |
| `turn_end(g, pause_start_ms)` | endpointer | |
| `final(utt_id, text, how)` | STT | |
| `detector(decision, reason)` | `BargeIn.feed` | `duck`, `barge_in` (reason `open`, `fric`, `long`, `burst`), `resume` |
| `brain_text(rid, delta)`, `brain_done(rid)`, `brain_error(rid)` | brain | |
| `tts_piece(rid, piece, kind, text, dur_ms)` | TTS pipeline | a piece is synthesized and queued at the gate |
| `tts_done(rid)` | TTS pipeline | every piece of the reply synthesized |
| `played(rid, piece, ms)`, `drained(rid)` | sink | `drained`: nothing queued or buffered |
| `task_result(id, prompt)` | TaskBridge | |
| `timer(name)` | adapter | after `set_timer` |
| `end` | session | stop everything |

## Actions (outputs)

| Action | What the adapter does |
|---|---|
| `start_reply(rid, label, question, context, carries)` | start the brain. If a stop is still running (`brain.cancel` can wait up to 5 s, brain.py:551), the brain pool waits as today (brain.py:677-686) |
| `stop_reply(rid)` | cancel the brain and TTS; drop queued and buffered audio |
| `pause(rid)` | stop playback at once; keep every piece's audio; buffer new TTS output; drop a keepalive piece |
| `play(rid, rewind_ms)` | flush the sink, write again from `rewind_ms` before the pause point (moved to a word boundary), then the buffered pieces; then `bargein.reset()` |
| `duck(rid)` / `unduck(rid)` | set the sink's gain |
| `reset_detector()` | `bargein.reset()`, so the detector can fire again |
| `keepalive(rid)` | synthesize the next holding line; it reaches the gate as a `keepalive` piece |
| `set_timer(name, at_ms)` / `cancel_timer(name)` | |
| `floor(assistant: bool)` | the detector's input. True while a reply plays, or is paused or tentative after heard content |
| `emit(name, label, fields)` | SPEC and debug events, including `x_floor` with the reason for every pause, duck and play |

**Rewind** scales with the pause, so a short false pause costs almost nothing:

| Pause length | `rewind_ms` |
|---|---|
| under 1 s | 0: go on from the pause point |
| 1-3 s | 500 |
| over 3 s | to the start of the piece, at most 2,000 |

The word boundary is estimated from the piece's length, because the TTS gives no word timings
(orchestrator.py:836-853 estimates the same way).

## Rules

1. **Before heard content, sound holds.** While the reply has no heard content, a new segment adds
   its id to the gate's pause set (`cause = hold`). Nothing plays until the set is empty or rule 4
   allows a tentative play. Holds never count toward `nowords_pauses` or `resume_guard_ms`.
2. **During heard content, interruptions pause at once.**
   - A new `speech_start` while the gate is paused adds the segment to the pause set. During a
     tentative play it also joins the set, and play continues.
   - **Headphones** (`speakers` false):
     - A detector `barge_in`, any reason, pauses at once (`cause = barge_in`). Today the flush comes
       40-60 ms after onset (`open` needs 2 frames, bargein.py:233), and a pause must be as fast.
     - **Duck only** instead, with `reset_detector`, when: the segment or blip is already in the
       pause set (for example during a tentative play); or the gate played after a no-words pause
       less than `resume_guard_ms` (3 s) ago and no new `speech_start` came since; or the reply
       already had 2 detector pauses that ended with no words (blips count). The segment or blip
       goes into `ducked_on`.
     - While anything is in `ducked_on`, a new detector `barge_in` attaches to it, not a new blip.
     - Detector `duck` sets `detector_ducked` and `resume` clears it.
   - **Speakers** (`speakers` true): detector decisions are logged and otherwise ignored, because on
     speakers the detector hears echo as speech (bargein.py:49-50) and its duck and resume would pump.
   - **Both modes**: an interim with words (non-empty after `meaningful`, and not a backchannel over
     heard content), in a segment over heard content so far, pauses at once (`cause = barge_in`).
     The 2-pause limit and the resume guard never block this.
   - The volume is ducked exactly while `ducked_on` is non-empty or `detector_ducked` is true. When a
     segment or blip in `ducked_on` resolves, it leaves the set.
   - A segment that pauses nothing is still read when its final comes (rule 3).
3. **Finals decide, per segment.** When segment g's final comes, take `meaningful(final)`. Test in
   this order:
   - **No words**, or not an interruption: g leaves the utterance, the pause set and `ducked_on`.
     When the pause set is empty, `play`. Nothing is asked again.
   - **Interim words but a final with no words** (an STT dropout; session issue 4): use the
     segment's last interim text as its final, and go on below.
     *Note (step 3): this test runs first, before "No words"; in the order above it could never apply.*
   - **Words, and g is part of the current reply's question** (it is in `reply.segs`: it started
     before `start_reply`, including segments carried in at an interrupt): **join**, whatever its
     over-heard flag.
   - **Words, g not over heard content, and no reply**: g joins the current utterance, and rule 5
     starts the reply.
   - **Words, g not over heard content, a reply with no heard content, g started within
     `join_gap_ms` (2 s) of the utterance's `last_word_end_ms` (the speech end of its newest word
     segment), and no keepalive played with the gate open**: **join**.
   - **Otherwise**: **interrupt**. The reply stops, and `last_context = Interrupted(spoken,
     question)` (spoken may be ""). A new utterance starts with g and every other unresolved
     segment and live blip, in segment order, under a new label. Its first reply takes
     `last_context`, which is then cleared.

   **Join** means: g is in the utterance and the question is rebuilt. If the question changed, the
   reply stops and a new reply starts on it under the same label (rule 5); if it did not, nothing
   restarts.

   An `announce` reply has no utterance, so a segment with words always interrupts it (and rule 8
   requeues the item).
4. **The cap gives a tentative play.** For either cause, the gate turns `tentative` and plays at the
   later of these two times:
   - `pause_max_ms` (1.5 s) after the newest segment in the pause set started;
   - when every segment in the set has had an interim with no words, or its `turn_end` with no
     interim words.

   An interim or `turn_end` that comes after the cap ends the wait at once. A blip in the set waits
   for `short_bargein_ms`. During a tentative play, an interim with words pauses again at once.
5. **A reply starts on the words known.** A new reply starts as soon as the utterance has at least
   one segment with a word final. Unresolved segments do not delay the brain. The new reply's
   `segs` holds every segment of the utterance, resolved or not, and its gate starts paused on
   every unresolved segment and live blip (rule 1). If one of them later has words, rule 3 joins
   it, in segment order.
6. **One reply, no replays.** At most one reply exists. A stopped reply never plays again. A reply
   restarts only when its question's words change.
7. **Announcements.** A task result is queued, with its own label (`a1`, `a2`, ...).
   - It starts an `announce` reply only when the floor is free: no open segment, no unresolved
     segment, no reply, for `announce_quiet_ms` (its own timer does not count).
   - A user-turn reply carries every queued item.
   - When the carrying reply ends, the item is `heard` only if the reply had `brain_done` and
     `tts_done`, and at least 60% of its content audio played. Otherwise it goes back to `queued`
     with `tries + 1`, and the next try sends `ANNOUNCE_RETRY_NOTE` (orchestrator.py:103) or
     `ANNOUNCE_CARRY_NOTE`, as today.
   - A heard item is never carried again.
8. **Keepalive.** While the brain runs, the gate is open, and nothing has played for
   `keepalive_after_ms` (4.5 s), the controller sends `keepalive`, then again every
   `keepalive_every_ms` (9 s). The 4.5 s timer restarts on every `play`. No keepalive is sent while
   the gate is paused. A keepalive piece that arrives while the gate is paused, or is paused while it
   plays, is dropped, not buffered. When one plays with the gate open, it sets `heard = keepalive`.
   It never counts as content.
9. **Failure and end.** `brain_error` stops the reply and logs it. If its content was heard,
   `last_context = Interrupted(spoken, question)`. Its unresolved segments carry over into the next
   utterance. `end` stops everything.
10. **The detector only hears sound.** `BargeIn.on_transcript` goes. Words are decided only here.

## Labels and metrics

`bin/live-review` sorts events by `t_ms` (line 118), makes one row per `turn_end_detected`
(lines 192-195), and pairs it with the first `first_reply_audio` of that label within 20 s.
`bench/score.py:112-125` matches by label, and score.py:117-120 and piece_metrics.py:152-161 count
`turn_end_detected` inside the user's speech as false cut-offs. `piece_metrics.py:309-310` matches
`playback_stopped` and `barge_in_detected` by time, and line 331 marks `false_barge_in` for any
`playback_stopped` within 1.5 s.

- An utterance gets its label when it first has a word final. The label is the first word segment's
  `hint` if the bench gave one, else `counter + 1`. Labels are never reused. A noise-only utterance
  gets no label.
- **`x_turn_end`** is emitted at every endpointer `turn_end`, at once. The false cut-off metrics in
  score.py and piece_metrics.py move to it, so a joined early cut still counts.
- Each label gets exactly **one** `turn_end_detected`, emitted when the label's first content
  plays, or when its utterance ends with no reply. Its `t_ms` is the `turn_end` of the newest word
  segment before that moment, so it is written out of time order. Readers sort by `t_ms`:
  live-review does, and score.py and piece_metrics.py must.
- `final_transcript` is emitted with the full rebuilt question each time the question changes.
  live-review's "heard" column shows that rebuilt question.
- `first_reply_audio` is emitted once per label, at the first **played** content audio, with
  `filler=False`. A restart under the same label does not emit it again. Keepalive lines never
  emit it.
- A pause emits `barge_in_detected` (for `cause = barge_in`) and `playback_paused`. A pause that
  ends with no words emits `playback_resumed`. A stop emits `playback_stopped`, once.
- piece_metrics: `flush_delay_ms` is measured to `playback_paused` or `playback_stopped`, whichever
  is first. `false_barge_in` is a pause that ends in `playback_resumed`.
- score.py `dead_air_ms` keeps counting paused time: a pause on noise is silence the user hears.
- live-review's barge-in table reads `x_bargein`. The adapter logs it for every detector decision,
  and the controller logs one with `reason = "interim"` for a pause on interim words. The table's
  verdict uses the shared `backchannel` test.
- `transcript.json` keeps its shape (orchestrator.py:1029-1043). `text_spoken` is the text up to the
  furthest played offset. A piece replayed after a rewind counts once.

## Sink: pause and play

One `Sink` class serves `FileAudio` and the replay (`replay_session.py:40,144`). `LiveAudio`
pipes it to sox `play`. `bench/reference_runner.py` drives the reference system, not ours, so it
needs no change.

- `pause()` zeroes the queued samples (as `flush` does, audio_io.py:111) without emitting. The
  adapter keeps every piece's whole audio, so it can rewind into a piece that already started.
  `play()` writes from the rewind point, then the buffered pieces, at the sink's current end. TTS
  output while paused goes to the adapter's buffer.
- A `paused` flag keeps `is_playing()` and `reply_active` True while paused. Today `is_playing()` is
  `queued_end > now` (audio_io.py:121). Without the flag, `FileAudio` would start the next scripted
  turn in the gap (audio_io.py:231, 295), and `Sink.tick` would emit `reply_audio_end`
  (audio_io.py:127-129).
- Duck is a gain (-12 dB) that `Sink.tick` applies to the samples it hands out (audio_io.py:123), so
  the stored samples stay unchanged and unduck is instant.
- Each chunk carries `(rid, piece)`, and the sink reports `played(rid, piece, ms)`. On `LiveAudio`,
  "played" runs ahead of the speaker by the pipe and device buffer, as today.
- `FileAudio` gets an echo option: it mixes the sink's output back into the mic at a set delay and
  level (-35 to -45 dB). Any A/B runs old and new code with the same echo setting.

## Invariants (what the fuzzer checks)

The invariants are checked against each scenario's **ground truth** (which sounds are words, and
what they say), not against the controller's own reasons, so the fuzzer does not check the
controller against itself. `x_floor` reasons are logged to explain failures.

**Quiescence**: no open segment, every segment resolved, no pending timer, no queued event, the
brain and TTS done, and the sink drained.

Checked after every event:

- **I3 Right order.** Every question is its word segments' `meaningful` finals in segment order.
- **I4 One voice.** At no time does more than one reply have audio queued or playing. A stopped
  reply never plays again.
- **I5 No talking over words.** Once a segment has an interim with words, no audio plays until
  that segment's final.
- **I9 Noise does not stop speech.** A segment whose ground truth is no words never stops a reply
  with heard content. When `speakers` is true, a detector decision never pauses one.
- **I10 No stutter.** A reply has at most 2 detector pauses that end with no words.
- **I11 Pause set is live.** The pause set holds only unresolved segments, and blips younger than
  `short_bargein_ms`.
- **I12 No early play from a hold.** A held gate plays only when its pause set is empty or at rule
  4's time.
- **I13 No same-question restart.** Two `start_reply` calls in a row for one label never have the
  same question.

Checked at quiescence:

- **I1 No lost question.** Every segment with words is in the question of the last reply of its
  label, and that reply drained or was interrupted by words. The exception is a reply that ended in
  `brain_error`, which is logged.
- **I2 Noise never costs an answer.** A reply paused only by segments or blips with no words played
  on from its rewind point and drained with no restart.
- **I6 Announcements once.** Each task result is `heard` exactly once. No reply carried an item
  that was already heard.
- **I7 Metrics.** Each label has exactly one `turn_end_detected`. Each label with heard content has
  exactly one `first_reply_audio`, after that `t_ms`. No noise-only utterance emitted
  `turn_end_detected`.
- **I8 No deadlock.** No gate is paused.

## Fuzzer

Two layers, with fixed seeds.

**`tests/test_floor_fuzz.py` drives `floor.py` directly**, on a simulated clock:

- **Audio and detector**: each scenario is a list of sounds rendered as mic audio from the
  synthetic sounds in `tests/test_bargein.py` (vowel, hum, fricative, noise), including sounds under
  60 ms so blips really happen. Each sound is labelled as words, no words, backchannel,
  hallucination or echo, with its text. The **real** `BargeIn` runs on that audio with the
  controller's `floor` output and `reset_detector` actions. Echo is the simulated sink's output
  mixed back in. So the detector's latch and floor rules (bargein.py:262-293) are the real ones.
  Scenarios stay under 30 s, because the detector runs an FFT and an autocorrelation per frame.
- **Endpointer**: rule-based `speech_start` (60 ms of sound) and `turn_end` from the sounds, with
  fixed values: `turn_end` 600 ms after the sound ends (300 ms and 1,500 ms in some scenarios),
  plus some early false `turn_end`s.
- **STT**: interims every 1 s after a 300 ms preroll, plus 350-500 ms of request time. Finals
  complete 0.1-3 s after `turn_end`, so they reorder. `timeout` finals. Empty finals after interims
  with words. Hallucinations on noise.
- **Brain**: first text after 0.2-20 s, `brain_error`, and a cancel that takes up to 5 s.
- **Sink**: a simulated sink that tracks play offsets per piece.
- Sounds and task results at every point of a reply's life.

It shrinks a failing case by removing and shortening **sounds**, not events, because the
detector's events come from the audio.

**`tests/test_adapter_fuzz.py` drives the orchestrator adapter** with a fake STT, fake async brain
and TTS, and `FileAudio` frames with `realtime=False`. Time is `clock.ms`, advanced by the frames;
the fakes schedule their callbacks on it. It checks that stale callbacks (late text or audio from a
stopped reply, synthesis across a pause, a keepalive in flight) never reach the sink. Bug 12 was
this kind of race.

The 19 floor bugs, and the critics' event orderings, become named regression scenarios in
`tests/test_floor.py`.

## Plan

Each step ends with all unit tests passing.

1. **Sink primitives**, with tests: `pause()` without emit, `play()` from an offset, the `paused`
   flag, the gain at `tick`, piece tags and `played`, the `first_reply_audio` flag, and the
   `FileAudio` echo option. The old orchestrator does not use them.
2. **STT utterance ids (`finalize(utt=None)` keeps today's behaviour), `STT_HALLUCINATIONS`, the
   widened shared backchannel test, `Decision.pause_start_ms`, and `Interrupted.question`**, with
   tests.
3. **`live/floor.py`, test-first**: the regression scenarios, then the floor fuzzer.
4. **The new adapter, behind `orchestrator.floor: true`**, default off. It is a new code path next to
   the old one; the old `_reply` stays untouched. In two parts:
   - 4a: real pause and play from the pause point, with no rewind and no duck. This proves the
     event wiring, labels, metrics, keepalive and announcements. Add the adapter fuzzer and the
     metrics changes.
   - 4b: rewind and duck.
5. **A/B**: all bench scenarios, old and new, with the same synthetic echo setting. This is where
   pause, duck and echo decisions are judged. Then the replay of session 20261030-110521, old and
   new, judged blind against the recording and against today's branch head. The replay runs the
   real mic audio through the real `BargeIn` with the new controller's `floor`. Its mic holds the
   old replies' echo at the old times (replay_session.py:17), so it is not used to judge echo
   handling. The session's 3 false barge-ins had echo corr 0.00-0.01: they were low rumble, which
   the high-pass from commit c122a6e now targets (bargein.py:245-252). The replay judges that.
6. **Make it the default and delete**: the old path, flags, polling coroutines, fillers,
   prediction, prefetch and their warm-up, `on_user_resumed`, `finalize()` without an id,
   `BargeIn.on_transcript` and its tests (test_bargein.py:141-169). Update test_orchestrator and
   test_e2e_bench.

## Decided

1. **Pause, not flush.** With headphones, every detector barge-in pauses at once, as fast as today's
   flush. A false pause costs little: under 1 s it resumes where it stopped. Ducking is for the
   cases where pausing would loop (rule 2).
2. **Fillers go.** They are off by default since commit 61c3df6. The first content audio keeps
   `filler=False`, so live-review and piece_metrics stay valid.
3. **No probe start.** Commit 7a34aaf removed starting the brain on the endpointer's on-device probe
   text, because the probe misheard ("What do you need my S4?" for "yes for") and the brain answered
   the wrong words. Today it survives only behind an early filler (orchestrator.py:523), and fillers
   are off. The brain starts on the final.
4. **Keepalive stays**, through the gate. It is not content, it is not sent while paused, and it is
   dropped when paused. One heard with the gate open makes new words interrupt instead of join.
5. **Join or interrupt** is decided by when the segment started: a segment in the reply's question
   always joins; one within `join_gap_ms` of the last word segment's speech end with nothing heard
   joins; over heard content, after a heard keepalive, or later, it interrupts. Brain run time is not
   used: it says nothing about what the user meant.
6. **One backchannel test**, turntaking's `is_backchannel`, for every floor decision and for
   live-review. The question tail makes short answers to a question count.
7. **Speakers mode** (`speakers: true`): the detector is ignored, and interim words pause. That costs
   about 1.1-1.8 s of interrupt latency (the first interim comes about 0.7 s after `speech_start`,
   stt.py:216, 250, plus request time). It is the price of no echo cancellation. Default `false`,
   for headphones.

## Known limits

- **Announcement at 60%**: an item can be marked heard when the carrying reply played 60% of its
  content but not the sentence with the news, or told twice when it is cut just under 60% after the
  news was spoken. The controller cannot know which sentence carries it. The retry note makes the
  second telling read as news, not a repeat.
- **After the guard or the 2-pause limit** (headphones), a real interruption ducks at once but
  pauses only on its first interim with words, about 1.1-1.8 s later.
- **Speakers mode** is slower to interrupt (decided 7).
- **"Played" on `LiveAudio`** runs ahead of the speaker by the pipe and device buffer, so the rewind
  and `text_spoken` can be off by that buffer.
- **Latency**: with no probe start, the brain waits for the final (about 300-400 ms after the turn
  end). The latency piece's other fixes (commits 044e943, 21c20cd) stay.

## Considered and not taken

- **A smaller first step**: pull out only the question-by-segment-order and settling rules as the
  pure core, and add pause later. It would fix most ordering bugs. It was not taken because the
  floor bugs also come from the hold, release and cut paths, which only the single gate removes.
  The flag in step 4 and the A/B in step 5 give the same safety.
- **Shadow mode or offline trace replay**: after the first divergence, logged inputs (above all the
  detector's, which depend on `floor`) come from the old code's choices. Step 5 runs real audio
  instead.
- **`turntaking.decide` as the backchannel test**: it mutates filler state when fillers are on
  (turntaking.py:270-297) and returns an `Action`; `is_backchannel` is pure.

## Review history

| Round | Bug walk | Design review | Main changes |
|---|---|---|---|
| 1 | 10 gaps | 10 problems | narrower pause trigger, per-segment join, labels, sink section |
| 2 | 20/25 bugs prevented; 8 gaps | 10 problems | speakers mode, lazy labels, named STT and brain changes |
| 3 | every floor bug prevented; 7 serious | 7 serious | one backchannel test, reply end, orphan carry-over |
| 4 | 7 serious | 7 serious | pause on every barge-in, duck-only resets the detector, tail fix |
| 5 | every bug prevented; 8 serious | 9 serious | over-heard includes the detector's own pause, join before interrupt for carried segments, rewind scales with pause, no probe start |

From round 3 on, every sweep bug was prevented, and the gaps found were interactions between rules,
each a few lines to fix. Prose review finds these one round at a time. The fuzzer in step 3 checks
them against ground truth on thousands of orderings, so it takes over from here.
