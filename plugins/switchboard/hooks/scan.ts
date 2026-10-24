// Pure parts of the scan, kept apart so tests reach them without the engine.

export type Installed = { id: string; version: string; enabled: boolean; installPath: string }

function parseList(json: string): Installed[] {
  try {
    const list = JSON.parse(json)
    return Array.isArray(list)
      ? list.filter(p => typeof p?.id === 'string' && typeof p?.installPath === 'string').map(p => ({ id: p.id, version: String(p.version ?? ''), enabled: p.enabled === true, installPath: p.installPath }))
      : []
  } catch {
    return []
  }
}

// `@inline` plugins come from --plugin-dir or CLAUDE_CODE_PLUGIN_DIRS: `claude plugin enable|disable` cannot toggle them, so they are session rows.
export const parseInstalled = (json: string) => parseList(json).filter(p => !p.id.endsWith('@inline'))
export const parseInline = (json: string) => parseList(json).filter(p => p.id.endsWith('@inline'))

// The hooks files a manifest points at: its `hooks` entry (a path or a list), else the default.
export function hooksFiles(manifest: string): string[] {
  try {
    const hooks = JSON.parse(manifest).hooks
    const listed = typeof hooks === 'string' ? [hooks] : Array.isArray(hooks) ? hooks.filter((h): h is string => typeof h === 'string') : []
    return ['./hooks/hooks.json', ...listed].map(h => h.replace(/^\.\//, ''))
  } catch {
    return ['hooks/hooks.json']
  }
}

// A plugin is a mod when one of its hooks files names a hooks module.
export function namesModule(hooksJson: string): boolean {
  try {
    const modules = JSON.parse(hooksJson).modules
    return Array.isArray(modules) && modules.length > 0
  } catch {
    return false
  }
}

export function findRow<T extends { name: string; id: string }>(rows: readonly T[], name: string): T | undefined {
  const n = name.toLowerCase()
  return rows.find(r => r.name.toLowerCase() === n || r.id.toLowerCase() === n)
}

export const HOTKEYS = '123456789abcdefghijklmnopqrstuvwxyz'
