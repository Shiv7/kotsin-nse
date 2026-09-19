export type Mode = 'SHADOW' | 'PAPER' | 'LIVE_CAPPED' | 'LIVE'

export interface Wallet {
  strategy: string
  initial: number
  balance: number
  peak: number
  day_start_balance: number
  day: string
  realized_pnl: number
  charges_paid: number
  trades: number
  wins: number
  losses: number
  deployed: number
  halted: boolean
  halt_reason: string
}

export interface PositionView {
  id: string
  strategy: string
  symbol: string
  scrip_code: string
  qty: number
  qty_remaining: number
  entry: number
  direction: 'BULLISH' | 'BEARISH'
  equity_entry: number
  equity_sl: number
  equity_targets: number[]
  option_sl: number
  option_targets: number[]
  targets_hit: number
  grade: string
  charges: number
  peak_r: number
  mfe_r: number
  mae_r: number
  opened_ist: string
  instrument: { name: string; scrip_code: string; lot_size: number; strike: number; option_type: string }
  ltp: number | null
  underlying_ltp: number | null
  unrealized: number | null
  r_now: number | null
  note: string
}

export interface Overview {
  mode: Mode
  armed_until: number | null
  halted: boolean
  halt_reason: string
  wallets: Wallet[]
  capital: number
  day_pnl: number
  positions: PositionView[]
  exposure: {
    gross: number
    gross_pct: number
    by_underlying: Record<string, { outlay: number; pct: number }>
  }
  universe: number
  boot_notes: string[]
}

export interface GateResult {
  name: string
  passed: boolean
  required: boolean
  value: number | null
  threshold: number | null
  missing: boolean
  note: string
}

export interface SignalRow {
  signal_id: string
  strategy: string
  symbol: string
  direction: string
  ts: number
  entry: number
  stop: number
  targets: number[]
  grade: string
  rr: number
  score: number
  reason: string
  gates: GateResult[]
  evidence: Record<string, number>
  source_signal_id: string
}

export interface RejectionRow {
  strategy: string
  symbol: string
  ts: number
  direction: string | null
  binding_gate: string
  gates: GateResult[]
  evidence: Record<string, number>
  note: string
}

export interface TradeRow {
  id: string
  strategy: string
  symbol: string
  underlying: string
  qty: number
  entry: number
  exit: number
  gross: number
  charges: number
  net: number
  r_multiple: number
  mfe_r: number
  mae_r: number
  exit_reason: string
  opened_ts: number
  closed_ts: number
  duration_s: number
  grade: string
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
  zones: { price: number; strength: number; members: string[] }[]
}

export interface GateStats {
  candidates: number
  passed: number
  by_gate: Record<string, { evaluated: number; rejected: number; missing: number; binding: number }>
}

export interface StrategyView {
  key: string
  config: Record<string, unknown>
  gates: GateStats
  binding: { gate: string; count: number }[]
  wallet: Wallet
  multipliers?: Record<string, number>
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
    connected: boolean
    messages: number
    ticks: number
    depth: number
    oi: number
    reconnects: number
    silence_s: number | null
    subscriptions: Record<string, number>
  }
  bars: Record<string, number>
  gateway: Record<string, unknown>
  rest: Record<string, unknown>
  catalogue: Record<string, unknown>
  telegram: Record<string, unknown>
  positions_open: number
  boot_notes: string[]
}

export interface Pnl {
  trades: number
  gross?: number
  charges?: number
  net?: number
  charges_share_of_gross?: number | null
  win_rate?: number
  avg_r?: number
  by_exit_reason?: Record<string, { n: number; net: number }>
}

export interface UniverseRow {
  symbol: string
  scrip_code: string
  segment: string
  ltp: number | null
  bars_30m: number
  bars_1d: number
  zones: number
}
