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

export interface BoardRow {
  symbol: string
  sector: string
  last: number
  pct: number | null
  /** the 09:15 open against yesterday's close, % (engines from 2026-10-23) */
  gap_pct?: number | null
  score: number
  side: 'BULLISH' | 'BEARISH' | 'NONE'
  rank: number | null
  percentile: number | null
  event_risk: boolean
  above_vwap: boolean | null
  oi_build_up: string | null
  oi_change_pct: number | null
  /** when the OI reference was taken, if not at the previous close (MCX without a recorded close) */
  oi_since?: number | null
  pick: boolean
  rule: string
  align: number | null
  setup: { z: number; s1: number; r1: number | null; r2: number | null; rr: number | null; state: string; distance_pct: number | null } | null
}

export interface SectorRow {
  sector: string
  stocks: number
  avg_pct: number | null
  avg_score: number
  up: number
  down: number
  cells: { symbol: string; pct: number | null }[]
}

export interface BoardData {
  mode: 'live' | 'replay'
  ts: number
  day?: string
  at?: string
  symbols?: number
  without_adr?: string[]
  rows: BoardRow[]
  bullish: string[]
  bearish: string[]
  picks: string[]
  breadth: { bullish: number; bearish: number; neutral: number; up: number; down: number; total: number }
  sectors: SectorRow[]
}

export interface OHLCV {
  ts: number
  o: number
  h: number
  l: number
  c: number
  v: number
}

export interface StructZone {
  side: 'BULLISH' | 'BEARISH'
  ob_ts: number
  bos_ts: number
  bos_kind: string
  low: number
  high: number
  z: number
  s1: number
  impulse: string
  state: string
  retests: number
  broken_ts: number | null
  swept_before: boolean
}

export interface StructureData {
  zones: StructZone[]
  liquidity: { price: number; kind: string; ts: number | null }[]
  sweeps: { ts: number; level: number; kind: string; side: string }[]
  gaps: { ts: number; low: number; high: number; side: string; filled_at: number | null }[]
  setup: {
    side: string
    zone_index: number
    z: number
    s1: number
    r1: number | null
    r2: number | null
    rr: number | null
    extra: { price: number; kind: string }[]
    state: string
  } | null
  opening_range: [number, number] | null
}

export interface VolumeProfileData {
  step: number
  prices: number[]
  volumes: number[]
  poc: number
  vah: number
  val: number
  hvn: number[]
  lvn: number[]
  total: number
}

export interface MarketProfileData {
  step: number
  prices: number[]
  letters: string[]
  poc: number
  vah: number
  val: number
  ib_high: number | null
  ib_low: number | null
  single_prints: number[]
  poor_high: boolean
  poor_low: boolean
  day_type: string
  periods: number
}

export interface ProfileData {
  volume: VolumeProfileData | null
  market: MarketProfileData | null
  composite?: VolumeProfileData | null
  composite_sessions?: number
}

export interface PivotCluster {
  low: number
  high: number
  mid: number
  weight: number
  members: string[]
  kind: 'SUPPORT' | 'RESISTANCE' | 'AT_PRICE'
  distance_pct: number | null
}

export interface LevelsData {
  symbol: string
  adr: number | null
  last: number | null
  tolerance: number | null
  levels: { name: string; price: number; frame: 'D' | 'W' | 'M' }[]
  clusters: PivotCluster[]
}
