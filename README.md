# claude-code-plugins

A collection of Claude Code plugins, mods and skills, published as a plugin marketplace.

| Plugin | What it does |
| --- | --- |
| [brain-dump](plugins/brain-dump) | Turns long voice-dictated prompts into a clear, structured prompt before Claude sees them, or into a Gauntlet Loop prompt (`gl`) |
| [session-status](plugins/session-status) | Session status above the prompt, decisions as hotkeys, and focus rules that gate side work |
| [switchboard](plugins/switchboard) | `/mods` lists every mod and turns each on or off with a key |
| [cache-clock](plugins/cache-clock) | Status line countdown until the prompt cache goes cold, a warning before it does, and `/keepwarm` |
| [live-conversation](plugins/live-conversation) | `/live`: talk to Claude Code by voice in a live, interruptible conversation; coding tasks go to your session and the result is spoken |

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install <plugin> --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope. From a shell:

```
claude plugin install <plugin> --marketplace VladimirLi/claude-code-plugins
```

In the desktop app: click + next to the prompt box, then Plugins, Add marketplace with `VladimirLi/claude-code-plugins`, then Add plugin.

To browse everything here, add the marketplace once with `/plugin marketplace add VladimirLi/claude-code-plugins`, then open `/plugin`.

## Updates

Turn on auto-update once: in `/plugin`, open Marketplaces, select `vladimirli-plugins`, then Enable auto-update. A plugin changed by a push then updates in the background during your next session, and the new version loads in the session after that (or at once with `/reload-plugins`). Without auto-update, run `/plugin marketplace update vladimirli-plugins`.

The plugins set no `version` in `plugin.json`, so every pushed commit that changes a plugin is a new version. Do not add one back: a pinned `version` stops updates until the string changes. The version is the repository's commit, so any push updates all plugins at once.

A copy of a plugin with the same name in a `--plugin-dir` or `CLAUDE_CODE_PLUGIN_DIRS` folder replaces the marketplace copy silently, so remove such copies after you install from here.

## Layout

Each plugin lives in `plugins/<name>/` with its own `.claude-plugin/plugin.json` and README, and has one entry in `.claude-plugin/marketplace.json`. A standalone skill is a plugin folder with a `SKILL.md` at its root.

To add one: copy it to `plugins/<name>/` with no `version` in its `plugin.json`, add its entry to `.claude-plugin/marketplace.json`, then run `claude plugin validate .` from the repository root.

## License

MIT
