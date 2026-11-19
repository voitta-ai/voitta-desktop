/** One account the gateway can route a window to (GET /_gw/api/llm/options). */
export type LlmOption = {
  id: string
  label: string
  provider: string
  provider_label: string
  status: string
  status_detail: string
  main: string | null
  background: string | null
}

/** The gateway's view of this window: its pick, the global default, the choices. */
export type LlmView = {
  session: string
  pick: string | null
  default: LlmOption | null
  options: LlmOption[]
  ui: string
}

declare module 'claude-code' {
  interface PluginState {
    'voitta-llm': { view: LlmView | null; error: string | null }
  }
}
