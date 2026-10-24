import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

import { AGENT_DECIDES } from '../hooks/logic'

const OPTIONS = { options: { mode: 'auto', minWords: 5 } }
const USAGE = { input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
const DUMP = 'um so fix that file please, on the uh branch we talked about'
const submit = (text: string) => ({ text, origin: { kind: 'composer' as const }, wait: false })

type Calls = { forks: string[]; analyses: string[]; rewrites: string[]; systems: string[]; asked: { question: string; header: string; options: string[] }[]; filled: string[]; statuses: string[]; dialogs: string[][] }

// The engine beneath the mod: model replies, the question dialog, the prompt box, the clock.
function engine(on: On, replies: { fork: unknown; analysis?: string; rewrites: string[]; answer: (q: { question: string; header: string }) => string }) {
  const calls: Calls = { forks: [], analyses: [], rewrites: [], systems: [], asked: [], filled: [], statuses: [], dialogs: [] }
  const clock = mock.clock(on)
  mock.store(on)
  on('ui.status', (_, e) => {
    if (e.text !== undefined) calls.statuses.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('model.fork', (_, e) => {
    calls.forks.push(e.prompt)
    return { value: replies.fork as never }
  })
  on('model.complete', (_, e) => {
    if (e.system === undefined) {
      calls.analyses.push(e.prompt)
      return { value: { isAnswered: true, text: replies.analysis ?? '{"questions":[]}', usage: USAGE } }
    }
    calls.rewrites.push(e.prompt)
    calls.systems.push(e.system)
    return { value: { isAnswered: true, text: replies.rewrites[calls.rewrites.length - 1] ?? '', usage: USAGE } }
  })
  on('tool.call', { tool: 'AskUserQuestion' }, (_, e) => {
    calls.dialogs.push(e.questions.map(q => q.header))
    const answers: Record<string, string> = {}
    for (const q of e.questions) {
      calls.asked.push({ question: q.question, header: q.header, options: q.options.map(o => o.label) })
      answers[q.question] = replies.answer(q)
    }
    return { result: { questions: e.questions, answers } } as never
  })
  on('prompt.fill', (_, e) => {
    calls.filled.push(e.text)
    return { isFilled: true, text: e.text, cursor: e.text.length } as never
  })
  on('prompt.submit', (_, e) => ({ text: e.text }))
  return { calls, clock }
}

test('gaps are asked, settled and checked again; the reviewed prompt is sent as is', OPTIONS, async ($, on) => {
  const { calls, clock } = engine(on, {
    // Point 4: the fork sees the conversation and resolves "that file".
    fork: { isAnswered: true, usage: USAGE, text: '{"resolved":["\'that file\' = apps/web/a.ts"],"questions":[{"question":"Which branch?","header":"Scope","options":["dev"]}]}' },
    rewrites: [
      'Task: Fix apps/web/a.ts.\n\nScope:\n- the dev branch',
      'Task: Fix apps/web/a.ts.\n\nScope:\n- the dev branch\n\nAgent decides:\n- how to check the fix',
    ],
    answer: q => (q.header === 'Done when' ? AGENT_DECIDES : 'dev'),
  })

  const first = await $.prompt.submit(submit(DUMP))
  await clock.advance(200)

  expect(first.drop).toContain('The rewrite is in the prompt box')
  expect(calls.forks).toHaveLength(1)
  expect(calls.analyses).toHaveLength(0)
  // Point 3: every question offers the agent-decides way out.
  expect(calls.asked.every(q => q.options.includes(AGENT_DECIDES))).toBe(true)
  // Point 2: the first rewrite lacked "Done when", so it was asked, and nothing more after it was left to the agent.
  expect(calls.asked.map(q => q.header)).toEqual(['Scope', 'Done when'])
  expect(calls.rewrites).toHaveLength(2)
  expect(calls.rewrites[0]).toContain("'that file' = apps/web/a.ts")
  expect(calls.rewrites[0]).toContain('Which branch? → dev')
  expect(calls.rewrites[1]).toContain(`→ ${AGENT_DECIDES}`)
  expect(calls.filled).toEqual(['Task: Fix apps/web/a.ts.\n\nScope:\n- the dev branch\n\nAgent decides:\n- how to check the fix'])

  // Point 6: the edited rewrite is long enough to trigger, yet goes out untouched and costs no model call.
  const edited = `${calls.filled[0]}\n- and keep the old API working please`
  const second = await $.prompt.submit(submit(edited))
  expect(second.text).toBe(edited)
  expect(calls.rewrites).toHaveLength(2)

  // The review pass-through is spent: the next long prompt is a brain dump again.
  await $.prompt.submit(submit(DUMP))
  expect(calls.forks).toHaveLength(2)
})

test('several questions open as tabs in one dialog, each answer kept', OPTIONS, async ($, on) => {
  const { calls, clock } = engine(on, {
    fork: { isAnswered: true, usage: USAGE, text: '{"resolved":[],"questions":[{"question":"Which branch?","header":"Scope","options":["dev"]},{"question":"Which test?","header":"Done when","options":["unit"]}]}' },
    rewrites: ['Task: Fix it.\n\nScope:\n- dev\n\nDone when:\n- unit'],
    answer: q => (q.header === 'Scope' ? 'dev' : 'unit'),
  })

  await $.prompt.submit(submit(DUMP))
  await clock.advance(200)

  expect(calls.dialogs).toEqual([['Scope', 'Done when']])
  expect(calls.rewrites[0]).toContain('Which branch? → dev')
  expect(calls.rewrites[0]).toContain('Which test? → unit')
})

test('with nothing to fork, the brain dump alone is read and no reference is resolved', OPTIONS, async ($, on) => {
  const { calls, clock } = engine(on, {
    fork: { isAnswered: false, reason: 'nothing-to-fork' },
    analysis: '{"resolved":["\'that file\' = made-up.ts"],"questions":[]}',
    rewrites: ['Task: Fix the file.\n\nScope:\n- dev\n\nDone when:\n- tests pass'],
    answer: () => 'dev',
  })

  await $.prompt.submit(submit(DUMP))
  await clock.advance(200)

  expect(calls.forks).toHaveLength(1)
  expect(calls.analyses).toHaveLength(1)
  expect(calls.rewrites[0]).not.toContain('made-up.ts')
  expect(calls.asked).toHaveLength(0)
})

test('a bug report gets the bug template: its sections are written and its missing symptom is asked', OPTIONS, async ($, on) => {
  const { calls, clock } = engine(on, {
    fork: { isAnswered: true, usage: USAGE, text: '{"intent":"bug","resolved":[],"questions":[]}' },
    rewrites: ['Evidence:\n- 401 after refresh\n\nDone when:\n- the login test passes', 'Symptom: Login fails after a timeout.\n\nDone when:\n- the login test passes'],
    answer: () => 'Login fails after a timeout',
  })

  const first = await $.prompt.submit(submit(DUMP))
  await clock.advance(200)

  expect(first.drop).toContain('read as a bug fix')
  expect(calls.systems[0]).toContain('Symptom:')
  expect(calls.systems[0]).not.toContain('Task:')
  // "Scope" and "Task" are not required for a bug, so only the symptom is asked.
  expect(calls.asked.map(q => q.header)).toEqual(['Symptom'])
  expect(calls.filled[0]).toContain('Symptom: Login fails')
})

test('with questions off, the intent is still picked and nothing is asked', { options: { ...OPTIONS.options, askQuestions: false } }, async ($, on) => {
  const { calls, clock } = engine(on, {
    fork: { isAnswered: true, usage: USAGE, text: '{"intent":"question","resolved":[],"questions":[{"question":"Which?","header":"Deliverable"}]}' },
    rewrites: ['Question: Why is it slow?'],
    answer: () => 'x',
  })

  const first = await $.prompt.submit(submit(DUMP))
  await clock.advance(200)

  expect(first.drop).toContain('read as a question or research')
  expect(calls.asked).toHaveLength(0)
  expect(calls.rewrites).toHaveLength(1)
})

test('saying it is a brain dump skips the confirm dialog and the status says why', { options: { mode: 'confirm', minWords: 500 } }, async ($, on) => {
  const { calls, clock } = engine(on, {
    fork: { isAnswered: true, usage: USAGE, text: '{"intent":"general","resolved":[],"questions":[]}' },
    rewrites: ['Task: Fix it.\n\nScope:\n- the mod\n\nDone when:\n- tests pass'],
    answer: () => 'x',
  })

  const first = await $.prompt.submit(submit('So, it’s a brain dump. Fix the mod please'))
  await clock.advance(200)

  expect(first.drop).toContain('The rewrite is in the prompt box')
  // No "This looks like a brain dump" dialog, though the prompt is far below minWords.
  expect(calls.asked).toHaveLength(0)
  expect(calls.statuses[0]).toContain('you said it is a brain dump')
  expect(calls.rewrites[0]).toContain('it’s a brain dump')
})

test('/braindump shows every way to force or skip a rewrite', async $ => {
  const r = await $.command.run({ command: 'braindump', args: '', origin: { kind: 'plugin', name: 'test' }, presentation: { isFullscreen: false, columns: 120 } })
  expect(r.text).toContain('"bd" first or last, or say "this is a brain dump"')
  expect(r.text).toContain('skip: "raw" first')
})

test('/braindump on turns the mode back to confirm', async ($, on) => {
  const written: unknown[] = []
  on('config.set', (_, e) => {
    written.push(e)
    return { value: (e as { value: unknown }).value } as never
  })
  const r = await $.command.run({ command: 'braindump', args: 'on' } as never)
  expect(r.text).toBe('brain-dump: mode = confirm')
  expect(written).toEqual([expect.objectContaining({ key: 'brain-dump.mode', value: 'confirm' })])
})

for (const keepHistory of [false, true]) {
  test(`history is written only when keepHistory is on (${keepHistory})`, { options: { mode: 'auto', minWords: 5, review: false, askQuestions: false, keepHistory } }, async ($, on) => {
    engine(on, { fork: { isAnswered: true, usage: USAGE, text: '{"intent":"general","resolved":[],"questions":[]}' }, rewrites: ['Task: Fix it.'], answer: () => 'x' })
    const writes: string[] = []
    on('process.run', (_, e) => {
      writes.push(e.argv.join(' '))
      return { value: { exitCode: 0, stdout: '', stderr: '' } as never }
    })
    const r = await $.prompt.submit(submit(DUMP))
    expect(r.text).toBe('Task: Fix it.')
    for (let i = 0; i < 5; i++) await Promise.resolve()
    expect(writes.some(w => w.includes('history.jsonl'))).toBe(keepHistory)
  })
}
