## Observations (from the raw server events, 2026-10-29)

- Where the time goes: semantic_vad (eagerness auto) sends `speech_stopped` about 1.13 s after the true
  speech end on every clean turn (very stable, about +/-30 ms). Then the first audible reply sample
  comes about 0.7-1.0 s later (response created, first audio delta, plus about 80 ms of leading
  near-silence in the model's audio). So the bar is about 2.1 s voice to voice, not 500 ms.
- Barge-in: the client flushes the speaker the moment `input_audio_buffer.speech_started` arrives.
  That event normally arrives 0.37-0.6 s after the server's own `audio_start_ms`. In 3 of 9
  interruption or backchannel runs it arrived 0.7-6.4 s late (s08 run1/run2, s09 run1). Every time,
  it arrived just before `response.output_audio.done` of the reply that was still generating. It
  looks like the server holds VAD events while it generates a response. That is the measured
  behaviour of this API, so it counts against the bar.
- s13 backchannel: in all 3 runs the "Mm-hmm" was treated as an interruption. The reply was cut
  92-155 ms after the user started, and the model started its explanation again from the top.
  Under SPEC this is a false barge-in for the bar.
- s05 and s12: semantic_vad cut in at the scripted pauses (4 and 3 decisions in 3 runs). Mostly the
  response was cancelled before any audio. In s12 run2 the reply was audible over the user's
  continuation (230 ms overlap) before the client flushed it.
- `score.py` `stop_delay_ms` checks for the first 100 ms below -50 dBFS after the interrupting
  speech starts. The bar's normal pauses between phrases meet that test, so the value can be far
  below the real stop time. The `stop p50` column above uses the client flush time from
  `playback_stopped` instead.
- Mic pacing: a chunk was sent at most 42 ms late (typically under 15 ms). The pacer catches up on
  the next chunk.
- Speaking rate: the replies are long (s07 has about 312 words over 3 turns; s08 turn 2 "short
  version" is about 85 words, about 30 s of audio).
