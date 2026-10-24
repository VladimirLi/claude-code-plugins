import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const USAGE = { input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 900, cache_creation_input_tokens: 10 }
const MIN = 60_000

function engine(on: On) {
  const w = { pings: 0, toasts: [] as string[] }
  const clock = mock.clock(on, { now: 1_000_000 })
  on('command.register', () => ({ value: { command: 'x' } }) as never)
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', (_, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('model.fork', () => {
    w.pings++
    return { value: { isAnswered: true, text: 'ok', usage: USAGE } } as never
  })
  on('turn.start', () => ({}) as never)
  on('turn.complete', () => ({ text: '' }) as never)
  on('prompt.submit', (_, e) => ({ text: e.text }))
  on('session.start', (_, e) => ({ cwd: e.cwd }) as never)
  return { w, clock }
}

const turn = { agentId: undefined, reason: 'answer', answer: '', durationMs: 1, usage: USAGE } as never

test('keep-warm pings before the cache goes cold, and stops at the next prompt from the terminal or the phone', async ($, on) => {
  const { w, clock } = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as never)
  await $.turn.complete(turn)
  expect((await $.command.run({ command: 'keepwarm', args: '2' } as never)).text).toContain('keeping the cache warm for 2h')

  await clock.advance(59 * MIN)
  expect(w.pings).toBe(1)
  expect((await $.command.run({ command: 'cacheclock', args: '' } as never)).text).toContain('keep-warm for')

  // A prompt sent through Remote Control means the person is back.
  await $.prompt.submit({ text: 'hi from my phone', origin: { kind: 'bridge' }, wait: false } as never)
  expect((await $.command.run({ command: 'cacheclock', args: '' } as never)).text).not.toContain('keep-warm')
  await clock.advance(120 * MIN)
  expect(w.pings).toBe(1)
})

test('without keep-warm the clock warns once before the cache goes cold', async ($, on) => {
  const { w, clock } = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as never)
  await $.turn.complete(turn)
  await clock.advance(56 * MIN)
  await clock.advance(1 * MIN)
  expect(w.toasts.filter(t => t.startsWith('Cache goes cold'))).toHaveLength(1)
  expect(w.pings).toBe(0)
})
