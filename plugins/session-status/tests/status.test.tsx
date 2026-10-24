import { expect, mock, test } from 'claude-code/testing'

import { afterPrompt, dropAnswered, gateChoice, mergeAnswer, parseStatus, progressOf, stripStatus, toMarkdown } from '../hooks/status'

const TAGGED =
  'Done.\n\n<status>{"goal":"Ship mods","now":"Switchboard built","needs":[{"ask":"Load mods in every session?","options":["yes","no"]}],"next":"Wait","plan":[{"item":"Build","state":"done"},{"item":"Test","state":"bogus"}]}</status>'

test('the last well-formed tag is the status, and it is hidden from the reply', async () => {
  const s = parseStatus(`<status>not json</status>\n${TAGGED}`)
  expect(s?.now).toBe('Switchboard built')
  expect(s?.needs).toEqual([{ ask: 'Load mods in every session?', options: ['yes', 'no'] }])
  expect(s?.plan[1]).toEqual({ item: 'Test', state: 'waiting' })
  expect(parseStatus('no tag here')).toBeUndefined()
  expect(stripStatus(TAGGED)).toBe('Done.')
  // A tag still streaming in is hidden from its opening.
  expect(stripStatus('Done.\n<status>{"goal":"Sh')).toBe('Done.')
  expect(toMarkdown(s!)).toContain('**Needs you:** Load mods in every session? (yes / no)')
})

test('the band shows Now and each ready answer as a hotkey', async ($, on) => {
  const s = parseStatus(TAGGED)
  const state: Record<string, unknown> = { status: { shape: 'status-1', value: s }, updatedAt: 0, isFromFork: false }
  on('state.get', (_, e) => ({ value: { value: state[e.key], version: 1 } }))
  on('store.get', () => ({ value: undefined }))
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>below</Text>
  })

  const ui = await $.ui.mount({
    plugin: 'session-status',
    component: 'AbovePrompt',
    requestId: 'band',
    surface: 'terminal',
    props: { bodyColumns: 100, hasSurvey: false, isWorking: false, maxRows: 20, scroll: { offset: 0, bodyRows: 20 }, view: {} },
  })
  expect((await ui.find({ type: 'Text', text: /Switchboard built/ }))?.text).toContain('◆ Switchboard built')
  expect((await ui.findAll({ type: 'Button' })).map(b => b.text)).toEqual(['yes', 'no'])
  expect((await ui.find({ type: 'Text', text: /1\/2 done/ }))).toBeDefined()
  await ui.unmount()
})

test('answers to several questions share the draft; a new answer to the same question replaces the old', async () => {
  const a = { ask: 'Ship it?', options: ['yes', 'no'] }
  const b = { ask: 'Which branch?', options: ['dev'] }
  expect(mergeAnswer('\nShip it? → yes', a, 'yes')).toBe('Ship it? → yes')
  expect(mergeAnswer('Ship it? → yes\nWhich branch? → dev', b, 'dev')).toBe('Ship it? → yes\nWhich branch? → dev')
  expect(mergeAnswer('my note\nShip it? → yes\nWhich branch? → dev\nShip it? → no', a, 'no')).toBe('my note\nWhich branch? → dev\nShip it? → no')
})

test('a sent answer takes its question off Needs you; the plan shows how much is done', async () => {
  const s = parseStatus(TAGGED)!
  expect(dropAnswered(s, 'Load mods in every session? → yes').needs).toEqual([])
  expect(dropAnswered(s, 'something else')).toBe(s)
  expect(progressOf(s.plan)).toBe('1/2 done')
  expect(progressOf([])).toBe('')
})

test('a prompt that answers no open question marks the status as older than that prompt and clears its questions', async () => {
  const s = parseStatus(TAGGED)!
  expect(afterPrompt(s, 'something else')).toEqual({ status: { ...s, needs: [] }, isStale: true })
  // With no question open, a follow-up prompt leaves the status as it is.
  const quiet = { ...s, needs: [] }
  expect(afterPrompt(quiet, 'and also check the docs')).toEqual({ status: quiet, isStale: false })
  expect(afterPrompt(s, 'Load mods in every session? → yes')).toEqual({ status: { ...s, needs: [] }, isStale: false })
})

test('a stale status draws dimmed as the status before the last prompt', async ($, on) => {
  const state: Record<string, unknown> = { status: { shape: 'status-1', value: { ...parseStatus(TAGGED)!, needs: [] } }, updatedAt: 0, isFromFork: false, isStale: true }
  on('state.get', (_, e) => ({ value: { value: state[e.key], version: 1 } }))
  on('store.get', () => ({ value: undefined }))
  const ui = await $.ui.mount({ plugin: 'session-status', component: 'AbovePrompt', requestId: 'band', surface: 'terminal', props: { bodyColumns: 100, hasSurvey: false, isWorking: false, maxRows: 20, scroll: { offset: 0, bodyRows: 20 }, view: {} } })
  expect((await ui.find({ type: 'Text', text: /before your last prompt/ }))?.text).toContain('◇ before your last prompt: Switchboard built')
  expect(await ui.find({ type: 'Text', text: /◆/ })).toBeUndefined()
  await ui.unmount()
})

test('a gate becomes the first decision row, its recommendation first, and an answer to it is read back', async () => {
  const s = parseStatus(
    '<status>{"goal":"Pick a design","kind":"planning","done":"A design is chosen","now":"Gated","needs":[{"ask":"Other?","options":["a"]}],"next":"Wait","plan":[],"gate":{"thread":"Tie-break experiments","size":"~1h","recommend":"Park"}}</status>',
  )
  expect(s?.kind).toBe('planning')
  expect(s?.done).toBe('A design is chosen')
  expect(s?.needs[0]).toEqual({ ask: 'Side: Tie-break experiments (~1h)', options: ['Park', 'Fork', 'Now', 'Make it the goal'] })
  expect(s?.needs[1]?.ask).toBe('Other?')
  // A need the model wrote for the gate itself gives way to the gate's own row.
  const dup = parseStatus('<status>{"goal":"g","now":"n","needs":[{"ask":"Build it?","options":["Now","Fork","Park"]}],"next":"","plan":[],"gate":{"thread":"Checker","size":"","recommend":"Now"}}</status>')
  expect(dup?.needs).toEqual([{ ask: 'Side: Checker', options: ['Now', 'Fork', 'Park', 'Make it the goal'] }])
  expect(gateChoice(s!.gate!, 'Side: Tie-break experiments (~1h) → Now\nand go')).toBe('Now')
  expect(gateChoice(s!.gate!, 'just carry on')).toBeUndefined()
  expect(toMarkdown(s!)).toContain('**Done when:** A design is chosen')
  // An unknown kind is dropped, and a gate without a thread is no gate.
  const t = parseStatus('<status>{"goal":"g","kind":"other","now":"n","needs":[],"next":"","plan":[],"gate":{"size":"1h"}}</status>')
  expect(t?.kind).toBeUndefined()
  expect(t?.gate).toBeUndefined()
  expect(t?.needs).toEqual([])
})

for (const focusRules of [true, false]) {
  test(`the focus rules ride with each prompt only when focusRules is on (${focusRules})`, { options: { focusRules } }, async ($, on) => {
    mock.store(on)
    const runs: string[] = []
    on('process.run', (_, e) => {
      runs.push(e.argv.join(' '))
      return { value: { exitCode: 0, stdout: '', stderr: '' } as never }
    })
    let context: readonly string[] = []
    on('prompt.submit', (_, e) => {
      context = e.context ?? []
      return { text: e.text }
    })
    await $.prompt.submit({ text: 'now: hi', origin: { kind: 'composer' }, wait: false })
    expect(context.some(c => c.startsWith('Session status:'))).toBe(true)
    expect(context.some(c => c.startsWith('Focus:'))).toBe(focusRules)
    // The usage log is off unless asked for.
    expect(runs).toEqual([])
  })
}
