# live-conversation

A Claude Code mod for talking to Claude Code by voice, in a live conversation you can interrupt, like ChatGPT's voice mode. Run `/live`, talk, and run `/live` again to stop. Ask for a code change and the voice hands it to your Claude Code session, then tells you the result when it is done.

Claude Code itself answers. A headless `claude -p` process (Haiku) works in the session's folder and can read files and run commands. Speech recognition uses Azure `gpt-4o-mini-transcribe` (or a local Whisper), and the voice is Kokoro, a local neural voice, run on the Mac.

How fast it is, measured on an M2 Pro over 13 recorded test conversations: the first sound of a reply (a short "Hmm, fair question," or the answer itself) comes about 0.3 s after you stop talking (median; 0.9 s at the 90th percentile). The answer's own words take about 2.5 s at the median. When you talk over it, it stops and listens.

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install live-conversation --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope. From a shell:

```
claude plugin install live-conversation --marketplace VladimirLi/claude-code-plugins
```

Requires macOS on Apple silicon and a Claude Code build that runs mods (hooks modules); tested on 2.1.312. The voice runs in a terminal session only (it needs the microphone of the machine Claude Code runs on).

### First run

The first `/live` sets itself up: `bin/live-setup` builds the voice daemon's Python environment (about 1 GB of
packages, a few minutes) while the status line shows each step, then the conversation starts by itself. Later
starts skip this. The environment lives in `~/.claude/live-conversation/venv`, shared by every installed copy,
so plugin updates do not set up again unless the requirements change. Set `LIVE_VENV` to put it elsewhere.

Setup uses [uv](https://docs.astral.sh/uv/) when it is installed (it fetches Python 3.12 by itself), else any
Python 3.11 or newer on your PATH; the `python3` that macOS ships is 3.9 and is not enough. It installs sox
with Homebrew if the microphone tools are missing. If setup fails, the toast says why, `/live status` shows
the last lines, and the full log is `~/.claude/live-conversation/setup.log`. `brew install uv` fixes most failures.

A checkout with its own `.venv` in the plugin folder (for development) uses that instead.

The first start downloads the models once (under 1 GB, to `~/.cache/huggingface` and `~/.cache/live-conversation`): the Kokoro voice, Whisper base.en for the Mac's GPU, and the Smart Turn end-of-turn model.

### Microphone permission (macOS)

The microphone is read by sox (`rec`), which runs under your terminal app, so macOS asks that app (Terminal, iTerm2, Ghostty, the VS Code terminal, ...) for microphone access the first time you run `/live`. Click Allow.

If you clicked Don't Allow, or nothing asked: open System Settings, Privacy & Security, Microphone, turn your terminal app on, then quit and reopen it. Without access, macOS gives the app silence: the status line stays at `live: listening` and never hears you.

### Azure speech recognition

Speech recognition is best with Azure OpenAI. Set these two variables before you start Claude Code (in your shell profile, or under `env` in `~/.claude/settings.json`):

| Variable | Value |
| --- | --- |
| `AZURE_OPENAI_API_KEY` | a key of your Azure OpenAI resource |
| `AZURE_OPENAI_RESOURCE_NAME` | the resource's name: the `<name>` in `https://<name>.openai.azure.com` |

The resource needs a deployment named `gpt-4o-mini-transcribe` of that model. Without the variables, the daemon uses the `whisper` command on the Mac (`brew install openai-whisper`), which is slower and makes replies start later.

## Use

```
/live           start, or stop when it runs
/live start     start
/live stop      stop
/live status    what it is doing, its tasks, and its last error output
/live feedback <note>   mark what just felt wrong ("it stopped but I said nothing")
/live review    have this session review the last recorded conversation and propose fixes
```

It is ready about 30 seconds after `/live`, when the status line says `live: listening`. The status line then follows the conversation: `listening`, `thinking`, `heard "..."`, `speaking`, and `task 1 running` while a task runs.

Use headphones: there is no echo cancellation yet, so on speakers it hears itself.

### Handing tasks to Claude Code

The voice is a small, fast model. For real coding work (change a file, fix a bug, write tests, anything longer than a minute) it hands the task to your Claude Code session:

1. You say, for example, "add a --verbose flag to the bench runner".
2. The voice says "Okay, handing that to Claude Code." and writes the task down for the session.
3. The task arrives in your session as a prompt from the live-conversation plugin and runs there, with your model, your tools and your permission settings. A permission prompt still waits for you at the keyboard.
4. When that turn ends, the voice tells you the outcome in two or three sentences, as soon as you are not talking.

You can keep talking while a task runs, and ask "how is the task going?". `/live status` lists the tasks.

### Recording and feedback

Every conversation is recorded on your Mac, in `~/.claude/live-conversation/sessions/<date-time>/`:
`audio.wav` (left: your microphone, right: what the speaker played), `events.jsonl` (every turn, filler,
barge-in and the rule that fired it) and `feedback.jsonl` (your `/live feedback` notes). Nothing is uploaded.
Set `LIVE_RECORD=0` to turn recording off, or `LIVE_SESSIONS_DIR` to record somewhere else. Delete old
sessions by hand; ten minutes is about 60 MB.

When something feels wrong, type `/live feedback <what happened>` right away: it marks that moment.
Afterwards, `/live review` runs `bin/live-review` on the recording (a report plus a short audio clip of each
barge-in and each note) and asks this session to find the cause of each problem and propose fixes.

## What the voice Claude may do

The voice runs `claude -p` with Haiku in the session's project folder, and may use Read, Grep, Glob and Bash there without asking. Your hooks, settings, MCP servers and slash commands are not loaded for it, so they do not slow it down. It uses your Claude Code login: it keeps two warm processes, sends each turn to both and keeps the faster answer.

## Troubleshooting

- `/live status` shows the daemon's last output when it stopped or failed.
- Run the daemon from a shell to see everything: `.venv/bin/python -m live --cwd <project folder>` in the plugin's folder (Ctrl-C stops it). Task handoff needs `/live`.

## How it is built

[ARCHITECTURE.md](ARCHITECTURE.md) has the modules (endpointing, speech recognition, brain, voice, barge-in, turn taking) and the task handoff; [bench/SPEC.md](bench/SPEC.md) how it is measured against OpenAI's realtime voice model.

## Development

```
claude plugin validate .
claude plugin test .
.venv/bin/python -m unittest discover -s tests
```

## License

MIT
