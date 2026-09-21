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
  confidence: number
  reason: string
  gates: GateResult[]
  evidence: Record<string, number>
  source_signal_id: string
  decision?: string
  decision_reason?: string
  context?: SignalContext
}

export interface Zone {
  price: number
  strength: number
  wall: boolean
  members: string[]
}

export interface SignalContext {
  indicators?: {
    bb_upper: number
    bb_middle: number
    bb_lower: number
    st_value: number
    st_trend: number
    bars_in_trend: number
    atr: number
    params: Record<string, number>
  }
  confluence?: {
    stop: number
    stop_zone: string
    targets: number[]
    target_zones: string[]
    grade: string
    rr: number
    fortress: number
    room_ratio: number
    note: string
    policy: Record<string, number>
  }
  zones?: Zone[]
  conviction?: Record<string, number | string>
  volume?: Record<string, number | string | null>
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
  pnl?: { n: number; net: number; charges: number; last_closed_ts: number } | null
  last_signal?: { ts: number; symbol: string; direction: string; grade: string; decision: string; signal_id: string } | null
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
  kind: string
  ltp: number | null
  bars_30m: number
  bars_1d: number
  zones: number
  futures: string[]
  options: number
  option_expiry: string | null
  prev_close: number | null
  note: string
}

export interface IndicatorRow {
  ts: number
  bb_upper: number | null
  bb_middle: number | null
  bb_lower: number | null
  st_value: number | null
  st_trend: number | null
}

export interface IndicatorsResponse {
  symbol: string
  tf: string
  params: Record<string, number>
  rows: IndicatorRow[]
}

// -- review committee ------------------------------------------------------------------------------

export interface CommitteeStatus {
  available: boolean
  auto: boolean
  model: string | null
  runs_today: number
  max_runs_per_day: number
  running: string[]
  experiments_running: number
  errors: number
  last_error: string
  last_run_ts: number | null
  llm: { model: string; calls: number; errors: number; est_cost_usd: number; input_tokens?: number; output_tokens?: number } | null
  log: {
    entries: number
    by_kind: Record<string, number>
    by_failure_mode: Record<string, number>
    hypotheses: number
    hypotheses_by_status: Record<string, number>
    mean_confidence: number | null
  }
  path_bars: number
}

export interface Bucket {
  label: string
  n: number
  n_days?: number
  avg_r?: number
  t?: number | null
  win_rate?: number
  net?: number
  loss_share?: number
  too_small?: boolean
}

export interface Forensics {
  meta: { source: string; strategy: string | null; n: number; segment?: string | null; run?: Record<string, unknown> }
  cohort: Record<string, number | string | boolean | null | Record<string, number>>
  dims: Record<string, Bucket[]>
}

export interface ParamChange {
  path: string
  value: number
}

export interface ExperimentResult {
  verdict: string
  note?: string
  baseline?: { n: number; n_days: number; avg_r: number; avg_r_t: number | null; net: number; win_rate: number | null }
  patched?: { n: number; n_days: number; avg_r: number; avg_r_t: number | null; net: number; win_rate: number | null }
  delta_avg_r?: number | null
  p_value?: number | null
  symbols?: number
  seconds?: number
}

export interface HypothesisRow {
  id: string
  title: string
  rationale: string
  changes: ParamChange[]
  expected: string
  status: string
  result?: ExperimentResult | null
  reflection?: string | null
  error?: string | null
  review_id: string
  review_kind?: string
  review_ts?: number
  subject?: Record<string, unknown>
}

export interface CohortFinding {
  title: string
  failure_mode: string
  magnitude: string
  evidence_keys: string[]
  confidence: number
}

export interface ReviewRow {
  id: string
  kind: 'case' | 'cohort'
  ts: number
  strategy?: string | null
  symbol?: string | null
  subject?: Record<string, unknown>
  failure_mode?: string | null
  secondary?: string[]
  confidence?: number | null
  lesson?: string
  verdict?: string
  what_happened?: string
  why?: string
  counterfactual?: string
  findings?: CohortFinding[]
  not_explained?: string[]
  hypotheses?: HypothesisRow[]
  error?: string | null
  run?: { calls: number; seconds: number; analysts?: unknown[]; debate?: unknown; verdict?: unknown; report?: unknown }
  pack?: Record<string, unknown>
}
