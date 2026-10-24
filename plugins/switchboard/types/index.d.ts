export type ModRow = {
  id: string
  name: string
  version: string
  kind: 'session' | 'installed'
  isOn: boolean
  path: string
}

declare module 'claude-code' {
  interface PluginState {
    switchboard: { rows: Shaped<ModRow[]>; message: string }
  }
}
