import { expect, test } from 'claude-code/testing'

import { findRow, hooksFiles, namesModule, parseInline, parseInstalled } from '../hooks/scan'

test('an installed plugin is a mod only when a hooks file names a module', async () => {
  expect(hooksFiles('{"hooks":["./hooks/a.json","./hooks/band.json"]}')).toEqual(['hooks/hooks.json', 'hooks/a.json', 'hooks/band.json'])
  expect(hooksFiles('{"hooks":"./x.json"}')).toEqual(['hooks/hooks.json', 'x.json'])
  expect(hooksFiles('not json')).toEqual(['hooks/hooks.json'])
  expect(namesModule('{"hooks":{},"modules":["./band/register.tsx"]}')).toBe(true)
  expect(namesModule('{"hooks":{"SessionStart":[]}}')).toBe(false)
  expect(parseInstalled('[{"id":"ponytail@ponytail","version":"5.3.2","enabled":true,"installPath":"/p"},{"id":"x"}]')).toEqual([
    { id: 'ponytail@ponytail', version: '5.3.2', enabled: true, installPath: '/p' },
  ])
  expect(parseInstalled('oops')).toEqual([])
  // A mod loaded from the mods folder is not listed a second time as an installed plugin.
  expect(parseInstalled('[{"id":"cache-clock@inline","version":"0.1.0","enabled":true,"installPath":"/m/cache-clock"}]')).toEqual([])
  expect(findRow([{ id: 'ponytail@ponytail', name: 'ponytail' }], 'Ponytail')?.id).toBe('ponytail@ponytail')
})

const PANE = {
  plugin: 'switchboard',
  component: 'Pane',
  requestId: 'switchboard',
  props: { title: 'Mods', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

test('the pane lists each mod with its state and a hotkey', async ($, on) => {
  const state: Record<string, unknown> = {
    rows: { shape: 'rows-1', value: [
      { id: 'brain-dump', name: 'brain-dump', version: '0.2.1', kind: 'session', isOn: true, path: '/m/brain-dump' },
      { id: 'ponytail@ponytail', name: 'ponytail', version: '5.3.2', kind: 'installed', isOn: false, path: '/p' },
    ] },
    message: '',
  }
  on('state.get', (_, e) => ({ value: { value: state[e.key], version: 1 } }))

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const buttons = await ui.findAll({ type: 'Button' })
  expect(buttons.map(b => b.key)).toEqual(['brain-dump', 'ponytail@ponytail'])
  expect(buttons[0]?.text).toContain('● on')
  expect(buttons[1]?.text).toContain('○ off')
  await ui.unmount()
})

const LISTED = '[{"id":"brain-dump@inline","version":"0.4.0","enabled":true,"installPath":"/m/brain-dump"}]'

test('a press acts on the mod as it is on disk now, not as the pane last drew it', async ($, on) => {
  let file = 'hooks.json'
  const moved: string[] = []
  on('ui.open', () => ({ value: { isPlaced: true } }) as never)
  on('fs.list', () => ({ value: [{ kind: 'dir', name: 'brain-dump' }] }) as never)
  on('fs.stat', (_, e) => ((e as { path: string }).path.endsWith(`/hooks/${file}`) ? { value: {} } : { deny: 'missing' }) as never)
  on('fs.read', (_, e) => ({ value: (e as { path: string }).path.includes('/hooks/') ? '{"modules":["./register.tsx"]}' : '{}' }) as never)
  on('command.run', { command: 'reload-plugins' }, () => ({ text: '' }) as never)
  on('process.run', (_, e) => {
    if (e.argv[0] === 'mv') {
      const [from, to] = e.argv.slice(1).map(p => p.replace(/^.*\//, ''))
      if (from !== file) return { value: { exitCode: 1, stdout: '', stderr: `mv: ${from}: No such file` } } as never
      moved.push(`${from} -> ${to}`)
      file = to!
    }
    return { value: { exitCode: 0, stdout: LISTED, stderr: '' } } as never
  })

  await $.command.run({ command: 'mods', args: '' } as never)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ key: 'brain-dump' }))?.text).toContain('● on')

  // Another session turns it off after the pane was drawn.
  file = 'hooks.json.off'
  await ui.press({ key: 'brain-dump' })
  expect(moved).toEqual(['hooks.json.off -> hooks.json'])
  expect((await ui.find({ type: 'Text', text: /turned on/ }))?.text).toContain('brain-dump turned on')
  await ui.unmount()
})

test('an installed switchboard lists the mods loaded from disk, never the folders beside it in the plugin cache', async ($, on) => {
  on('ui.open', () => ({ value: { isPlaced: false } }) as never)
  // Beside an installed copy sits only its own version folder.
  on('fs.list', () => ({ value: [{ kind: 'dir', name: '0.1.0' }] }) as never)
  on('fs.stat', (_, e) => ((e as { path: string }).path.endsWith('/hooks/hooks.json') ? { value: {} } : { deny: 'missing' }) as never)
  on('fs.read', (_, e) => ({ value: (e as { path: string }).path.includes('/hooks/') ? '{"modules":["./register.tsx"]}' : '{}' }) as never)
  on('process.run', () => ({ value: { exitCode: 0, stdout: LISTED, stderr: '' } }) as never)
  const r = await $.command.run({ command: 'mods', args: '' } as never)
  expect(r.text).toContain('brain-dump')
  expect(r.text).not.toContain('0.1.0')
  expect(parseInline(LISTED).map(p => p.installPath)).toEqual(['/m/brain-dump'])
})
