export type Mode = 'SHADOW' | 'PAPER' | 'LIVE_CAPPED' | 'LIVE'

export interface Overview {
  mode: Mode
  armed_until: number | null
  halted: boolean
  halt_reason: string
  universe: number
  market_open: boolean
  boot_notes: string[]
}

export interface Bar {
  ts: number
  ist: string
  o: number
  h: number
  l: number
  c: number
  v: number
  source: string
  vwap: number | null
  oi: number | null
  oi_change_pct: number | null
}

export interface BarsResponse {
  symbol: string
  tf: string
  bars: Bar[]
  forming: Bar | null
}

export interface Health {
  status: 'ok' | 'degraded'
  degraded: string[]
  uptime_s: number
  mode: Mode
  halted: boolean
  armed_until: number | null
  checks: { name: string; ok: boolean; detail: string; value: number | null; consecutive_failures: number }[]
  feed: {
    source: string
    connected: boolean
    messages: number
    ticks: number
    depth: number
    oi: number
    reconnects: number
    silence_s: number | null
    subscriptions: Record<string, number>
  }
  feed_rate: Record<string, Record<string, FeedRateRow>>
  bars: Record<string, number>
  gateway: Record<string, unknown>
  rest: Record<string, unknown>
  catalogue: Record<string, unknown>
  telegram: Record<string, unknown>
  boot_notes: string[]
}

export interface UniverseRow {
  symbol: string
  scrip_code: string
  segment: string
  kind: string
  ltp: number | null
  bars_30m: number
  bars_1d: number
  futures: string[]
  options: number
  option_expiry: string | null
  prev_close: number | null
  note: string
}

export interface EventRow {
  kind?: string
  ts?: number
  [k: string]: unknown
}

export interface FeedRateRow {
  instruments: number
  measured: number
  frames_per_min: number | null
  trades_per_min: number | null
  gap_median_s: number | null
  gap_p90_s: number | null
}
