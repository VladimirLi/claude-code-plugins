# session-status

A Claude Code mod that keeps the session's status above the prompt: what is happening now, how much of the plan is done, and the decisions Claude needs from you, each ready answer a hotkey.

It also adds focus rules: the session has one fixed goal with a "done when" line, side requests go through a gate (Fork, Park, Now, or Make it the goal) instead of quietly widening the work, and after a long pause Claude starts with a short resume.

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install session-status --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope (user scope is first). From a shell:

```
claude plugin install session-status --marketplace VladimirLi/claude-code-plugins
```

In the desktop app: click + next to the prompt box, then Plugins, Add marketplace with `VladimirLi/claude-code-plugins`, then Add plugin.

Requires a Claude Code build that runs mods (hooks modules); tested on 2.1.310.

## How it works

- Each prompt you send carries a short instruction: when a reply ends a step, it ends with a hidden `<status>` line (goal, kind, done when, now, needs, next, plan).
- The tag is cut from the reply as drawn. The band above the prompt shows **Now**, plan progress, the age of the status, and one row per decision. Pressing an answer's key (1-9) puts `question → answer` in the prompt box for you to edit and send.
- When a turn of 2 minutes or more ends without a status, a fork of the conversation writes one (marked "inferred"). This costs one extra model call.
- After 2 hours away, or after a compaction, the next reply starts with a resume of at most 5 lines.

## Commands

```
/standing        open the full status and plan in a pane
/standing copy   copy the status as Markdown
/standing off    stop asking for a status (on turns it back on)
```

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| Fallback after (seconds) | 120 | A turn this long without a status gets one from a fork. 0: never. |
| Focus rules | on | Add the goal, gate, proportion and close rules to each prompt. Off: only the status line is asked for. |
| Usage log | off | Append a line to `~/.claude/usage.jsonl` for each gate shown, gate answer and resume. |

## Privacy

The status lives in the session's own state and the mod's store on your machine. The fallback fork sends the conversation to the model through your own session, like any reply. Nothing is written to disk outside Claude Code unless you turn on the usage log.

## Development

```
claude plugin validate .
claude plugin test .
```

## License

MIT
