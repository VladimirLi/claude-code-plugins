import type { EngineInterface, Register } from 'claude-code'

// /live starts the voice daemon (live/ in this plugin, run with the plugin's .venv)
// working in this session's folder; /live again (or /live stop) stops it; /live status
// says what it is doing. The daemon prints one JSON event per line on stderr, and the
// status line shows them.
//
// Task handoff: the voice brain runs bin/live-task, the daemon prints `task_handoff`,
// and this mod submits the task to this session as a prompt. When that prompt's turn
// ends, the mod writes the answer to <run dir>/tasks/<n>.result.json, and the daemon
// has the voice brain speak it.
//
// Every session is recorded (live/recorder.py). /live feedback <note> marks the moment in
// that recording; /live review asks this session to review it (bin/live-review) and propose fixes.

type Task = { n: number; text: string; turnId?: string; state: 'queued' | 'running' | 'done' | 'spoken' }

type Daemon = {
  runDir: string
  cwd: string
  phase: string
  startedAt: number
  isStopping: boolean
  kill: () => void
  ended: Promise<void>
  tail: string[] // last lines that were not events (errors)
  recording?: string // session folder (live/recorder.py): audio, events, feedback
  lastTms: number // clock of the last event, to place feedback notes
}

let daemon: Daemon | undefined
const tasks = new Map<number, Task>()
let expecting: number | undefined // a task prompt is entering the session; its turn starts next
let lastRecording: string | undefined // kept after /live stops, so feedback and review still work

async function runDirOf($: EngineInterface): Promise<string> {
  const tmp = ((await $.env.get('TMPDIR')) || '/tmp').replace(/\/+$/, '')
  return `${tmp}/claude-live-conversation/${await $.session.id()}`
}

const mark = (n: number) => `[live voice task ${n}]`
const MARK = /\[live voice task (\d+)\]/

export function taskPrompt(n: number, text: string): string {
  return (
    `${mark(n)} The user asked for this in the live voice conversation (/live):\n\n${text}\n\n` +
    'Do it now. End with a short plain summary of what you changed and whether it worked, ' +
    'without markdown: it is read aloud to the user.'
  )
}

export function phaseOf(event: { event?: string; text?: string }): string | undefined {
  switch (event.event) {
    case 'session_start': return 'listening'
    case 'turn_end_detected': return 'thinking'
    case 'final_transcript': return `heard "${(event.text ?? '').slice(0, 50)}"`
    case 'first_reply_audio': return 'speaking'
    case 'reply_audio_end': return 'listening'
    case 'barge_in_detected': return 'listening (interrupted)'
    default: return undefined
  }
}

function running(): Task[] {
  return [...tasks.values()].filter(t => t.state === 'queued' || t.state === 'running')
}

function showStatus($: EngineInterface): void {
  if (!daemon) return
  const busy = running()
  const extra = busy.length === 1 ? ` · task ${busy[0]?.n} running` : busy.length > 1 ? ` · ${busy.length} tasks running` : ''
  $.ui.status(`live: ${daemon.phase}${extra}`)
}

function onEvent($: EngineInterface, e: Record<string, unknown>): void {
  const d = daemon
  if (!d) return
  if (typeof e.t_ms === 'number' && e.t_ms > d.lastTms) d.lastTms = e.t_ms
  if (e.event === 'x_recording' && typeof e.path === 'string') {
    d.recording = e.path
    lastRecording = e.path
  } else if (e.event === 'task_handoff' && typeof e.task === 'number' && typeof e.text === 'string') {
    const n = e.task
    if (!tasks.has(n)) {
      tasks.set(n, { n, text: e.text, state: 'queued' })
      // a turn of its own once the session is idle; turn.start below binds it to the task
      void $.prompt.submit({ text: taskPrompt(n, e.text) })
      $.ui.toast(`live: task ${n} handed to this session`)
    }
  } else if (e.event === 'task_result' && typeof e.task === 'number') {
    const t = tasks.get(e.task)
    if (t) t.state = 'spoken'
  } else if (e.event === 'x_warmup_failed') {
    $.ui.toast(`live: ${String(e.what)} did not start (${String(e.error).slice(0, 120)})`)
  } else {
    const p = phaseOf(e as { event?: string; text?: string })
    if (p) d.phase = p
  }
  showStatus($)
}

async function startDaemon($: EngineInterface): Promise<string> {
  const root = $.plugin.root
  const python = `${root}/.venv/bin/python`
  if (!(await $.fs.exists(python))) {
    return (
      'Live conversation needs a one-time setup first (see the plugin README). It needs Python 3.11 or newer\n' +
      '(macOS ships 3.9: brew install sox python, then use the Homebrew python3):\n' +
      `  cd "${root}"\n  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt\n` +
      '  .venv/bin/pip install --no-deps -r requirements-nodeps.txt'
    )
  }
  const which = await $.process.run(['/bin/sh', '-c', 'command -v rec && command -v play']).catch(() => undefined)
  if (!which || which.exitCode !== 0) return 'Live conversation needs sox for the microphone: brew install sox'

  const cwd = await $.session.root()
  const runDir = await runDirOf($)
  let resolveEnded = () => {}
  const ended = new Promise<void>(r => (resolveEnded = r))
  const d: Daemon = {
    runDir, cwd, phase: 'starting', startedAt: await $.clock.now(), isStopping: false,
    kill: () => {}, ended, tail: [], lastTms: 0,
  }
  daemon = d

  // Start from a timer, so the child belongs to no command's dispatch and lives on.
  $.clock.after(0, () => {
    void (async () => {
      const child = $.process.spawn({
        argv: [python, '-m', 'live', '--cwd', cwd, '--run-dir', runDir],
        cwd: root,
        env: { PYTHONUNBUFFERED: '1' },
      })
      d.kill = () => void child.return(undefined as never)
      let rest = ''
      try {
        for await (const { stream, text } of child) {
          if (stream !== 'stderr') continue
          rest += text
          const lines = rest.split('\n')
          rest = lines.pop() ?? ''
          for (const line of lines) {
            let e: Record<string, unknown> | undefined
            try {
              e = line.startsWith('{') ? (JSON.parse(line) as Record<string, unknown>) : undefined
            } catch {
              e = undefined
            }
            if (e && typeof e.event === 'string') onEvent($, e)
            else if (line.trim()) d.tail = [...d.tail.slice(-19), line]
          }
        }
        if (!d.isStopping) {
          const why = d.tail.filter(l => !l.startsWith(' ')).at(-1) ?? 'it exited'
          $.ui.toast(`live: the voice daemon stopped (${why.slice(0, 160)}). /live status has more.`, { timeoutMs: 10000 })
        }
      } catch (err) {
        $.ui.toast(`live: could not start the voice daemon (${String(err).slice(0, 160)}). See the plugin README.`)
      } finally {
        if (daemon === d) daemon = undefined
        $.ui.status(undefined)
        resolveEnded()
      }
    })()
  })
  $.ui.status('live: starting')
  return (
    'Live conversation starting: it is ready in about 30 seconds, when the status line says "live: listening". ' +
    'Talk; /live stops it. ' +
    'Use headphones. Ask for a code change and the voice hands it to this session, then tells you the result.'
  )
}

async function stopDaemon($: EngineInterface): Promise<string> {
  const d = daemon
  if (!d) return 'Live conversation is not running.'
  d.isStopping = true
  // SIGTERM lets the daemon close its claude processes and the microphone itself.
  try {
    const pid = (await $.fs.read(`${d.runDir}/daemon.pid`)).trim()
    if (/^\d+$/.test(pid)) await $.process.run(['kill', '-TERM', pid])
  } catch {
    // no pid yet (still starting): the kill below ends it
  }
  const timer = new Promise<void>(r => $.clock.after(4000, r))
  await Promise.race([d.ended, timer])
  d.kill()
  if (daemon === d) daemon = undefined
  $.ui.status(undefined)
  return 'Live conversation stopped.'
}

async function addFeedback($: EngineInterface, text: string): Promise<string> {
  const dir = daemon?.recording ?? lastRecording
  if (!dir) return 'No recorded live session yet. Start one with /live.'
  if (!text) return 'Usage: /live feedback <what felt wrong>, right after it happens. It marks that moment.'
  const path = `${dir}/feedback.jsonl`
  const line = JSON.stringify({ t_wall: (await $.clock.now()) / 1000, t_ms_hint: daemon?.lastTms, text }) + '\n'
  let before = ''
  try {
    before = await $.fs.read(path)
  } catch {
    before = ''
  }
  await $.fs.write(path, before + line)
  return `Noted at this moment of the live session: "${text}". /live review goes through all notes.`
}

export function reviewPrompt(root: string, dir: string): string {
  return (
    `Review the recorded live voice session in ${dir} and improve the live-conversation plugin (${root}).\n\n` +
    `1. Run: "${root}/.venv/bin/python" "${root}/bin/live-review" "${dir}" and read the report.\n` +
    '2. For each of my feedback notes, each barge-in marked FALSE? and each odd turn, find the cause in the events ' +
    'and the code (live/*.py). The clips in clips/ are stereo: left is my microphone, right is what the speaker played.\n' +
    '3. Tell me, in plain words, what went wrong and the fix you propose for each. Do not change code yet; ' +
    'wait for my go.'
  )
}

async function statusText($: EngineInterface): Promise<string> {
  const lines: string[] = []
  const d = daemon
  if (!d) lines.push('Live conversation is off. /live starts it.')
  else {
    const mins = Math.round(((await $.clock.now()) - d.startedAt) / 60000)
    lines.push(`Live conversation: ${d.phase}, for ${mins} min, working in ${d.cwd}.`)
    lines.push(`Run folder: ${d.runDir}`)
  }
  if (lastRecording) lines.push(`Recording: ${lastRecording} (/live feedback <note> marks a moment, /live review reviews it)`)
  for (const t of tasks.values()) {
    const state = { queued: 'waiting for this session', running: 'running', done: 'done, not spoken yet', spoken: 'done and spoken' }[t.state]
    lines.push(`Task ${t.n} (${state}): ${t.text}`)
  }
  if (d && d.tail.length) lines.push('Last daemon output:', ...d.tail.slice(-5).map(l => `  ${l}`))
  return lines.join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'live',
      description: 'Live voice conversation with Claude: start, stop, or status',
      argumentHint: '[start|stop|status|feedback <note>|review]',
      immediate: true,
    })
    return next(e)
  })

  on('command.run', { command: 'live' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'status') return { text: await statusText($) }
    if (arg.startsWith('feedback')) return { text: await addFeedback($, e.args.trim().slice('feedback'.length).trim()) }
    if (arg === 'review') {
      const dir = daemon?.recording ?? lastRecording
      if (!dir) return { text: 'No recorded live session yet. Start one with /live.' }
      // from a timer: a prompt submitted inside this command would wait on the command itself
      $.clock.after(0, () => void $.prompt.submit({ text: reviewPrompt($.plugin.root, dir) }))
      return { text: `Reviewing ${dir}.` }
    }
    if (arg === 'stop' || (arg === '' && daemon)) return { text: await stopDaemon($) }
    if (arg === 'start' || arg === '') {
      if (daemon) return { text: 'Live conversation is already running. /live stop stops it.' }
      return { text: await startDaemon($) }
    }
    return { text: 'Usage: /live [start|stop|status|feedback <note>|review]. /live alone starts or stops.' }
  })

  // Bind a handed-off task to the turn its prompt starts: by the mark in the turn's text,
  // or else as the turn that starts while the task's prompt enters.
  on('prompt.submit', async ($, e, next) => {
    const m = e.origin.kind === 'plugin' ? MARK.exec(e.text) : null
    if (!m) return next(e)
    expecting = Number(m[1])
    try {
      return await next(e)
    } finally {
      expecting = undefined
    }
  }).catch(($, e, next) => next(e)) // never refuses a prompt, even if it fails

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      const m = MARK.exec(e.text)
      const n = m ? Number(m[1]) : expecting
      const t = n === undefined ? undefined : tasks.get(n)
      if (t && !t.turnId) {
        t.turnId = e.turnId
        t.state = 'running'
        showStatus($)
      }
    }
    return next(e)
  })

  // The task's turn ended: hand its answer back to the daemon, which speaks it.
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      const t = [...tasks.values()].find(x => x.turnId === e.turnId)
      if (t && t.state === 'running') {
        t.state = 'done'
        const runDir = daemon?.runDir ?? (await runDirOf($))
        const body = { task: t.n, reason: e.reason, answer: e.answer, durationMs: e.durationMs }
        await $.fs.write(`${runDir}/tasks/${t.n}.result.json`, JSON.stringify(body))
        showStatus($)
      }
    }
    return result
  })

  on('session.end', async ($, e, next) => {
    if (daemon) await stopDaemon($)
    return next(e)
  })
}
