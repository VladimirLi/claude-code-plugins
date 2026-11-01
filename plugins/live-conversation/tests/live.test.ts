import type { On, ProcessSpawnRequest } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const RUN = '/tmp/claude-live-conversation/s1'

function engine(on: On, opts: { hasVenv?: boolean; setupFails?: boolean } = {}) {
  const w = {
    status: [] as (string | undefined)[],
    toasts: [] as string[],
    prompts: [] as string[],
    writes: [] as { path: string; text: string }[],
    runs: [] as string[][],
    spawned: [] as string[][],
    lines: [] as string[],
    wake: () => {},
    // the daemon writes one line on stderr
    say: (line: string) => {
      w.lines.push(line)
      w.wake()
    },
  }
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { TMPDIR: '/tmp/' })
  on('command.register', () => ({ value: { command: 'live' } }) as never)
  on('ui.status', (_, e) => {
    w.status.push((e as { text?: string }).text)
    return { value: undefined }
  })
  on('ui.toast', (_, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('session.id', () => ({ value: 's1' }))
  on('session.root', () => ({ value: '/work/project' }))
  on('fs.exists', () => ({ value: opts.hasVenv ?? true }))
  on('fs.read', () => ({ value: '4242\n' }))
  on('fs.write', (_, e) => {
    w.writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  on('process.run', (_, e) => {
    w.runs.push([...e.argv])
    if (e.argv.includes('--check')) return { value: { exitCode: 1, stdout: '', stderr: '' } } as never
    if (e.argv[0] === 'kill') w.say('EXIT')
    return { value: { exitCode: 0, stdout: '/usr/bin/rec\n', stderr: '' } } as never
  })
  on('process.spawn', async function* (_: unknown, e: ProcessSpawnRequest) {
    w.spawned.push([...e.argv])
    if (e.argv[1]?.endsWith('bin/live-setup')) {
      yield { stream: 'stdout' as const, text: 'setup: installing speech packages\n' }
      yield { stream: 'stdout' as const, text: opts.setupFails ? 'setup failed: needs Python 3.11 or newer, or uv\n' : 'setup: done\n/home/v/venv/bin/python\n' }
      return { value: { code: opts.setupFails ? 1 : 0, signal: null } }
    }
    for (;;) {
      const line = w.lines.shift()
      if (line === 'EXIT') break
      if (line !== undefined) yield { stream: 'stderr' as const, text: line + '\n' }
      else await new Promise<void>(r => (w.wake = r))
    }
    return { value: { code: 0, signal: null } }
  } as never)
  on('prompt.submit', (_, e) => {
    w.prompts.push(e.text)
    return { text: e.text }
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }) as never)
  return { w, clock }
}


test('an installed copy sets itself up on the first /live, then starts the daemon', async ($, on) => {
  const { w, clock } = engine(on, { hasVenv: false })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as never)
  const { text } = await $.command.run({ command: 'live', args: '' } as never)
  expect(text).toContain('First run: setting up')
  await clock.advance(0)
  expect(w.spawned[0]![1]).toMatch(/bin\/live-setup$/)
  expect(w.status).toContain('live: setting up: installing speech packages')
  expect(w.spawned[1]).toEqual(['/home/v/venv/bin/python', '-m', 'live', '--cwd', '/work/project', '--run-dir', RUN])
})

test('a failed setup says why and leaves live off', async ($, on) => {
  const { w, clock } = engine(on, { hasVenv: false, setupFails: true })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as never)
  await $.command.run({ command: 'live', args: '' } as never)
  await clock.advance(0)
  expect(w.spawned).toHaveLength(1)
  expect(w.toasts.at(-1)).toContain('setup failed: needs Python 3.11 or newer, or uv')
  expect(w.status.at(-1)).toBeUndefined()
  expect((await $.command.run({ command: 'live', args: 'status' } as never)).text).toContain('Live conversation is off')
})

test('/live starts the daemon, a handed-off task goes to the session and its answer back to the daemon', async ($, on) => {
  const { w, clock } = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as never)
  expect((await $.command.run({ command: 'live', args: '' } as never)).text).toContain('starting')
  await clock.advance(0)
  expect(w.spawned[0]!.slice(1)).toEqual(['-m', 'live', '--cwd', '/work/project', '--run-dir', RUN])
  expect(w.spawned[0]![0]).toMatch(/\.venv\/bin\/python$/)

  w.say('{"t_ms": 0, "event": "session_start", "turn": null}')
  w.say('{"t_ms": 10, "event": "task_handoff", "turn": null, "task": 1, "text": "Add a verbose flag to the CLI"}')
  await clock.advance(0)
  expect(w.status.at(-1)).toBe('live: listening · task 1 running')
  expect(w.prompts).toHaveLength(1)
  expect(w.prompts[0]).toContain('[live voice task 1]')
  expect(w.prompts[0]).toContain('Add a verbose flag to the CLI')

  await $.turn.start({ text: w.prompts[0]!, turnId: 'turn-9' } as never)
  await $.turn.complete({ turnId: 'turn-9', reason: 'answer', answer: 'Added --verbose. Tests pass.', durationMs: 5, isAborted: false } as never)
  const out = w.writes.find(x => x.path === `${RUN}/tasks/1.result.json`)
  expect(out).toBeDefined()
  expect(JSON.parse(out!.text)).toEqual({ task: 1, reason: 'answer', answer: 'Added --verbose. Tests pass.', durationMs: 5 })

  // another turn of the session is no task's
  await $.turn.start({ text: 'unrelated', turnId: 'turn-10' } as never)
  await $.turn.complete({ turnId: 'turn-10', reason: 'answer', answer: 'x', durationMs: 1, isAborted: false } as never)
  expect(w.writes).toHaveLength(1)

  const status = (await $.command.run({ command: 'live', args: 'status' } as never)).text
  expect(status).toContain('Task 1 (done, not spoken yet): Add a verbose flag to the CLI')

  const stop = $.command.run({ command: 'live', args: 'stop' } as never)
  await clock.advance(0)
  expect((await stop).text).toBe('Live conversation stopped.')
  expect(w.runs.some(r => r[0] === 'kill' && r[2] === '4242')).toBe(true)
  expect((await $.command.run({ command: 'live', args: 'status' } as never)).text).toContain('Live conversation is off')
})

test('/live feedback marks the moment in the recording, /live review asks the session to review it', async ($, on) => {
  const { w, clock } = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as never)
  expect((await $.command.run({ command: 'live', args: 'feedback too early' } as never)).text).toContain('No recorded')
  await $.command.run({ command: 'live', args: '' } as never)
  await clock.advance(0)
  w.say('{"t_ms": 0, "event": "x_recording", "turn": null, "path": "/rec/s1"}')
  w.say('{"t_ms": 5300, "event": "barge_in_detected", "turn": 2}')
  await clock.advance(0)

  const fb = (await $.command.run({ command: 'live', args: 'feedback it stopped but I said nothing' } as never)).text
  expect(fb).toContain('Noted')
  const note = w.writes.find(x => x.path === '/rec/s1/feedback.jsonl')
  expect(note).toBeDefined()
  const row = JSON.parse(note!.text.trim().split('\n').at(-1)!)
  expect(row).toMatchObject({ t_ms_hint: 5300, text: 'it stopped but I said nothing' })
  expect(typeof row.t_wall).toBe('number')

  expect((await $.command.run({ command: 'live', args: 'review' } as never)).text).toContain('/rec/s1')
  await clock.advance(0)
  expect(w.prompts.at(-1)).toContain('bin/live-review" "/rec/s1"')
})
