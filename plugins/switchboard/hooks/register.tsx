import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { ModRow } from '../types'
import { HOTKEYS, findRow, hooksFiles, namesModule, parseInline, parseInstalled } from './scan'

const PANE = 'switchboard'
const rows = atom({ plugin: 'switchboard', key: 'rows' } as const, [] as ModRow[], { shape: 'rows-1' })
const message = atom({ plugin: 'switchboard', key: 'message' } as const, '')

const exists = ($: EngineInterface, path: string) =>
  $.fs.stat(path).then(
    () => true,
    () => false,
  )

// A mod folder loaded from disk: on with hooks/hooks.json, off when that file is parked as hooks.json.off.
async function sessionRow($: EngineInterface, path: string): Promise<ModRow | undefined> {
  const name = path.replace(/\/+$/, '').replace(/^.*\//, '')
  const isOn = await exists($, `${path}/hooks/hooks.json`)
  const hooks = await $.fs.read(`${path}/hooks/${isOn ? 'hooks.json' : 'hooks.json.off'}`).catch(() => '')
  if (!namesModule(hooks)) return undefined
  const manifest = await $.fs.read(`${path}/.claude-plugin/plugin.json`).catch(() => '{}')
  let version = ''
  try {
    version = String(JSON.parse(manifest).version ?? '')
  } catch {}
  return { id: name, name, version, kind: 'session', isOn, path }
}

// This session's mods: the plugins loaded from disk, and, when switchboard itself is one, the folders beside it,
// where a parked mod no longer loads. An installed switchboard sits in the plugin cache, whose folders are not mods.
async function scanSession($: EngineInterface, listed: string): Promise<ModRow[]> {
  const inline = parseInline(listed).map(p => p.installPath)
  const paths = inline.filter(p => p !== $.plugin.root)
  if (inline.includes($.plugin.root)) {
    const dir = $.plugin.root.replace(/\/[^/]+\/?$/, '')
    for (const entry of await $.fs.list(dir).catch(() => [])) {
      if (entry.kind === 'dir' && entry.name !== $.plugin.name) paths.push(`${dir}/${entry.name}`)
    }
  }
  const found: ModRow[] = []
  for (const path of new Set(paths)) {
    const row = await sessionRow($, path)
    if (row) found.push(row)
  }
  return found
}

// Installed plugins that ship a hooks module; on and off is `claude plugin enable|disable`.
async function scanInstalled($: EngineInterface, listed: string): Promise<ModRow[]> {
  const found: ModRow[] = []
  for (const p of parseInstalled(listed)) {
    const manifest = await $.fs.read(`${p.installPath}/.claude-plugin/plugin.json`).catch(() => '{}')
    let isMod = false
    for (const file of hooksFiles(manifest)) {
      if (namesModule(await $.fs.read(`${p.installPath}/${file}`).catch(() => ''))) isMod = true
    }
    if (isMod) found.push({ id: p.id, name: p.id.split('@')[0] ?? p.id, version: p.version, kind: 'installed', isOn: p.enabled, path: p.installPath })
  }
  return found
}

async function refresh($: EngineInterface): Promise<ModRow[]> {
  const listed = (await $.process.run(['claude', 'plugin', 'list', '--json']).catch(() => undefined))?.stdout ?? ''
  const all = [...(await scanSession($, listed)), ...(await scanInstalled($, listed))]
  await update($, rows, () => all)
  return all
}

async function toggle($: EngineInterface, row: ModRow, turnOn: boolean): Promise<string> {
  if (row.isOn === turnOn) return `${row.name} is already ${turnOn ? 'on' : 'off'}.`
  if (row.kind === 'session') {
    const [from, to] = turnOn ? ['hooks.json.off', 'hooks.json'] : ['hooks.json', 'hooks.json.off']
    const r = await $.process.run(['mv', `${row.path}/hooks/${from}`, `${row.path}/hooks/${to}`])
    if (r.exitCode !== 0) return `Could not turn ${row.name} ${turnOn ? 'on' : 'off'}: ${r.stderr.trim()}`
    await refresh($)
    // The mods folder hot-reloads: the change takes effect once the folder is quiet, a moment later.
    return `${row.name} turned ${turnOn ? 'on' : 'off'}; it reloads in a moment.`
  }
  const r = await $.process.run(['claude', 'plugin', turnOn ? 'enable' : 'disable', row.id])
  if (r.exitCode !== 0) return `Could not turn ${row.name} ${turnOn ? 'on' : 'off'}: ${(r.stderr || r.stdout).trim()}`
  await refresh($)
  const reloaded = await $.command.run({ command: 'reload-plugins', args: '' }).then(
    () => true,
    () => false,
  )
  return `${row.name} turned ${turnOn ? 'on' : 'off'}${reloaded ? '.' : '; run /reload-plugins to apply it now.'}`
}

const listText = (all: readonly ModRow[]) =>
  all.length === 0 ? 'No mods found.' : all.map(r => `${r.isOn ? '● on ' : '○ off'}  ${r.name} ${r.version} (${r.kind})`).join('\n')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'mods', description: 'Switchboard: list mods and turn each on or off', argumentHint: '[on|off <mod>]' })
    return next(e)
  })

  on('command.run', { command: 'mods' }, async ($, e) => {
    const [word = '', name = ''] = e.args.trim().split(/\s+/)
    const all = await refresh($)
    if ((word === 'on' || word === 'off') && name !== '') {
      const row = findRow(all, name)
      if (!row) return { text: `switchboard: no mod named "${name}".\n${listText(all)}` }
      const said = await toggle($, row, word === 'on')
      return { text: `switchboard: ${said}` }
    }
    if (word !== '') return { text: 'switchboard: usage /mods, or /mods on|off <mod>' }
    await update($, message, () => '')
    const opened = await $.ui.open({ id: PANE, title: 'Mods', focus: true, closeOnEscape: true })
    // Where no pane can be placed, the list is the command's output.
    return { text: opened.isPlaced ? 'switchboard: pick a mod to turn it on or off; Esc closes.' : `switchboard:\n${listText(all)}` }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const all = await read($, rows)
    const said = await read($, message)
    // The drawn row may be out of date (a quick second press, another session), so the press reads the mod's state again first.
    const press = (row: ModRow) => async () => {
      const now = (await refresh($)).find(r => r.id === row.id) ?? row
      const text = await toggle($, now, !now.isOn)
      await update($, message, () => text)
    }
    return (
      <Box flexDirection="column" width={e.props.bodyColumns}>
        {all.length === 0 && <Text dimColor>No mods found.</Text>}
        {all.map((row, i) => (
          <Button key={row.id} plain {...(HOTKEYS[i] ? { hotkey: HOTKEYS[i] } : {})} onPress={press(row)}>
            <Text color={row.isOn ? 'success' : 'inactive'}>{row.isOn ? '● on ' : '○ off'}</Text> {row.name} <Text dimColor>
              {row.version} · {row.kind === 'session' ? 'this session' : 'installed'}
            </Text>
          </Button>
        ))}
        <Text> </Text>
        {said !== '' && <Text>{said}</Text>}
        <Text dimColor>A key or Enter turns a mod on or off · Esc closes</Text>
      </Box>
    )
  })
}
