# Bar for the tts piece

Recorded by `bench/tts_reference.py` on 2026-10-29: deployment `gpt-realtime` (GA realtime
WebSocket, voice `alloy`), session opened and configured first, then the clock starts and
`response.create` is sent with "Say exactly this, nothing else: <text>". Every take had
transcript WER 0 against the text. Two reps: `<id>/run1`, `<id>/run2`, linked as
`bench/runs/2026-10-29-bar-tts-rep<k>`. Only whole-text takes exist (the model cannot be fed
text word by word), so `_stream` folders have no bar.

Metrics of rep 1 (`bench/piece_metrics.py tts`):

| text | first audio ms | gaps > 250 ms | F0 median Hz | F0 std st | energy std dB | rate wpm | clicks | whisper WER |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| t01_short | 566.7 | [700, 510] | 165.5 | 3.63 | 8.29 | 236.4 | 84 | 0.0 |
| t02_question | 694.1 | [360, 760] | 160.5 | 3.52 | 8.63 | 242.1 | 21 | 0.0 |
| t03_repo | 1344.6 | [330, 440, 460, 470] | 140.4 | 2.82 | 8.11 | 236.8 | 192 | 0.0333 |
| t04_ack | 564.8 | [330, 600] | 149.1 | 1.87 | 8.76 | 266.7 | 4 | 0.0 |
| t05_numbers | 612.8 | [380, 340] | 177.8 | 3.2 | 9.47 | 276.9 | 11 | 0.0476 |
| t06_code_words | 613.8 | [280, 610] | 154.3 | 2.67 | 8.07 | 197.8 | 87 | 0.1111 |
| t07_long | 971.1 | [520, 760, 510, 930, 470, 500, 750, 450, 550] | 149.5 | 3.83 | 8.22 | 269.3 | 164 | 0.0 |
| t08_empathy | 616.1 | [290, 270, 530] | 145.5 | 3.45 | 8.81 | 232.3 | 37 | 0.0588 |

Summary: first audio median 614.95 ms, whisper WER mean 0.0314,
F0 std median 3.325 semitones, energy std median 8.46 dB,
26 gaps over 250 ms, 600 clicks. Rep 2 summary (from blind.py):
first audio median 831 ms, WER 0.037, F0 std 3.22, energy std 8.43, 25 gaps, 894 clicks.

Observations: the bar pauses 270-930 ms at commas and sentence ends; its first audible sample
comes 550-1350 ms after the request (about 80 ms of near-silence leads each reply); active
speech level is about -18 dBFS with peaks near 0 dBFS; the remaining whisper errors are
homophones ("cash timer"), number format ("240" for "two hundred and forty") and "tilde".
