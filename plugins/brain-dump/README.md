# brain-dump

A Claude Code mod that turns a long, voice-dictated prompt into a clear, structured prompt before Claude sees it.

You talk; brain-dump picks what kind of request it is (bug, feature, change, question, review, workflow, or general), asks you about the gaps that matter, and writes a prompt with the sections that kind of request needs. The rewrite goes into the prompt box for you to edit and send, so nothing is sent without you seeing it.

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install brain-dump --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope (user scope is first). From a shell:

```
claude plugin install brain-dump --marketplace VladimirLi/claude-code-plugins
```

In the desktop app: click + next to the prompt box, then Plugins, Add marketplace with `VladimirLi/claude-code-plugins`, then Add plugin.

Requires a Claude Code build that runs mods (hooks modules); tested on 2.1.310.

## When it rewrites

- A prompt of 120 words or more. In `confirm` mode (the default) it asks first.
- A prompt that starts or ends with `bd`, or that says "this is a brain dump" at the start of a sentence. Always rewritten, in any mode.
- A prompt that starts or ends with `gl`, or that asks to "make a gauntlet loop" ("create", "build", "write" and "set up" work too), becomes a Gauntlet Loop prompt. See below.
- A prompt that starts with `raw` is never rewritten; the prefix is removed.

Slash commands and prompts that do not come from you (SDK, scheduled tasks, other plugins) are never touched. If anything fails, your prompt is sent as you typed it.

## How it works

1. A model reads the brain dump and picks its intent. With "Use the conversation" on, it reads a fork of the current conversation, so "that file" or "the PR from before" is resolved and settled points are not asked again.
2. It asks up to 3 questions about missing required sections and unclear points. Every question offers "Let the agent decide"; open ones also offer "Leave it out". You can type your own answer.
3. It writes the prompt, checks it for gaps again (up to 2 rounds), and puts it in the prompt box. `/braindump original` brings back what you said.

## Gauntlet loops

A [Gauntlet Loop](https://github.com/robonuggets/gauntlet-loop) prompt makes an agent pick a real reference (the bar), split the work into small pieces, and run a builder and a separate harsh critic on each piece. It loops until the critic picks our work over the bar in a blind comparison. The technique is [Matt Shumer's](https://github.com/mshumer).

```
gl a pricing page for my SaaS, has to feel trustworthy
```

brain-dump reads the conversation and your request, then asks about what the template needs:

- **Bar**: a named thing the critic can actually fetch and put side by side with ours. When you named none, or a vague one ("award-winning sites"), it offers 2 or 3 concrete candidates and no "Let the agent decide". You can type your own.
- **Judged on**: what the critic decides, such as "which one a non-engineer understands faster".
- **Measurable half**: a number to beat as well, when the goal has one.

It never asks about architecture, stack, round counts, budget or tools. A budget, tools or pieces go into the prompt only if you named them. The prompt is the skill's template, filled in. Its fixed lines are written by the mod, not the model: the harsh critic, `/loop` until it wins blind, the live progress page, and "Fan out subagents and ultracode". Paste it into a fresh session, or send it here.


Set them when you enable the plugin, in `/config`, or with `/braindump`:

```
/braindump                      show the settings (the gauntlet prefix is the gauntletPrefix setting)
/braindump confirm|auto|off|on  set the mode (on = confirm)
/braindump min <words>          set the length that counts as a brain dump
/braindump sections <a, b, c>   set the required sections for a request no intent fits
/braindump rounds <n>           how many times gaps are checked and asked
/braindump questions on|off     ask about gaps or not
/braindump conversation on|off  find gaps with the conversation as context
/braindump review on|off        put the rewrite in the prompt box, or send it at once
/braindump history on|off       keep a local log of brain dumps and rewrites, or not
/braindump original             put your last original brain dump back in the prompt box
```

The model that asks and rewrites is `sonnet` by default (the `model` setting).

## Privacy

Your brain dump is sent to the model you choose, through your own Claude Code session, like any prompt. Nothing else leaves your machine. The last original is kept in the plugin's store so `/braindump original` can restore it. With `history` on (off by default), each brain dump, your answers and the rewrite are appended to `~/.claude/brain-dump/history.jsonl`.

## Development

```
claude plugin validate .
claude plugin test .
```

## License

MIT
