import type { TradeMapCard } from './components/TradeMap'

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
  /** the deepest fall since it opened, from its best point (entry first) to a later low, on the bid: R, and ₹ on the full size */
  max_dd_r: number
  max_dd_inr: number | null
  note: string
}

export type StopRule = 'current' | 'E' | 'A'

/** One trade under one stop rule — the book's current stop or a stop-rule mirror's (operator, 2026-10-04). */
export interface StopRuleRow {
  rule: StopRule
  label: string
  book: string
  status: 'OPEN' | 'EXITED' | 'NONE'
  entry?: number
  qty?: number
  qtyRemaining?: number
  targetsHit?: number
  exitPrice?: number | null
  exitReason?: string | null
  closedTs?: number | null
  net?: number | null
  openGross?: number | null
  operatorClosedReal?: boolean
  stop?: { level: number; triggerPrice: number; triggerOn: string; bidAtTrigger: number | null; executable: number | null; fill: number | null } | null
  /** an open mirror's stop now: the stock's stop, the premium cap on the bid, the trail once lifted, and the
   *  confirmation spent while the stock is through (areaNeeded null: stop E, which waits maxSeconds) */
  live?: {
    equitySl: number
    premiumCap: number
    trailStop: number | null
    through: { seconds: number; area: number; areaNeeded: number | null; maxSeconds: number } | null
    maxDdR: number
    maxDdInr: number | null
  } | null
}

/** A stop-rule mirror still open on its own rule after the real trade closed. */
export interface MirrorAlone extends PositionView {
  rule: StopRule
  source: string
  sourceLabel: string
  stopRule: StopRuleRow | null
  realExit: { exitReason: string | null; exitPrice: number | null; closedTs: number | null; net: number | null; byOperator: boolean } | null
}

/** One book × one stop rule: trades closed under all three rules (net, wins, stops, against the current
 *  stop), and those still open or waiting on another rule. */
export interface StopRuleCell {
  closed: number
  net: number
  wins: number
  stops: number
  better: number
  worse: number
  same: number
  diff: number
  open: number
  waiting: number
  openGross: number | null
}

export interface StopRuleGrid {
  since: number | null
  scope: 'today' | 'start'
  rules: Record<StopRule, string>
  books: { book: string; label: string; shadow: boolean; cells: Partial<Record<StopRule, StopRuleCell>> }[]
  total: Record<StopRule, StopRuleCell> | null
}

export interface Overview {
  mode: Mode
  armed_until: number | null
  halted: boolean
  halt_reason: string
  wallets: Wallet[]
  capital: number
  day_pnl: number
  /** each with its two stop-rule mirrors under it (null: a book without mirrors) */
  positions: (PositionView & { stopRules?: StopRuleRow[] | null })[]
  mirrors_alone?: MirrorAlone[]
  mirrors_open?: number
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
  /** the book's name and its side of the trigger, from the registry (an older engine serves neither) */
  strategy_label?: string
  trend?: 'trend' | 'counter-trend' | ''
  /** the stop rule it ran under and the book whose trade it is (a mirror's source; any other book itself) */
  stop_rule?: StopRule
  stop_rule_label?: string
  source_book?: string
  source_label?: string
  /** MFE / MAE as the option's price at that moment and the rupees open then, before charges (null: no R unit) */
  mfe_price?: number | null
  mae_price?: number | null
  mfe_inr?: number | null
  mae_inr?: number | null
  /** "bid": marked on the price the position could be sold for, every fill included; "last": an older trade, the last trade alone */
  mark_basis?: 'bid' | 'last'
  /** a stop's prices, apart: the level that fired, the read that breached it, the bid and the depth-walk price then, the fill */
  stop?: { level: number; triggerPrice: number; triggerOn: string; bidAtTrigger: number | null; askAtTrigger: number | null; executable: number | null; fill: number | null } | null
  /** the trade's fills against the books recorded with them, read at request time (record-only; an older engine serves none) */
  depthCheck?: DepthCheck | null
  /** was the order book stale on the way out (2026-10-09): the exits refused for a stale book and the age of the book the
   *  closing exit filled on, read from the stored orders at request time (an older engine serves none) */
  staleBook?: StaleBook | null
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
  blind?: boolean
  holdout_frac?: number
  n_tested?: number
  autopilot?: boolean
  autopilot_ist?: string
  last_autopilot_ts?: number | null
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
  p_adjusted?: number | null
  n_tested?: number
  in_sample?: { delta_avg_r: number | null; p_value: number | null; verdict: string; baseline?: { n: number; avg_r: number }; patched?: { n: number; avg_r: number } }
  out_of_sample?: { delta_avg_r: number | null; p_value: number | null; p_adjusted: number | null; verdict: string }
  cost_stress?: { delta_avg_r: number | null; p_value: number | null; brokerage_mult: number; slippage_mult: number }
  survives_cost_stress?: boolean
  monthly?: { months: number; patched_beats_baseline: number }
  split?: { train_start: string; train_end: string; test_start: string; test_end: string }
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
  kind: 'case' | 'cohort' | 'manual' | 'autopilot'
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


// -- backtest trade debugger --------------------------------------------------------------------

export interface BtTradeRow {
  strategy: string
  symbol: string
  direction: string
  day: string
  entry_ts: number
  exit_ts: number
  entry: number
  exit: number
  stop: number
  target1: number | null
  qty: number
  gross: number
  charges: number
  net: number
  r_multiple: number
  mfe_r: number
  mae_r: number
  exit_reason: string
  grade: string
  bars_held: number
  opt_net_modelled: number | null
  opt_r_modelled: number | null
  stop_at_exit?: number
  targets_hit?: number
}

export interface BtSummary {
  id: string
  created_ts: number
  symbols: number
  bars: number
  signals: number
  rejections: number
  trades: number
  gross: number
  charges: number
  net: number
  charges_share_of_gross: number | null
  win_rate: number | null
  avg_r: number
  avg_r_stderr: number | null
  avg_r_t: number | null
  n_days: number
  sample_too_small: boolean
  profit_factor: number | null
  max_drawdown: number
  by_strategy: Record<string, { trades: number; net: number; avg_r: number; win_rate: number }>
  by_exit_reason: Record<string, { n: number; net: number }>
  binding_gates: Record<string, number>
  modelled_option_net: number | null
  params: Record<string, unknown>
  fukaa_on_triggers?: { triggers: number; taken: number; watching: number; rejected: number; not_evaluated: number; by_gate: Record<string, number> }
}

export interface BtDetail {
  summary: BtSummary
  trades: BtTradeRow[]
}

export interface FukaaVerdict {
  verdict: 'TAKEN' | 'WATCHING' | 'REJECTED'
  binding_gate?: string
  gates: GateResult[]
  evidence: Record<string, number>
  note?: string
  reason?: string
}

export interface TradeView {
  run_id: string
  index: number
  count: number
  tf: string
  trade: BtTradeRow
  signal: SignalRow | null
  fukaa: FukaaVerdict | null
  bars: { ts: number; o: number; h: number; l: number; c: number; v: number; bb_upper: number | null; bb_middle: number | null; bb_lower: number | null; st_value: number | null; st_trend: number | null; roles: string[] }[]
  indicator_params: Record<string, number>
  zones: Zone[]
  levels: {
    decision_ts: number
    entry_ts: number
    exit_ts: number
    entry: number
    stop: number
    stop_at_exit: number | null
    targets: number[]
    exit: number
    risk: number
    stop_dist_pct: number | null
    stop_dist_atr: number | null
    atr: number | null
    t1_dist_atr: number | null
    stop_zone: string | null
    target_zones: string[] | null
    rr: number | null
    grade: string | null
    fortress: number | null
    room_ratio: number | null
    note: string | null
  }
  path: Record<string, number | string | boolean | null>
}

/** The depth check (operator, 2026-10-09: "go ahead build it n tell me where to view it"): each paper fill against the
 *  book recorded with it. full = the book showed every lot at the price; partial = at least half; thin = under half;
 *  print = filled by a trade through the price while the touch never reached it (counted, never priced: our order
 *  would have stood first at its price); no book = none recorded. Record-only: it never moves an order, a fill, a
 *  stop or a wallet. One module serves both engines, so the words mean the same on each. */
export type DepthVerdict = 'full' | 'partial' | 'thin' | 'print' | 'no book'

export interface DepthOrderCheck {
  orderId: string
  clientOrderId?: string | null
  purpose: string
  /** "entry" | "exit" | "target" | "market" */
  kind: string
  /** an exit crossed at its deadline: it walked the book */
  crossed: boolean
  /** walked the book (a market order or a cross), judged level by level */
  market: boolean
  /** filled on a trade printing through the price while the touch never reached it */
  onPrint: boolean
  /** a print fill: the touch on the side that trades, as it stood at the fill */
  reach: number | null
  /** a print fill: what taking the book instead would have cost, in rupees — to read, never summed */
  crossRs: number | null
  side: 'BUY' | 'SELL'
  qty: number
  lots: number
  lotSize: number
  paperPrice: number | null
  limit: number | null
  /** the book judged on: at the fill, the one a cross walked, or at placement (a fill at placement only) */
  source: 'fill' | 'cross' | 'place' | null
  ageMs: number | null
  stale: boolean
  visibleQty: number | null
  visibleLots: number | null
  covered: number | null
  shortfallLots: number | null
  touch: number | null
  touchQty: number | null
  realisticPrice: number | null
  beyondBook: boolean
  beyondQty: number
  /** rupees the paper fill was kinder than the realistic price (positive = the paper flattered it); null on a print */
  optimismRs: number | null
  verdict: DepthVerdict
  words: string
  filledTs: number | null
  reason: string
}

export interface DepthSide {
  fills: number
  checked: number
  prints: number
  verdict: DepthVerdict
  optimismRs: number | null
  orders: DepthOrderCheck[]
}

export interface DepthCheck {
  verdict: DepthVerdict
  optimismRs: number | null
  fills: number
  /** fills judged on a book (full / partial / thin): the ones in the rupees */
  checked: number
  prints: number
  stale: number
  lotSize: number
  /** a mirror's entry: the book whose fill it copied, and its name */
  entryFrom: string | null
  entryFromLabel?: string
  entry: DepthSide
  exit: DepthSide
}

export interface DepthWorst {
  tradeId: string
  book: string
  label: string
  underlying: string
  symbol: string
  closedTs: number | null
  net: number | null
  optimismRs: number
  verdict: DepthVerdict
  entryRs: number | null
  exitRs: number | null
  prints: number
}

export interface DepthTally {
  trades: number
  /** fills, each counted once however many books' trades carry it */
  fills: number
  checked: number
  byVerdict: Record<DepthVerdict, number>
  optimismRs: number
  stale: number
  /** trades whose entry was another book's fill (a stop-rule mirror, the wide-stop shadow) */
  copied: number
}

export interface DepthSummary {
  since: number | null
  scope: 'today' | 'start'
  bookSet: string
  thinBelow: number
  staleMs: number
  books: (DepthTally & { book: string; label: string; shadow: boolean; worst: DepthWorst[] })[]
  total: DepthTally
  worst: DepthWorst[]
}

/** A closed trade's order book on the way out: exits refused because the book was older than the paper matcher's limit, and
 *  the age of the book the closing exit filled on (``/api/trades`` with ``depth=1`` or ``stale=1``). */
export interface StaleBook {
  stale: boolean
  refused: number
  maxAgeMs: number | null
  limitMs: number | null
  lastRefusedTs: number | null
  fillAgeMs: number | null
  fillLimitMs: number | null
  fillStale: boolean
  /** "order book stale (2 exits refused, 45.6 s)" — empty when it was not */
  words: string
}

/** One side of a recorded book walked for a quantity (api/live_trades.py ``walk_all``). */
export interface LiveWalk {
  avg: number
  qty: number
  inBook: number
  beyondQty: number
  levels: number
  touch: number
  touchQty: number
  /** what the paper matcher would take in one cross (within its ceiling under the touch) */
  matcherQty: number
  matcherAvg: number | null
}

type NifAlign = { with: number; against: number; flat: number; names: number }

/** One open position on /api/positions/live (api/live_trades.py): a card on the Options page's active trades. Read-only. */
export interface LiveTradeRow {
  id: string
  engine: string
  book: string
  bookLabel: string
  trend: 'trend' | 'counter-trend' | ''
  stop_rule: StopRule
  stop_rule_label: string
  source_book: string
  source_label: string
  variant: StopRule
  mirror: boolean
  shadow: boolean
  /** the book whose trade it is (a mirror's source) is a shadow */
  sourceShadow: boolean
  /** the book whose entry fill this one copied (a stop-rule mirror's source, the wide-stop shadow's RT-Y) */
  entryFrom: string | null
  underlying: string
  segment: string
  contract: string
  scripCode: string
  kind: string
  optionType: 'CE' | 'PE' | null
  side: 'LONG' | 'SHORT'
  tradeDirection: 'BULLISH' | 'BEARISH'
  triggerDirection: 'BULLISH' | 'BEARISH'
  qty: number
  qtyRemaining: number
  lotSize: number
  lots: number
  lotsAtEntry: number
  multiplier: number
  entry: number
  openedTs: number
  openedHm: string
  signalId: string
  triggerId: string | null
  triggerTs: number | null
  triggerClose: number | null
  grade: string | null
  quote: { ltp: number | null; bid: number | null; ask: number | null; mid: number | null; ageS: number | null; limitS: number; spreadPct: number | null }
  orderBook: {
    state: 'fresh' | 'stale' | 'none'
    ageS: number | null
    limitS: number
    stale: boolean
    ts: number | null
    depth: { bids: [number, number][]; asks: [number, number][]; ageMs: number } | null
  }
  pnl: {
    unrealised: number | null
    basis: 'bid' | 'ask' | 'last' | null
    unrealisedLtp: number | null
    realisedGross: number
    tradeGross: number | null
    band: number
    state: 'profit' | 'loss' | 'neutral' | null
    netIfExitNow: number | null
    rNow: number | null
    maxDdInr: number | null
  }
  money: {
    chargesSoFar: { entry: number; exits: number; total: number }
    exitCharges: { price: number | null; qty: number; total: number | null; brokerage?: number; stt?: number; exchange?: number; sebi?: number; stamp?: number; gst?: number }
    slippage: {
      entry: {
        fill: number | null; ref: number | null; midAtPlace: number | null; bidAtPlace: number | null; askAtPlace: number | null; limit: number | null
        vsRefRs: number | null; vsMidRs: number | null; vsRefPct: number | null; waitS: number | null; why?: string; outcome?: string; copiedFrom: string | null
      } | null
      exits: { reason: string | null; qty: number; fill: number | null; midAtPlace: number | null; vsMidRs: number | null }[]
      totalVsMidRs: number
    }
    exitNow: {
      basis: 'depth' | 'touch' | 'none'
      avg: number | null
      qty: number
      vsLtpRs: number | null
      vsMidRs: number | null
      vsTouchRs: number | null
      walk: LiveWalk | null
      bookState?: 'fresh' | 'stale' | 'none'
    } | null
  }
  staleExits: { count: number; maxAgeMs: number; limitMs: number | null; lastTs: number | null } | null
  targets: {
    option: { rung: number; price: number; hit: boolean; share: string | null; qty: number | null; lots: number | null; slippage: { avg: number; rs: number; touchQty: number; beyondQty: number; levels: number } | null }[]
    equity: { price: number; passed: boolean }[]
    future: { label: string; price: number; passed: boolean }[]
    futureClose: number | null
    futureLtp: number | null
    optionStop: number
    initialOptionStop: number
    stopNow: number
    line: number | null
    equityStop: number
    peakMid: number | null
    armedBy: string | null
    mirror: { equitySl: number; premiumCap: number; trailStop: number | null; through: { seconds: number; area: number; areaNeeded: number | null; maxSeconds: number } | null } | null
    exitPlan: { policy: string; rows: { kind: string; at: string; action: string; qty: number | null }[] } | null
  }
  market: { aligned: 'yes' | 'no' | 'unknown'; atTrigger: NifAlign | null; now: NifAlign | null; shareAtTrigger?: number; shareNow?: number; why: string }
  winning: { direction: 'BULLISH' | 'BEARISH' | 'FLAT' | null; vsTrade: 'with' | 'against' | 'flat' | null; stockMovePct: number | null; reasons: string[]; why: string }
  /** the trade map's input, in the alert card's field names (components/TradeMap.tsx) */
  map: TradeMapCard
}

export interface LivePositions {
  engine: string
  nowTs: number
  count: number
  rows: LiveTradeRow[]
  skipped: { positionId: string; book: string; error: string }[]
  notes: string[]
  bookLimit: { nowMs: number; maxMs: number; openMaxMs: number; openWindow: string; ceilingPct: number }
  quoteLimitS: number
  neutralRule: string
  recordOnly: boolean
  tookMs: number
}
