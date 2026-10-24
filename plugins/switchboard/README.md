# switchboard

A Claude Code mod that adds `/mods`: one list of every mod you have, each turned on or off with a key.

A mod here is a plugin with a hooks module. Switchboard lists two kinds:

- **This session's mods**: plugins loaded from disk with `--plugin-dir` or `CLAUDE_CODE_PLUGIN_DIRS`. Turning one off parks its `hooks/hooks.json` as `hooks/hooks.json.off`, and the folder hot-reloads; turning it on puts the file back.
- **Installed mods**: plugins installed from a marketplace that ship a hooks module. On and off runs `claude plugin enable|disable`, then `/reload-plugins`.

## Install

At the prompt of a Claude Code terminal session:

```
/plugin install switchboard --marketplace VladimirLi/claude-code-plugins
```

Answer `y` to add the marketplace, then pick a scope (user scope is first). From a shell:

```
claude plugin install switchboard --marketplace VladimirLi/claude-code-plugins
```

In the desktop app: click + next to the prompt box, then Plugins, Add marketplace with `VladimirLi/claude-code-plugins`, then Add plugin.

Requires a Claude Code build that runs mods (hooks modules); tested on 2.1.310.

## Commands

```
/mods               open the list; a key (1-9, a-z) or Enter turns a mod on or off, Esc closes
/mods on <mod>      turn a mod on
/mods off <mod>     turn a mod off
```

Where no pane can be shown, `/mods` prints the list instead.

## What it touches

It runs `claude plugin list --json` to find mods, reads their manifests and hooks files, and changes only what a toggle asks for: it renames one `hooks.json` in a mod folder loaded from disk, or enables or disables one installed plugin in your settings.

## Development

```
claude plugin validate .
claude plugin test .
```

## License

MIT
