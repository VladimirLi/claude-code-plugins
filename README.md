# claude-code-plugins

A collection of Claude Code plugins, mods and skills, published as a plugin marketplace.

| Plugin | What it does |
| --- | --- |
| [brain-dump](plugins/brain-dump) | Turns long voice-dictated prompts into a clear, structured prompt before Claude sees them |

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install brain-dump --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope. From a shell:

```
claude plugin install brain-dump --marketplace VladimirLi/claude-code-plugins
```

In the desktop app: click + next to the prompt box, then Plugins, Add marketplace with `VladimirLi/claude-code-plugins`, then Add plugin.

To browse everything here, add the marketplace once with `/plugin marketplace add VladimirLi/claude-code-plugins`, then open `/plugin`.

## Layout

Each plugin lives in `plugins/<name>/` with its own `.claude-plugin/plugin.json` and README, and has one entry in `.claude-plugin/marketplace.json`. A standalone skill is a plugin folder with a `SKILL.md` at its root.

To add one: copy it to `plugins/<name>/`, add its entry to `.claude-plugin/marketplace.json`, then run `claude plugin validate .` from the repository root.

## License

MIT
