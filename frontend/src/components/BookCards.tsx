import { useMemo, useState } from 'react'
import { contractName, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'

// One card per FUDKII trigger, read for one book. Every trigger is scored the same way whether the
// book traded it or not, so a skip reads as "skipped, because" — never as "missed". Two cards per
// row; a card opens in place, under its row. Take / Skip are operator overrides (audited).

type Read = { leg: string; members: string[]; level: number; strength: number; distAtr: number; crossedAtr: number; rejected: boolean; volume: string; surgeT: number | null; surgeT1: number | null; score: number }
type Route = { route: string; reason: string; summary?: string; wall?: { strength: number; members: string[]; timeframes: string; distAtr: number | null; grade: string; leg?: string }; reads?: Read[]; ts?: number }
type Order = { ts: number; avg_price: number | null; filled: number | null; qty: number; reason: string; purpose: string }
type Level = { label: string; price: number }
type ExitRow = { kind: 'target' | 'stop' | 'trail' | 'time'; at: string; action: string; qty: number | null }
type Plan = { ok: boolean; reason?: string; contract?: string; strike?: number; type?: string; premium?: number; bid?: number | null; ask?: number | null; spreadPct?: number | null; delta?: number; lots?: number; qty?: number; outlay?: number; lotSize?: number; optionSl?: number; ladder?: number[]; edm?: number; ladderNote?: string; exitPlan?: { policy: string; rows: ExitRow[] } | null }
type BookRow = { book: string; label: string; status: 'OPEN' | 'EXITED' | 'NONE'; side: 'CE' | 'PE' | 'LONG' | 'SHORT' | null; openedTs: number | null; closedTs: number | null; exitReason: string | null; pnl: number | null }
type Verdict = { action: string; state?: string; gate?: string | null; why: string[] | string; side?: string; stop?: number | null; targets?: number[]; rr?: number | null; grade?: string | null; breadth?: number | null }
type TargetEv = { rung: number; limit: number; qty: number; placedTs: number; ts: number; outcome: string }
type Card = {
  verdicts?: { rtY: Verdict | null; ctY: Verdict | null; ctM?: Verdict | null }
  pending?: (ExecA & { restingS?: number; contract?: string; forTwins?: boolean; parentWhy?: string }) | null
  execLog?: { entry?: ExecA; exits?: (ExecA & { reason?: string; qty?: number })[]; targets?: TargetEv[] } | null
  // the next target SELL resting in advance (fills on a touch; any other exit cancels it first)
  restingTarget?: { rung: number; limit: number; qty: number; lots: number; placedTs: number } | null
  // every rung's SELL resting, lowest first (since 3 Oct the whole ladder rests from the fill)
  restingTargets?: { rung: number; limit: number; qty: number; lots: number; placedTs: number }[]
  side?: 'CE' | 'PE' | 'LONG' | 'SHORT'
  breadth?: { share: number | null; names: number; efficiency?: number; volBand?: string | null; gapDatr?: number; pivotsAhead?: string[]; openBar?: boolean } | null
  books?: BookRow[]
  cta?: { action: 'take' | 'skip' | 'taken'; enabled: boolean; contract: string | null; type?: string | null; bid?: number | null; ask?: number | null; spreadPct?: number | null; reason: string | null } & Size
  // a trigger the route sends COUNTER-TREND, on an in-trend book's card: the counter-trend book's buy
  // is the live button (operator, 2026-09-29) — RT-Y's tab → CT-Y, the others → CT-X
  ctaCounter?: ({ book: string; label: string; action: 'take' | 'taken' | 'held'; enabled: boolean; contract: string | null; type?: string | null; reason: string | null } & Size) | null
  signalId: string; symbol: string; direction: 'BULLISH' | 'BEARISH'; ts: number; grade: string | null; rr: number | null; reason: string
  entry: number; stop: number; targets: number[] | null; stopPct: number
  confluence: { stop_zone?: string; target_zones?: string[]; fortress?: number; room_ratio?: number; note?: string }
  evidence: Record<string, number>; parentDecision: string | null; parentReason: string | null
  candle: { o: number; h: number; l: number; c: number; v: number } | null; surgeT: number | null; surgeT1: number | null; baseline: number | null
  spark: [number, number][]; route: Route | null; skip: { reason: string; ts: number } | null; operator: { kind: string; ts: number }[]
  fade: { signal_id: string; direction: string; stop: number; targets: number[]; grade: string; rr: number; decision?: string; decision_reason?: string } | null
  state: string; routeLabel: string | null
  atr: number | null; oi: number | null; oiChangePct: number | null
  clusters: { price: number; strength: number; members: string[]; wall: boolean; side: 'ahead' | 'behind' }[]
  futLevels: { close: number; atr: number | null; surgeT: number | null; surgeT1: number | null; volume: string; behind: Level | null; ahead: Level[] } | null
  plan: Plan | null; exitPlan: { policy: string; rows: ExitRow[] } | null
  position: { id: string; entry: number; qty: number; qty_remaining: number; opened_ts: number; closed_ts: number | null; option_sl: number; option_targets: number[]; targets_hit: number; instrument: { name: string; lot_size: number; strike: number; option_type: string; multiplier?: number }; note: string; exit_reason?: string; exit_price?: number } | null
  trade: { net: number; gross: number; charges?: number; exit: number; exit_reason: string; mfe_r: number; mae_r: number; duration_s: number } | null
  exits: Order[]
  live: { mid: number; quoteOk: boolean | null; peak: number; line: number; optionSl: number; armedBy: string; armedTs: number | null; targetsHit: number; qtyRemaining: number; qty: number; ladder: number[]; edm: number; underlying: number | null; unrealised: number | null; realised: number } | null
  rtCard: { confidence?: { score: number }; odds?: { pT1: number | null; note?: string }; wallAhead?: { price: number; strength: number; members: string[] } | null; wallBehind?: { price: number; strength: number; members: string[] } | null; stop?: { equityStop: number; optionStop: number; delta: number } } | null
  pros: string[]; cons: string[]
}
/** What a button buys: its lots, quantity, the price of one and the money it needs. */
type Size = { lots?: number | null; qty?: number | null; premium?: number | null; outlay?: number | null }
const sizeText = (s?: Size | null) =>
  s?.lots && s?.qty && s?.premium ? ` · ${s.lots} lots (${s.qty.toLocaleString('en-IN')}) × ₹${s.premium.toFixed(2)} = ${inr0(s.outlay ?? s.premium * s.qty)}` : ''
type Resp = { book: string; day: string; wallet: { balance: number; day_start_balance: number; initial: number; trades: number } | null; counts: Record<string, number>; cards: Card[]; nowTs: number }

export const BOOK_TABS: [string, string][] = [
  ['FUDKII', 'FUDKII'], ['FUDKII_RT_X', 'FUDKII-RT-X'], ['FUDKII_RT_N', 'FUDKII-RT-N'], ['FUDKII_RT_Y', 'FUDKII-RT-Y'],
  ['FUDKII_CT_X', 'FUDKII-CT-X'], ['FUDKII_CT_Y', 'FUDKII-CT-Y'], ['FUDKII_RT_MCX', 'FUDKII-RT-MCX'],
  // the graded-F shadow (2026-09-29): its own wallet, cards of the triggers FUDKII did not publish — paper, no TAKE
  ['FUDKII_RT_Y_F', 'FUDKII-RT-Y-F'],
  // the market-against fade shadow (2026-10-03): CT-Y's fade where ≤ 45 % of the market agrees with the trigger — paper
  ['FUDKII_CT_M', 'FUDKII-CT-M'],
]
const BOOK_LABEL = Object.fromEntries(BOOK_TABS)

type Tone = 'emerald' | 'amber' | 'rose' | 'orange' | 'slate' | 'sky'
const TONE: Record<Tone, { chip: string; bar: string; text: string; soft: string }> = {
  emerald: { chip: 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200', bar: 'bg-emerald-400', text: 'text-emerald-300', soft: 'bg-emerald-400/5' },
  amber: { chip: 'border-amber-400/40 bg-amber-400/10 text-amber-200', bar: 'bg-amber-400', text: 'text-amber-300', soft: 'bg-amber-400/5' },
  rose: { chip: 'border-rose-400/40 bg-rose-400/10 text-rose-200', bar: 'bg-rose-400', text: 'text-rose-300', soft: 'bg-rose-400/5' },
  orange: { chip: 'border-orange-400/40 bg-orange-400/10 text-orange-200', bar: 'bg-orange-400', text: 'text-orange-200', soft: 'bg-orange-400/5' },
  slate: { chip: 'border-slate-600/60 bg-slate-800/70 text-slate-300', bar: 'bg-slate-600', text: 'text-slate-400', soft: 'bg-slate-800/40' },
  sky: { chip: 'border-sky-400/40 bg-sky-400/10 text-sky-200', bar: 'bg-sky-400', text: 'text-sky-200', soft: 'bg-sky-400/5' },
}
// one order's trail (exec/resting.py): signal → limit placed (book) → reprices → filled / crossed / missed
type ExecA = {
  kind?: string; signalTs?: number | null; placedTs?: number | null; limit?: number | null; ref?: number | null; why?: string
  bookAtPlace?: { bid?: number | null; ask?: number | null }; reprices?: [number, number][]
  filledTs?: number | null; fillPrice?: number | null; cancelledTs?: number | null; crossedTs?: number | null; waitS?: number | null; outcome?: string
  // the momentum rule's reads (30 s racing check, 60 s trade-or-skip) and why an order was cancelled
  momentum?: { atS: number; runPct?: number | null; runAtr?: number; note: string }[]; cancelReason?: string
}
const STATE: Record<string, { label: string; tone: Tone }> = {
  OPEN: { label: 'OPEN · live', tone: 'amber' }, TRADED: { label: 'TRADED · closed', tone: 'emerald' },
  SKIPPED: { label: 'SKIPPED', tone: 'rose' }, NO_FILL: { label: 'NO ENTRY', tone: 'slate' },
  NOT_MIRRORED: { label: 'NO ENTRY', tone: 'slate' }, IN_TREND: { label: 'IN TREND · no fade', tone: 'slate' },
  STOP_BREACHED: { label: 'STOP ALREADY BREACHED', tone: 'rose' }, WALLET: { label: 'NO MONEY LEFT', tone: 'slate' },
  ALREADY_RESTING: { label: 'ENTRY ALREADY WORKING', tone: 'slate' },
  COUNTER_NO_PLAN: { label: 'COUNTER · no plan', tone: 'orange' }, NO_ROUTE: { label: 'NO ROUTE', tone: 'slate' },
  PAPER_FILLED: { label: 'FILLED · paper', tone: 'emerald' }, LIVE_FILLED: { label: 'FILLED · live', tone: 'emerald' },
  SHADOW_OK: { label: 'SHADOW', tone: 'slate' }, NO_INSTRUMENT: { label: 'NO INSTRUMENT', tone: 'slate' },
  REJECTED_BOOK: { label: 'REJECTED · book', tone: 'rose' }, NOT_SIZED: { label: 'NOT SIZED', tone: 'slate' },
  EXPOSURE: { label: 'EXPOSURE', tone: 'slate' }, WALLET_HALTED: { label: 'WALLET HALTED', tone: 'rose' },
  PENDING: { label: 'PENDING · limit resting', tone: 'amber' }, LIMIT_UNFILLED: { label: 'MISSED · limit not filled', tone: 'slate' },
  PARENT_HALTED_TWINS_FED: { label: 'HALTED · twins traded', tone: 'amber' },
  PARENT_HALTED_TWINS_MISSED: { label: 'HALTED · twins missed', tone: 'slate' },
  PARENT_SKIPPED_TWINS_FED: { label: 'PARENT SKIPPED · twins traded', tone: 'amber' },
  PARENT_SKIPPED_TWINS_MISSED: { label: 'PARENT SKIPPED · twins missed', tone: 'slate' },
  // 2026-09-29: decided after the close, entered (or not) at the next open; the NSE last-entry minute
  CARRIED: { label: 'CARRIED · next 09:15 open', tone: 'sky' },
  PAST_ENTRY_CUTOFF: { label: 'PAST ENTRY CUTOFF', tone: 'slate' },
}
const stateOf = (s: string) => STATE[s] ?? { label: s.replace(/_/g, ' '), tone: 'slate' as Tone }
const routeTone = (l: string | null): Tone => (l === 'COUNTER-TREND' ? 'orange' : l === 'SKIP' ? 'rose' : l === 'IN TREND' ? 'sky' : 'slate')

const ist = (ts: number, secs = true) => {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).formatToParts(new Date(ts * 1000))
  const g = (k: string) => parts.find((x) => x.type === k)?.value ?? '00'
  return secs ? `${g('hour')}:${g('minute')}:${g('second')}` : `${g('hour')}:${g('minute')}`
}
const f = (v: number | null | undefined, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(d))
// "signal 09:45:00 · placed 09:45:01 @ 17.10 (book 16.95/17.25) · filled 09:45:23 @ 17.10 · 22 s"
const trail = (a: ExecA | null | undefined) => {
  if (!a) return '—'
  const bk = a.bookAtPlace ? ` (book ${f(a.bookAtPlace.bid)}/${f(a.bookAtPlace.ask)})` : ''
  const parts = [a.signalTs ? `signal ${ist(a.signalTs)}` : null,
    a.placedTs ? `placed ${ist(a.placedTs)} @ ${a.limit == null ? 'market' : f(a.limit)}${bk}` : null,
    a.reprices && a.reprices.length ? `repriced ${a.reprices.length}× → ${f(a.reprices[a.reprices.length - 1][1])}` : null,
    a.momentum && a.momentum.length ? a.momentum.map((m) => `${f(m.atS, 0)} s: ${m.note}`).join(' · ') : null,
    a.filledTs ? `filled ${ist(a.filledTs)} @ ${f(a.fillPrice)}` : a.cancelledTs ? `missed ${ist(a.cancelledTs)}${a.cancelReason ? ` (${a.cancelReason})` : ''}` : null,
    a.waitS != null ? `${f(a.waitS, 0)} s` : null, a.outcome && a.outcome !== 'filled at the limit' && a.outcome !== 'filled' ? a.outcome : null]
  return parts.filter(Boolean).join(' · ')
}
const human = (s: string | null | undefined) => (s ?? '')
  .replace(/no tradeable strike:/g, 'no strike to buy —')
  .replace(/(\d+(?:\.\d+)?):4-lots-(₹[\d,]*\d)≥(₹[\d,]*\d)/g, '$1: 4 lots cost $2 (cap $3)')
  .replace(/(\d+(?:\.\d+)?):one-sided/g, '$1: one-sided quote')
  .replace(/(\d+(?:\.\d+)?):spread-([\d.]+%)/g, '$1: spread $2')
  .replace(/(\d+(?:\.\d+)?):no-quote/g, '$1: no quote')
  .replace(/(\d+(?:\.\d+)?):unpriced/g, '$1: not priced yet')
  .replace(/(\d+(?:\.\d+)?):delta-([\d.]+)<([\d.]+)/g, '$1: delta $2 under $3')
  .replace(/\(\+(\d+) more\)/g, '(+$1 more strikes)')
  .replace(/(\d+(?:\.\d+)?):stale-(\d+)s/g, '$1: quote $2 s stale').replace(/(\d+(?:\.\d+)?) stale (\d+)s/g, '$1: quote $2 s stale')
  .replace(/(\d+(?:\.\d+)?):premium-/g, '$1: premium ')
const inr = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v < 0 ? '−' : v > 0 ? '+' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`)
const inr0 = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`)
const pct = (a: number | null | undefined, b: number) => (a === null || a === undefined || !b ? '—' : `${(((a - b) / b) * 100).toFixed(2)}%`)
const lakh = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v >= 1e7 ? `${(v / 1e7).toFixed(2)} Cr` : v >= 1e5 ? `${(v / 1e5).toFixed(1)} L` : v.toLocaleString('en-IN'))

// -- icons (inline stroke SVG, 16px) ---------------------------------------------------------------
const I = ({ d, className = '' }: { d: string; className?: string }) => (
  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={`inline-block shrink-0 ${className}`} aria-hidden="true"><path d={d} /></svg>
)
const IC = {
  up: 'M7 17 17 7M8 7h9v9', down: 'M7 7l10 10M17 8v9H8', bolt: 'M13 2 3 14h9l-1 8 10-12h-9l1-8z', layers: 'M12 2 2 7l10 5 10-5-10-5zM2 12l10 5 10-5M2 17l10 5 10-5',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', target: 'M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0M12 12m-8 0a8 8 0 1 0 16 0a8 8 0 1 0-16 0M12 2v3M12 19v3M2 12h3M19 12h3',
  clock: 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 7v5l3 3', wallet: 'M3 7h18v12H3zM3 7l2-3h14l2 3M16 13h2', route: 'M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9',
  activity: 'M22 12h-4l-3 9L9 3l-3 9H2', pie: 'M21.21 15.89A10 10 0 1 1 8 2.83M22 12A10 10 0 0 0 12 2v10z', flag: 'M4 22V4a1 1 0 0 1 1-1h11l-1.5 4L16 11H5', trend: 'M3 17l6-6 4 4 8-8M21 7h-6M21 7v6',
  ticket: 'M3 9V7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-4zM13 5v14', chart: 'M3 3v18h18M7 14l4-4 4 4 5-6', check: 'M20 6 9 17l-5-5', x: 'M18 6 6 18M6 6l12 12', open: 'M6 9l6 6 6-6',
}

function Pill({ tone, icon, children, title, big = false }: { tone: Tone; icon?: string; children: React.ReactNode; title?: string; big?: boolean }) {
  return <span title={title} className={`inline-flex items-center gap-1.5 rounded-full border font-semibold ${big ? 'px-3.5 py-1 text-[14px]' : 'px-2.5 py-[3px] text-[13px]'} ${TONE[tone].chip}`}>{icon && <I d={icon} className="opacity-90" />}{children}</span>
}
function Tile({ icon, k, v, tone, title }: { icon: string; k: string; v: React.ReactNode; tone?: Tone; title?: string }) {
  return (
    <div title={title} className="flex min-w-[120px] items-center gap-2.5 rounded-xl border border-slate-800 bg-slate-950/50 px-3 py-2">
      <I d={icon} className={`${tone ? TONE[tone].text : 'text-slate-500'}`} />
      <div className="flex flex-col leading-tight">
        <span className="text-[11.5px] font-semibold uppercase tracking-wider text-slate-500">{k}</span>
        <span className={`text-[15px] font-semibold tabular-nums ${tone ? TONE[tone].text : 'text-slate-100'}`}>{v}</span>
      </div>
    </div>
  )
}

/** The underlying since the trigger bar opened, with the stop and T1 as lines. */
function Spark({ c }: { c: Card }) {
  const w = 240, h = 64
  const pts = c.spark
  if (!pts || pts.length < 2) return <div className="flex h-16 items-center text-[12px] text-slate-600">underlying path appears once 1m bars accrue</div>
  const t1 = c.targets?.[0] ?? null
  const ys = pts.map((p) => p[1]).concat([c.stop, ...(t1 ? [t1] : [])])
  const ymin = Math.min(...ys), ymax = Math.max(...ys), yr = ymax - ymin || 1
  const xmin = pts[0][0], xmax = pts[pts.length - 1][0], xr = xmax - xmin || 1
  const X = (x: number) => 4 + ((x - xmin) / xr) * (w - 8), Y = (y: number) => 6 + (1 - (y - ymin) / yr) * (h - 12)
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ')
  const bull = c.direction === 'BULLISH'
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="block">
      <line x1="4" x2={w - 4} y1={Y(c.stop)} y2={Y(c.stop)} stroke="#fb7185" strokeDasharray="3 3" />
      {t1 !== null && <line x1="4" x2={w - 4} y1={Y(t1)} y2={Y(t1)} stroke="#34d399" strokeDasharray="3 3" />}
      <path d={d} fill="none" stroke={bull ? '#38bdf8' : '#f472b6'} strokeWidth="1.6" />
      <circle cx={X(c.ts)} cy={Y(c.entry)} r="3.5" fill="#fbbf24" stroke="#0f172a" strokeWidth="1.2" />
    </svg>
  )
}

/** Equity · Future · Option in one table: entry amber, stop red, targets green — by the trade's direction. */
function Levels({ c }: { c: Card }) {
  const bull = c.direction === 'BULLISH'
  const fut = c.futLevels
  const p = c.position, plan = c.plan
  const optEntry = p ? p.entry : plan?.ok ? plan.premium : null
  const optSl = p ? (c.live ? Math.max(c.live.line, c.live.optionSl) : p.option_sl) : plan?.ok ? plan.optionSl : null
  const optLadder = p ? (c.live?.ladder?.length ? c.live.ladder : p.option_targets) : plan?.ok ? (plan.ladder ?? []) : []
  const optHead = p ? `${contractName(p.instrument.name)} · ${p.qty / p.instrument.lot_size} lots (${p.qty})` : plan?.ok ? `${contractName(plan.contract)} · ${plan.lots} lots (${plan.qty})` : plan ? `no contract — ${plan.reason}` : 'no contract'
  const n = Math.max(c.targets?.length ?? 0, fut?.ahead.length ?? 0, optLadder.length, 1)
  const rows: { key: string; tone: Tone; icon: string; eq: React.ReactNode; fu: React.ReactNode; op: React.ReactNode }[] = []
  const cell = (price: number | null | undefined, sub?: string | null, ref?: number) => (
    <div className="flex flex-col leading-tight">
      <span className="text-[15px] font-semibold tabular-nums">{f(price)}{ref && price ? <span className="ml-1.5 text-[12.5px] font-normal text-slate-400">{pct(price, ref)}</span> : null}</span>
      {sub ? <span className="text-[12px] text-slate-500">{sub}</span> : null}
    </div>
  )
  rows.push({ key: 'entry', tone: 'amber', icon: IC.trend, eq: cell(c.entry, 'trigger close'), fu: fut ? cell(fut.close, 'FUT trigger close') : <span className="text-slate-600">—</span>, op: optEntry ? cell(optEntry, p ? `filled ${ist(p.opened_ts)}` : `live ask · δ ${f(plan?.delta)}`) : <span className="text-slate-600">—</span> })
  rows.push({ key: 'stop', tone: 'rose', icon: IC.shield, eq: cell(c.stop, c.confluence.stop_zone || '1 ATR fallback', c.entry), fu: fut?.behind ? cell(fut.behind.price, fut.behind.label, fut.close) : <span className="text-slate-600">—</span>, op: optSl ? cell(optSl, c.live && c.live.line > c.live.optionSl ? 'rising line' : 'equity stop via δ', optEntry ?? undefined) : <span className="text-slate-600">—</span> })
  for (let i = 0; i < n; i++) {
    const eqT = c.targets?.[i], fuT = fut?.ahead[i], opT = optLadder[i]
    if (eqT === undefined && fuT === undefined && opT === undefined) continue
    const hit = c.position ? i < (c.live?.targetsHit ?? c.position.targets_hit) : false
    rows.push({ key: `t${i + 1}`, tone: 'emerald', icon: IC.target, eq: eqT !== undefined ? cell(eqT, c.confluence.target_zones?.[i] ?? null, c.entry) : <span className="text-slate-600">—</span>, fu: fuT ? cell(fuT.price, fuT.label, fut!.close) : <span className="text-slate-600">—</span>, op: opT !== undefined ? <div className="flex items-center gap-2">{cell(opT, hit ? 'taken' : i === optLadder.length - 1 ? 'the rest' : '1 lot', optEntry ?? undefined)}{hit && <I d={IC.check} className="text-emerald-400" />}</div> : <span className="text-slate-600">—</span> })
  }
  return (
    <div className="overflow-hidden rounded-xl border border-slate-800">
      <table className="w-full border-collapse">
        <thead>
          <tr className="bg-slate-950/70 text-[12px] uppercase tracking-wider text-slate-400">
            <th className="w-[92px] px-3 py-2 text-left font-semibold">{bull ? 'long' : 'short'} bias</th>
            <th className="px-3 py-2 text-left font-semibold"><span className="inline-flex items-center gap-1.5"><I d={IC.chart} className="text-sky-300" />Equity</span></th>
            <th className="px-3 py-2 text-left font-semibold"><span className="inline-flex items-center gap-1.5"><I d={IC.activity} className="text-sky-300" />Future</span>{fut ? <span className="ml-2 normal-case tracking-normal text-slate-500">vol {f(fut.surgeT)}× / {f(fut.surgeT1)}×</span> : null}</th>
            <th className="px-3 py-2 text-left font-semibold"><span className="inline-flex items-center gap-1.5"><I d={IC.ticket} className="text-amber-300" />Option</span><span className="ml-2 normal-case tracking-normal text-slate-500">{optHead}</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={`border-t border-slate-800/80 ${TONE[r.tone].soft}`}>
              <td className={`px-3 py-2.5 text-[13px] font-semibold uppercase tracking-wide ${TONE[r.tone].text}`}><span className="inline-flex items-center gap-1.5"><I d={r.icon} />{r.key}</span></td>
              <td className={`px-3 py-2 ${TONE[r.tone].text}`}>{r.eq}</td>
              <td className={`px-3 py-2 ${TONE[r.tone].text}`}>{r.fu}</td>
              <td className={`px-3 py-2 ${TONE[r.tone].text}`}>{r.op}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ExitPlan({ plan }: { plan: { policy: string; rows: ExitRow[] } }) {
  const tone: Record<ExitRow['kind'], Tone> = { target: 'emerald', stop: 'rose', trail: 'sky', time: 'slate' }
  const icon: Record<ExitRow['kind'], string> = { target: IC.target, stop: IC.shield, trail: IC.trend, time: IC.clock }
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
      <div className="mb-2.5 flex flex-col gap-1"><span className="flex items-center gap-2 text-[12.5px] font-semibold uppercase tracking-wider text-slate-400"><I d={IC.route} className="text-slate-500" />exit plan</span><span className="text-[13px] text-slate-400">{plan.policy}</span></div>
      <div className="flex flex-col gap-1.5">
        {plan.rows.map((r, i) => (
          <div key={i} className="flex items-center gap-3 text-[14px]">
            <I d={icon[r.kind]} className={TONE[tone[r.kind]].text} />
            <span className="flex-1 text-slate-200">{r.at}</span>
            <span className={`font-semibold tabular-nums ${TONE[tone[r.kind]].text}`}>{r.action}{r.qty !== null ? ` · ${r.qty}` : ''}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function Timeline({ c }: { c: Card }) {
  const rows: { ts: number; what: string; detail: string; tone: Tone }[] = [{ ts: c.ts + 1800, what: 'fired', detail: `${c.reason} · grade ${c.grade ?? '—'} rr ${f(c.rr)}`, tone: 'sky' }]
  if (c.route) rows.push({ ts: c.route.ts ?? c.ts + 1800, what: `route ${c.route.route}`, detail: c.route.reason, tone: c.route.route === 'COUNTER' ? 'orange' : 'slate' })
  if (c.skip) rows.push({ ts: c.skip.ts, what: 'skipped', detail: c.skip.reason, tone: 'rose' })
  if (c.position) rows.push({ ts: c.position.opened_ts, what: 'filled', detail: `${c.position.qty} × @ ${f(c.position.entry)} · ${contractName(c.position.instrument.name)}`, tone: 'amber' })
  for (const o of c.exits) rows.push({ ts: o.ts, what: 'exit', detail: `${o.filled ?? o.qty} @ ${f(o.avg_price)} — ${o.reason}`, tone: 'emerald' })
  for (const e of c.operator) rows.push({ ts: e.ts, what: e.kind.replace('operator.', 'operator '), detail: 'override, audited', tone: 'amber' })
  if (c.trade) rows.push({ ts: c.position?.closed_ts ?? c.ts, what: 'closed', detail: `${c.trade.exit_reason} · net ${inr(c.trade.net)} · MFE ${f(c.trade.mfe_r)}R MAE ${f(c.trade.mae_r)}R`, tone: c.trade.net >= 0 ? 'emerald' : 'rose' })
  rows.sort((a, b) => a.ts - b.ts)
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r, i) => (
        <div key={i} className="flex items-start gap-3 text-[14px]">
          <span className={`w-[72px] flex-none tabular-nums ${TONE[r.tone].text}`}>{ist(r.ts)}</span>
          <span className={`w-[118px] flex-none font-semibold ${TONE[r.tone].text}`}>{r.what}</span>
          <span className="text-slate-300">{r.detail}</span>
        </div>
      ))}
    </div>
  )
}

function Reads({ r }: { r: Route | null }) {
  if (!r?.reads?.length) return <div className="text-[14px] text-slate-400">{r ? r.reason : 'no route recorded for this trigger (routes are kept from 23-Sep evening; MCX has none)'}</div>
  return (
    <table className="w-full border-collapse text-[13.5px]">
      <thead><tr className="text-[11.5px] uppercase tracking-wider text-slate-500">{['leg', 'nearest pivot', 'dist · crossed', 'volume T / T−1', 'score'].map((h) => <th key={h} className="border-b border-slate-800 px-2 py-1.5 text-left font-semibold">{h}</th>)}</tr></thead>
      <tbody>
        {r.reads.map((x) => (
          <tr key={x.leg} className="tabular-nums">
            <td className="px-2 py-1.5 text-slate-300">{x.leg}</td>
            <td className="px-2 py-1.5 text-slate-200">{x.members.join(',')} {f(x.level)} <span className="text-slate-500">({f(x.strength, 1)})</span></td>
            <td className="px-2 py-1.5 text-slate-200">{f(x.distAtr)} · {x.crossedAtr >= 0 ? '+' : ''}{f(x.crossedAtr)} ATR{x.rejected ? ' · rejected' : ''}</td>
            <td className={`px-2 py-1.5 ${x.volume === 'dried' ? 'text-rose-300' : x.volume === 'surge' ? 'text-emerald-300' : 'text-slate-300'}`}>{x.volume} {x.surgeT !== null ? `${f(x.surgeT)}/${f(x.surgeT1)}` : ''}</td>
            <td className={`px-2 py-1.5 ${x.score >= 0.5 ? 'text-orange-200' : 'text-slate-300'}`}>{f(x.score)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Cta({ book, c, onDone }: { book: string; c: Card; onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const held = c.state === 'OPEN'
  const canTake = !held && !['TRADED', 'PAPER_FILLED', 'LIVE_FILLED'].includes(c.state) && !!c.plan?.ok && (c.cta?.enabled ?? true)
  const cc = c.ctaCounter ?? null
  const post = async (target: string, kind: 'take' | 'skip', verb: string, tag: string) => {
    if (!window.confirm(verb)) return
    setBusy(tag); setMsg(null)
    try {
      const r = await postJson<Record<string, unknown>>(`/api/books/${target}/${kind}`, { signal_id: c.signalId })
      // the book's own decision and why — never a silent "not entered" (audit, 2026-09-26)
      const how = [r.decision, r.reason].filter(Boolean).join(' — ')
      setMsg(kind === 'take' ? (r.entered ? `entered — ${how}` : r.decision === 'RESTING' ? `limit resting — ${r.reason}` : `not entered — ${how || 'see the ledger decision'}`) : 'closing')
      onDone()
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)) } finally { setBusy(null) }
  }
  const act = (kind: 'take' | 'skip') =>
    post(book, kind, kind === 'take'
      ? `Enter ${BOOK_LABEL[book] ?? book} on ${c.symbol} now — ${c.plan?.lots} lots (${c.plan?.qty}) of ${contractName(c.plan?.contract)} × ₹${f(c.plan?.premium)} ≈ ${inr0(c.plan?.outlay)}?`
      : `Close ${BOOK_LABEL[book] ?? book}'s ${c.symbol} position now, at the market?`, kind)
  const takeCounter = () => cc && post(cc.book, 'take',
    `Enter ${cc.label} on ${c.symbol} COUNTER-TREND now — ${cc.lots} lots (${cc.qty}) of ${contractName(cc.contract)} × ₹${f(cc.premium)} ≈ ${inr0(cc.outlay)}?`, 'counter')
  const mid = c.live?.mid ?? null
  const left = c.live?.qtyRemaining ?? c.position?.qty_remaining ?? 0
  const lot = c.position?.instrument.lot_size || 1
  const grey = 'inline-flex cursor-not-allowed items-center gap-2 rounded-xl border border-slate-700 bg-slate-800 px-5 py-2.5 text-[14.5px] font-bold text-slate-500'
  return (
    <div className="flex flex-wrap items-center gap-2.5">
      {cc && (
        <div className="basis-full flex flex-wrap items-center gap-2.5">
          {cc.enabled ? (
            <button disabled={busy !== null} onClick={takeCounter} className="inline-flex items-center gap-2 rounded-xl bg-orange-400 px-5 py-2.5 text-[14.5px] font-bold text-slate-950 shadow-[0_6px_20px_-8px_rgba(251,146,60,0.8)] hover:bg-orange-300 disabled:opacity-40"><I d={IC.check} />TAKE COUNTER-TREND · {cc.label}: {contractName(cc.contract)}{sizeText(cc)}</button>
          ) : (
            <button disabled aria-disabled className={grey}><I d={IC.check} />{cc.action === 'taken' ? 'TAKEN' : cc.action === 'held' ? 'HELD' : 'TAKE'} COUNTER-TREND · {cc.label}: {contractName(cc.contract) ?? `${c.symbol} — no contract`}{sizeText(cc)}</button>
          )}
          {cc.reason && <span className={`text-[13.5px] ${cc.enabled ? 'text-orange-200/80' : 'text-slate-400'}`}>{cc.enabled ? '' : cc.action === 'take' ? 'Why disabled: ' : ''}{cc.reason}</span>}
        </div>
      )}
      {held ? (
        <button disabled={busy !== null} onClick={() => act('skip')} className="inline-flex items-center gap-2 rounded-xl bg-rose-500 px-5 py-2.5 text-[14.5px] font-bold text-slate-950 shadow-[0_6px_20px_-8px_rgba(251,113,133,0.8)] hover:bg-rose-400 disabled:opacity-40"><I d={IC.x} />SKIP · close {Math.floor(left / lot)} lots ({left.toLocaleString('en-IN')}) at market{mid ? ` ≈ ₹${mid.toFixed(2)} = ${inr0(mid * left * (c.position?.instrument.multiplier || 1))}` : ''}</button>
      ) : (
        canTake ? (
          <button disabled={busy !== null} onClick={() => act('take')} className="inline-flex items-center gap-2 rounded-xl bg-emerald-400 px-5 py-2.5 text-[14.5px] font-bold text-slate-950 shadow-[0_6px_20px_-8px_rgba(52,211,153,0.8)] hover:bg-emerald-300 disabled:opacity-40"><I d={IC.check} />TAKE {contractName(c.plan?.contract)}{sizeText(c.cta?.lots ? c.cta : { lots: c.plan?.lots, qty: c.plan?.qty, premium: c.plan?.premium, outlay: c.plan?.outlay })}</button>
        ) : (
          // Not takeable: still name the contract and what it would cost, greyed out, with the reason underneath.
          <button disabled aria-disabled className={grey}><I d={IC.check} />{c.cta?.action === 'taken' || c.state === 'TRADED' ? 'TAKEN' : 'TAKE'} {contractName(c.cta?.contract ?? c.plan?.contract) ?? `${c.symbol} ${c.cta?.type ?? c.side ?? ''} — no listed contract`}{sizeText(c.cta)}</button>
        )
      )}
      {canTake && <span className="text-[13.5px] text-slate-400">ask {f(c.plan?.ask)} · spread {f(c.plan?.spreadPct, 1)}% · δ {f(c.plan?.delta)}</span>}
      {msg && <span className="text-[13.5px] text-amber-300">{msg}</span>}
      {!held && c.cta?.reason && (
        <div className={`basis-full text-[13.5px] ${canTake ? 'text-amber-300/80' : 'text-slate-400'}`}>
          {canTake ? '' : 'Why disabled: '}{human(c.cta.reason)}
          {!canTake && c.cta.bid != null && <span className="text-slate-500"> · bid {f(c.cta.bid)} / ask {f(c.cta.ask)}{c.cta.spreadPct != null ? ` · spread ${f(c.cta.spreadPct, 1)}%` : ''}</span>}
        </div>
      )}
    </div>
  )
}

/** Every book on this trigger at a glance (operator, 2026-09-29): a glowing GREEN circle while the book
 *  holds a live trade on it, CE or PE alike; a GREY one once it has exited; a ring where it never bought.
 *  The side, times and P&L are on hover. */
// Gate B, the 09:45 gap fade and the market-against fade, on EVERY book's card (operator, 2026-09-26:
// "mention on all respective strategies as label"). Rose = RT-Y stands aside, emerald = RT-Y takes,
// amber = CT-Y fades, violet = CT-M fades (the shadow, 2026-10-03).
function VerdictChips({ v }: { v: { rtY: Verdict | null; ctY: Verdict | null; ctM?: Verdict | null } }) {
  const y = v.rtY
  const c = v.ctY
  const m = v.ctM ?? null
  const mWhy = m ? (Array.isArray(m.why) ? m.why.join(' · ') : m.why) : ''
  const mPct = m?.breadth != null ? ` · ${Math.round(m.breadth * 100)}% agree` : ''
  const yWhy = y ? (Array.isArray(y.why) ? y.why : [y.why]).filter(Boolean).join(' · ') : ''
  const yLabel = y ? `RT-Y: ${y.action}${y.state && y.state.startsWith('would') ? ' (would)' : ''}${y.action !== 'TAKE' && yWhy ? ` — ${yWhy}` : ''}` : ''
  const cPlan = c && c.action !== 'NONE'
    ? `CT-Y: ${c.action} ${c.side ?? ''} · stop ${f(c.stop)} · T1 ${f(c.targets?.[0])} · RR ${f(c.rr, 1)}${c.grade ? ` (${c.grade})` : ''}`
    : ''
  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-2 text-[13px] font-medium">
      {y ? (
        <span title={yWhy} className={`rounded-md border px-2.5 py-1 ${y.action === 'SKIP' ? 'border-rose-400/40 bg-rose-400/10 text-rose-200' : y.action === 'TAKE' ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200' : 'border-slate-500/50 bg-slate-800/70 text-slate-300'}`}>{yLabel}</span>
      ) : null}
      {c && c.action !== 'NONE' ? (
        <span title={Array.isArray(c.why) ? c.why.join(' · ') : c.why} className="rounded-md border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-amber-200">{cPlan}</span>
      ) : c ? (
        <span title={Array.isArray(c.why) ? c.why.join(' · ') : c.why} className="rounded-md border border-slate-600/60 bg-slate-800/70 px-2 py-0.5 text-slate-400">CT-Y: no fade — {Array.isArray(c.why) ? c.why.join(' · ') : c.why}</span>
      ) : null}
      {m && m.action === 'FADE' ? (
        <span title={mWhy} className="rounded-md border border-violet-400/40 bg-violet-400/10 px-2 py-0.5 text-violet-200">{`CT-M: FADE ${m.side ?? ''} · stop ${f(m.stop)} · T1 ${f(m.targets?.[0])}${mPct}`}</span>
      ) : m && m.action === 'WOULD FADE' ? (
        <span title={mWhy} className="rounded-md border border-violet-400/30 bg-slate-800/70 px-2 py-0.5 text-violet-300/80">{`CT-M: would fade${mPct}`}</span>
      ) : m ? (
        <span title={mWhy} className="rounded-md border border-slate-600/60 bg-slate-800/70 px-2 py-0.5 text-slate-400">{`CT-M: no fade — ${mWhy}`}</span>
      ) : null}
    </div>
  )
}

function BookDots({ books, current }: { books: BookRow[]; current: string }) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-1.5"><span className="text-[12px] font-semibold uppercase tracking-wide text-slate-500">books on it</span>
      {books.map((b) => {
        const dot = b.status === 'OPEN'
          ? 'animate-pulse bg-emerald-400 shadow-[0_0_10px_3px_rgba(52,211,153,0.85)]'
          : b.status === 'EXITED'
            ? 'bg-slate-400'
            : 'border-[1.5px] border-slate-500 bg-transparent'
        const tip = [
          `${b.label}: ${b.status === 'NONE' ? 'did not buy' : b.status === 'OPEN' ? 'holding' : 'exited'}`,
          b.side ?? '',
          b.openedTs ? `in ${ist(b.openedTs)}` : '',
          b.closedTs ? `out ${ist(b.closedTs)}${b.exitReason ? ` (${b.exitReason})` : ''}` : '',
          b.pnl !== null && b.pnl !== undefined ? inr(b.pnl) : '',
        ].filter(Boolean).join(' · ')
        return (
          <span key={b.book} title={tip} className={`inline-flex items-center gap-2 text-[13.5px] ${b.status === 'OPEN' ? 'font-semibold text-emerald-200' : b.status === 'EXITED' ? 'text-slate-300' : 'text-slate-500'} ${b.book === current ? 'font-bold underline decoration-slate-500 underline-offset-4' : ''}`}>
            <span className={`inline-block h-3 w-3 rounded-full ${dot}`} />{b.label}
          </span>
        )
      })}
    </div>
  )
}

// -- the card, in three layers (operator, 2026-09-29: "font size a bit small … visually a bit heavy to
// comprehend immediately"): GLANCE — who, which way, this book's verdict and why, the money, the button;
// SCAN — the trade map and the meters; STUDY — every table, under Details. Nothing is dropped: what left
// the face of the card is one click away, under it.

/** The trade on ONE rail (operator, 2026-09-30: "one rail, colour-coded"): every level of the equity (●),
 *  the future (◆) and the option (▲) placed where the STOCK would be when that level is reached — a
 *  future level by its own % move, an option level through the option's delta, as the engine projects
 *  it — so the three ladders read on one axis. Risk to the left of the entry, reward to the right,
 *  whichever way the trade runs. A level the stock has passed turns green with a ✔ (a live trade's
 *  option rungs: the rungs actually taken); the next level ahead on each ladder is named underneath. */
type RailMark = { key: string; inst: 'equity' | 'future' | 'option'; code: string; kind: 'stop' | 'target'; price: number; move: number; zone?: string | null; crossed: boolean }

function TradeMap({ c }: { c: Card }) {
  const bull = c.direction === 'BULLISH'
  const sg = bull ? 1 : -1
  const p = c.position, plan = c.plan, fut = c.futLevels
  const optEntry = p ? p.entry : plan?.ok ? plan.premium ?? null : null
  const optSl = p ? (c.live ? Math.max(c.live.line, c.live.optionSl) : p.option_sl) : plan?.ok ? plan.optionSl ?? null : null
  const optLadder = p ? (c.live?.ladder?.length ? c.live.ladder : p.option_targets) : plan?.ok ? plan.ladder ?? [] : []
  // the option's delta: the live preview's, the card's stop projection's, else the one the position was opened on
  const noteDelta = Number(/delta≈\s*([\d.]+)/.exec(p?.note ?? '')?.[1] ?? 0)
  const delta = Math.abs(plan?.ok ? plan.delta ?? 0 : 0) || Math.abs(c.rtCard?.stop?.delta ?? 0) || noteDelta || null
  const rungsTaken = p ? (c.live?.targetsHit ?? p.targets_hit) : 0
  const now = c.live?.underlying ?? (c.spark?.length ? c.spark[c.spark.length - 1][1] : null)
  // favourable stock move, in % of the trigger close: + is toward the targets
  const fav = (px: number) => (sg * (px - c.entry) / c.entry) * 100
  const nowRaw = now !== null ? fav(now) : null
  const nowAtEntry = nowRaw !== null && Math.abs(nowRaw) < 0.05  // on the entry itself: one label, not two on top of each other
  const nowMove = nowAtEntry ? null : nowRaw
  const crossedAt = (move: number) => nowMove !== null && move > 0 && nowMove >= move
  const marks: RailMark[] = []
  marks.push({ key: 'e-sl', inst: 'equity', code: 'SL', kind: 'stop', price: c.stop, move: fav(c.stop), zone: c.confluence.stop_zone, crossed: false })
  ;(c.targets ?? []).slice(0, 4).forEach((t, i) => {
    const m = fav(t)
    marks.push({ key: `e${i}`, inst: 'equity', code: `E${i + 1}`, kind: 'target', price: t, move: m, zone: c.confluence.target_zones?.[i], crossed: crossedAt(m) })
  })
  if (fut && fut.close) {
    const fmove = (px: number) => (sg * (px - fut.close) / fut.close) * 100
    if (fut.behind) marks.push({ key: 'f-sl', inst: 'future', code: 'SL', kind: 'stop', price: fut.behind.price, move: fmove(fut.behind.price), zone: fut.behind.label, crossed: false })
    fut.ahead.slice(0, 4).forEach((l, i) => {
      const m = fmove(l.price)
      marks.push({ key: `f${i}`, inst: 'future', code: `F${i + 1}`, kind: 'target', price: l.price, move: m, zone: l.label, crossed: crossedAt(m) })
    })
  }
  if (optEntry && delta) {
    // the stock move that takes the premium to a level: (level − entry premium) ÷ δ, in % of the close
    const omove = (px: number) => ((px - optEntry) / delta / c.entry) * 100
    if (optSl) marks.push({ key: 'o-sl', inst: 'option', code: 'SL', kind: 'stop', price: optSl, move: omove(optSl), zone: 'option stop', crossed: false })
    optLadder.slice(0, 4).forEach((l, i) => {
      const m = omove(l)
      marks.push({ key: `o${i}`, inst: 'option', code: `O${i + 1}`, kind: 'target', price: l, move: m, zone: i === optLadder.length - 1 ? 'the rest' : '1 lot', crossed: p ? i < rungsTaken : crossedAt(m) })
    })
  }
  // the scale: every stop, the nearer targets and "now"; a far option rung is pinned to the edge (›)
  const tgMoves = marks.filter((m) => m.kind === 'target').map((m) => m.move).sort((a, b) => a - b)
  const lo = Math.min(0, ...marks.filter((m) => m.kind === 'stop').map((m) => m.move), nowMove ?? 0)
  const eqLast = Math.max(0, ...marks.filter((m) => m.inst !== 'option' && m.kind === 'target').map((m) => m.move))
  const hiRaw = Math.max(eqLast, nowMove ?? 0, tgMoves.length ? tgMoves[Math.min(tgMoves.length - 1, 5)] : 0.5)
  const hi = hiRaw <= 0 ? 0.5 : hiRaw
  // two pieces: the risk side (lo … 0) gets at least 12 % of the rail, so the stops and the entry
  // stay apart when the targets run far; the header, the stops line and the hover keep the exact %
  const hiP = hi * 1.04, loP = lo < 0 ? lo * 1.08 : -0.05
  const riskW = Math.max(12, (Math.abs(loP) / (hiP - loP)) * 96)
  const x = (m: number) => Math.max(1.5, Math.min(98.5, m <= 0 ? 2 + riskW * (1 - m / loP) : 2 + riskW + ((96 - riskW) * m) / hiP))
  const off = (m: number) => m > hi * 1.03
  // codes stagger into three rows where two sit closer than 4.5 % of the rail
  const rowOf: Record<string, number> = {}
  const lastInRow = [-100, -100, -100]
  for (const m of [...marks].sort((a, b) => x(a.move) - x(b.move))) {
    const px = x(m.move)
    let r = lastInRow.findIndex((last) => px - last >= 4.5)
    if (r < 0) r = 2
    rowOf[m.key] = r
    lastInRow[r] = px
  }
  const col = { equity: 'text-sky-300', future: 'text-violet-300', option: 'text-amber-300' }
  const bg = { equity: 'bg-sky-300', future: 'bg-violet-300', option: 'bg-amber-300' }
  const shape = (m: RailMark) => {
    const fill = m.kind === 'stop' ? 'bg-rose-400' : m.crossed ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.9)]' : bg[m.inst]
    if (m.inst === 'future') return <span className={`block h-[11px] w-[11px] rotate-45 ${fill}`} />
    if (m.inst === 'option') {
      const tri = m.kind === 'stop' ? 'border-b-rose-400' : m.crossed ? 'border-b-emerald-400' : 'border-b-amber-300'
      return <span className={`block h-0 w-0 border-x-[7px] border-b-[12px] border-x-transparent ${tri}`} />
    }
    return <span className={`block h-3 w-3 rounded-full ${fill}`} />
  }
  const pctTxt = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
  const ticks = (() => {
    const range = hi - lo, step = range > 6 ? 2 : range > 3 ? 1 : range > 1.2 ? 0.5 : 0.25
    const out: number[] = []
    for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(Math.round(t * 100) / 100)
    return out
  })()
  const next = (inst: RailMark['inst']) => marks.find((m) => m.inst === inst && m.kind === 'target' && !m.crossed)
  const anyOf = (inst: RailMark['inst']) => marks.some((m) => m.inst === inst && m.kind === 'target')
  const stopOf = (inst: RailMark['inst']) => marks.find((m) => m.inst === inst && m.kind === 'stop')
  const risk = Math.abs(fav(c.stop)), reward = c.targets?.length ? fav(c.targets[0]) : null
  const label = { equity: 'Equity', future: 'Future', option: 'Option' }
  const nextTxt = (inst: RailMark['inst']) => {
    if (!anyOf(inst)) return null
    const n = next(inst)
    if (!n) return <span key={inst} className={col[inst]}>{label[inst]}: all crossed ✓</span>
    const more = nowMove !== null ? n.move - nowMove : n.move
    return <span key={inst}><span className={col[inst]}>{label[inst]} {n.code.replace(/^[EFO]/, 'T')} {f(n.price)}</span> <span className="text-slate-400">({inst === 'option' ? `stock ${pctTxt(n.move)}` : `${pctTxt(more)} more`})</span></span>
  }
  return (
    <div className="rounded-xl bg-slate-950/50 px-4 pb-3 pt-3">
      <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-[12px] font-semibold uppercase tracking-wider text-slate-400">trade map · stock move from the trigger close {f(c.entry)}{bull ? '' : ' (a fall is progress)'}</span>
        <span className="text-[13px] text-slate-400">risk <b className="text-rose-300">{risk.toFixed(2)}%</b>{reward !== null ? <> · to T1 <b className="text-emerald-300">{reward.toFixed(2)}%</b></> : <> · <span className="text-slate-500">no target</span></>} · RR <b className="text-slate-100">{f(c.rr, 1)}</b></span>
      </div>
      <div className="relative h-[118px]">
        {/* the rail, the stretch from the entry to now, the entry */}
        <div className="absolute left-[2%] right-[2%] top-[58px] h-[6px] rounded-full bg-slate-800" />
        <div className="absolute top-[58px] h-[6px] rounded-l-full bg-rose-500/40" style={{ left: `${x(lo)}%`, width: `${x(0) - x(lo)}%` }} />
        {nowMove !== null && (
          <div className={`absolute top-[58px] h-[6px] ${nowMove >= 0 ? 'bg-emerald-500/60' : 'bg-rose-500/70'}`} style={{ left: `${Math.min(x(0), x(nowMove))}%`, width: `${Math.abs(x(nowMove) - x(0))}%` }} />
        )}
        <div className="absolute top-[53px] -translate-x-1/2" style={{ left: `${x(0)}%` }} title={`entry — trigger close ${f(c.entry)}${optEntry ? ` · option ${f(optEntry)}` : ''}${fut ? ` · future ${f(fut.close)}` : ''}`}>
          <span className="block h-4 w-4 rounded-full bg-amber-300 ring-4 ring-slate-950" />
        </div>
        <span className="absolute top-[74px] -translate-x-1/2 whitespace-nowrap text-[12px] font-semibold uppercase tracking-wide text-amber-300" style={{ left: `${x(0)}%` }}>{nowAtEntry ? 'entry · now' : 'entry'}</span>
        {marks.map((m) => (
          <div key={m.key} className="absolute top-0 h-full -translate-x-1/2" style={{ left: `${x(m.move)}%` }}
            title={`${label[m.inst]} ${m.kind === 'stop' ? 'stop' : m.code.replace(/^[EFO]/, 'T')} ${f(m.price)}${m.zone ? ` · ${m.zone}` : ''} · stock ${pctTxt(m.move)}${m.inst === 'option' ? ' (via δ)' : ''}${m.crossed ? ' · crossed ✓' : ''}${off(m.move) ? ' · beyond the scale' : ''}`}>
            <span className={`absolute left-1/2 -translate-x-1/2 whitespace-nowrap text-[12.5px] font-bold ${m.kind === 'stop' ? 'text-rose-300' : m.crossed ? 'text-emerald-300' : col[m.inst]}`} style={{ top: `${4 + rowOf[m.key] * 15}px` }}>
              {m.crossed ? '✔ ' : ''}{m.code}{off(m.move) ? ' ›' : ''}
            </span>
            <div className="absolute left-1/2 top-[55px] flex -translate-x-1/2 items-center justify-center">{shape(m)}</div>
          </div>
        ))}
        {nowMove !== null && (
          <div className="absolute top-[70px] -translate-x-1/2 text-center" style={{ left: `${x(nowMove)}%` }} title={`stock now ${f(now)}`}>
            <div className="mx-auto h-0 w-0 border-x-[7px] border-b-[10px] border-x-transparent border-b-sky-300" />
            <div className="whitespace-nowrap text-[12px] font-semibold text-sky-300">now {pctTxt(nowMove)}</div>
          </div>
        )}
        <div className="absolute bottom-0 left-0 right-0 h-[16px]">
          {ticks.map((t) => <span key={t} className="absolute -translate-x-1/2 text-[11.5px] text-slate-500" style={{ left: `${x(t)}%` }}>{t === 0 ? '0' : pctTxt(t).replace('.00', '')}</span>)}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-slate-400">
        <span className="inline-flex items-center gap-1.5"><span className="block h-2.5 w-2.5 rounded-full bg-sky-300" />equity</span>
        <span className="inline-flex items-center gap-1.5"><span className="block h-2.5 w-2.5 rotate-45 bg-violet-300" />future</span>
        <span className="inline-flex items-center gap-1.5"><span className="block h-0 w-0 border-x-[5px] border-b-[9px] border-x-transparent border-b-amber-300" />option{marks.some((m) => m.inst === 'option') ? ` (via δ ${delta!.toFixed(2)})` : !optEntry ? ' — no contract' : ' — no delta, see Details'}</span>
        <span className="inline-flex items-center gap-1.5"><span className="text-emerald-300">✔</span> crossed</span>
        <span className="inline-flex items-center gap-1.5"><span className="block h-2.5 w-2.5 rounded-full bg-rose-400" />stops</span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-5 gap-y-1 text-[13.5px]">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-500">next</span>
        {(['equity', 'future', 'option'] as const).map((i) => nextTxt(i))}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-5 gap-y-1 text-[13.5px]">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-500">stops</span>
        {(['equity', 'future', 'option'] as const).map((i) => {
          const s = stopOf(i)
          return s ? <span key={i}><span className={col[i]}>{label[i]}</span> <span className="text-rose-300">{f(s.price)}</span> <span className="text-slate-500">({pctTxt(s.move)})</span></span> : null
        })}
      </div>
    </div>
  )
}

/** A 30m volume reading as a gauge: T (thick) and T−1 (thin) against the baseline, on 0 … 3×, with the
 *  dried line (0.85×) and the surge line (2.5×) marked. */
function SurgeMeter({ label, t, t1, title }: { label: string; t: number | null | undefined; t1: number | null | undefined; title?: string }) {
  const has = t !== null && t !== undefined
  const dried = has && t! < 0.85 && (t1 ?? 1) < 0.85, surge = has && t! >= 2.5
  const tone = dried ? 'bg-rose-400' : surge ? 'bg-emerald-400' : 'bg-sky-400'
  const w = (v: number | null | undefined) => `${Math.min(100, Math.max(0, ((v ?? 0) / 3) * 100))}%`
  if (!has) {
    return (
      <div title={title} className="flex items-baseline justify-between">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-400">{label}</span>
        <span className="text-[13px] text-slate-500">no reading at this bar</span>
      </div>
    )
  }
  return (
    <div title={title} className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-400">{label}</span>
        <span className={`text-[14px] font-semibold ${dried ? 'text-rose-300' : surge ? 'text-emerald-300' : 'text-slate-100'}`}>{has ? `${f(t)}×` : '—'}<span className="text-[12px] font-normal text-slate-500"> / {f(t1)}×{dried ? ' · dried' : surge ? ' · surge' : ''}</span></span>
      </div>
      <div className="relative h-[14px] rounded bg-slate-800/80">
        <div className={`absolute left-0 top-[2px] h-[6px] rounded ${tone}`} style={{ width: w(t) }} />
        <div className={`absolute left-0 top-[10px] h-[2px] rounded opacity-60 ${tone}`} style={{ width: w(t1) }} />
        <div className="absolute top-0 h-full w-px bg-rose-300/70" style={{ left: `${(0.85 / 3) * 100}%` }} title="0.85× — dried" />
        <div className="absolute top-0 h-full w-px bg-emerald-300/70" style={{ left: `${(2.5 / 3) * 100}%` }} title="2.5× — surge" />
      </div>
    </div>
  )
}

/** The share of the market moving the trigger's way, with RT-Y's 50 % line. */
function BreadthMeter({ share, names }: { share: number | null | undefined; names?: number }) {
  if (share === null || share === undefined) return null
  const with_ = share > 0.5
  return (
    <div className="flex flex-col gap-1" title="the share of the NSE universe trading beyond its own day open in the trigger's direction, at the trigger">
      <div className="flex items-baseline justify-between">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-slate-400">market with the trigger</span>
        <span className={`text-[14px] font-semibold ${with_ ? 'text-emerald-300' : 'text-rose-300'}`}>{(share * 100).toFixed(0)}%<span className="text-[12px] font-normal text-slate-500"> of {names ?? '—'}</span></span>
      </div>
      <div className="relative h-[14px] rounded bg-slate-800/80">
        <div className={`absolute left-0 top-[3px] h-[8px] rounded ${with_ ? 'bg-emerald-400' : 'bg-rose-400'}`} style={{ width: `${share * 100}%` }} />
        <div className="absolute top-0 h-full w-px bg-slate-300/80" style={{ left: '50%' }} title="50 % — RT-Y's breadth gate" />
      </div>
    </div>
  )
}

function Stat({ k, v, tone }: { k: string; v: React.ReactNode; tone?: string }) {
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="text-[11.5px] font-semibold uppercase tracking-wide text-slate-500">{k}</span>
      <span className={`text-[17px] font-bold ${tone ?? 'text-slate-100'}`}>{v}</span>
    </div>
  )
}

function KV({ k, v, tone, title }: { k: string; v: React.ReactNode; tone?: string; title?: string }) {
  return <span title={title} className="whitespace-nowrap text-[13.5px] text-slate-400">{k} <b className={`font-semibold ${tone ?? 'text-slate-100'}`}>{v}</b></span>
}

function TriggerCard({ book, c, open, onToggle, refresh, readOnly = false }: { book: string; c: Card; open: boolean; onToggle: () => void; refresh: () => void; readOnly?: boolean }) {
  const st = stateOf(c.state)
  const bull = c.direction === 'BULLISH'
  const conf = c.confluence ?? {}
  const eq = c.route?.reads?.find((r) => r.leg === 'equity'), fut = c.route?.reads?.find((r) => r.leg === 'future')
  const eqSurge = eq?.surgeT ?? c.surgeT, eqSurge1 = eq?.surgeT1 ?? c.surgeT1
  const futT = fut ? fut.surgeT : c.futLevels?.surgeT ?? null, futT1 = fut ? fut.surgeT1 : c.futLevels?.surgeT1 ?? null
  const behind = c.clusters.filter((z) => z.side === 'behind'), ahead = c.clusters.filter((z) => z.side === 'ahead')
  const pnl = c.live ? (c.live.unrealised ?? 0) + c.live.realised : c.trade ? c.trade.net : null
  const mine = c.books?.find((b) => b.book === book)
  const heldOpen = c.state === 'OPEN' || mine?.status === 'OPEN'
  const exited = !heldOpen && (mine?.status === 'EXITED' || c.state === 'TRADED')
  const exitWhy = mine?.exitReason ?? c.trade?.exit_reason ?? c.position?.exit_reason ?? ''
  // the side THIS book buys: a fade book's contract is the trigger's opposite
  const buys = c.side ?? (bull ? 'CE' : 'PE')
  const longSide = buys === 'CE' || buys === 'LONG'
  const fading = bull !== longSide
  // NSE's last bar is 15:15–15:30 (the close), decided at 15:30 — not 15:45; MCX bars (futures, LONG/SHORT) run to 23:30
  const barEnd = ist(c.ts, false) === '15:15' && (buys === 'CE' || buys === 'PE') ? c.ts + 900 : c.ts + 1800
  const sideText = fading ? `${bull ? 'Bullish' : 'Bearish'} trigger · fade buys ${buys}` : `${bull ? 'Bullish' : 'Bearish'} · buy ${buys}`
  const contract = contractName(c.position?.instrument.name ?? c.plan?.contract ?? c.cta?.contract) ?? `${c.symbol} ${c.side ?? (bull ? 'CE' : 'PE')} — no listed contract`
  const lots = c.position ? `${c.position.qty / c.position.instrument.lot_size} lots (${c.position.qty.toLocaleString('en-IN')})` : c.plan?.ok ? `${c.plan.lots} lots (${(c.plan.qty ?? 0).toLocaleString('en-IN')}) · ${inr0(c.plan.outlay)}` : null
  // the one sentence this card exists to say: what THIS book did, and why
  const why: { text: React.ReactNode; tone: Tone } = (() => {
    if (c.live) return { tone: 'amber', text: <>Holding · mid <b>{f(c.live.mid)}</b> ({pct(c.live.mid, c.position!.entry)} on the premium) · peak {f(c.live.peak)} · exit line {f(Math.max(c.live.line, c.live.optionSl))} · {c.live.armedBy ? `armed by ${c.live.armedBy}` : 'not armed yet'} · {c.live.qtyRemaining}/{c.live.qty} left</> }
    if (c.trade) return { tone: c.trade.net >= 0 ? 'emerald' : 'rose', text: <>Closed — {c.trade.exit_reason} · net <b>{inr(c.trade.net)}</b> · held {Math.round(c.trade.duration_s / 60)} min · best {f(c.trade.mfe_r)}R · worst {f(c.trade.mae_r)}R</> }
    if (c.state === 'CARRIED') return { tone: 'sky', text: <>Decided after the close — carried to the next session's 09:15 open</> }
    if (c.skip) return { tone: 'rose', text: <>Skipped at {ist(c.skip.ts, false)} — {human(c.skip.reason)}</> }
    if (c.state === 'NO_FILL') return { tone: 'slate', text: <>No entry — {c.parentDecision?.replace(/_/g, ' ').toLowerCase()}: {human(c.parentReason)}</> }
    if (c.state === 'IN_TREND') return { tone: 'slate', text: <>Routed in trend — the fade books stay flat. {c.route?.summary}</> }
    if (c.fade && !c.position) return { tone: 'orange', text: <>Fade {c.fade.direction} planned (stop {f(c.fade.stop)}, T1 {f(c.fade.targets?.[0])}) — {c.fade.decision ?? 'pending'} {c.fade.decision_reason ?? ''}</> }
    return { tone: 'slate', text: <>{c.parentDecision?.replace(/_/g, ' ').toLowerCase()} — {human(c.parentReason)}</> }
  })()
  const whyTone: Record<Tone, string> = { rose: 'border-rose-400/50 text-rose-100', amber: 'border-amber-300/60 text-amber-100', emerald: 'border-emerald-400/50 text-emerald-100', sky: 'border-sky-400/50 text-sky-100', orange: 'border-orange-400/50 text-orange-100', slate: 'border-slate-500/60 text-slate-200' }
  return (
    <article className={`relative overflow-hidden rounded-2xl border bg-slate-900/80 ${open ? 'border-sky-400/60' : 'border-slate-800'}`}>
      {/* the side this book trades: CE / long green, PE / short red — never the state's colour */}
      <div className={`absolute bottom-0 left-0 top-0 w-1.5 ${c.side === 'CE' || c.side === 'LONG' ? 'bg-emerald-400' : c.side === 'PE' || c.side === 'SHORT' ? 'bg-rose-500' : 'bg-slate-600'}`} />
      {exited && (
        <div title={`exited ${mine?.closedTs ? ist(mine.closedTs) : ''} · ${exitWhy}`} className="absolute right-[-46px] top-[20px] z-10 w-[170px] rotate-45 bg-slate-500 py-1 text-center text-[12px] font-bold tracking-[0.2em] text-slate-950">EXITED</div>
      )}
      <div className="px-7 pb-5 pt-5">
        {/* GLANCE: who, which way, what it would buy — and this book's verdict */}
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="flex min-w-0 items-start gap-3.5">
            <span className={`mt-1 inline-flex h-10 w-10 flex-none items-center justify-center rounded-xl ${bull ? 'bg-emerald-400/15 text-emerald-300' : 'bg-rose-400/15 text-rose-300'}`}><I d={bull ? IC.up : IC.down} /></span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="text-[30px] font-bold leading-none tracking-tight text-slate-50">{c.symbol}</span>
                <span className={`text-[15px] font-semibold ${longSide ? 'text-emerald-300' : 'text-rose-300'}`}>{sideText}</span>
                <span className="text-[14px] text-slate-400">fired {ist(barEnd, false)}<span className="text-slate-600"> · bar {ist(c.ts, false)}–{ist(barEnd, false)}</span></span>
              </div>
              <div className="mt-1.5 text-[14.5px] text-slate-300">{contract}{lots ? <span className="text-slate-400"> · {lots}</span> : null}{c.position ? <span className="text-slate-500"> · filled {ist(c.position.opened_ts)}</span> : null}</div>
            </div>
          </div>
          <div className={`flex flex-col items-end gap-2.5 ${exited ? 'pr-16' : ''}`}>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {heldOpen && <span className="inline-flex animate-pulse items-center gap-1.5 rounded-full bg-amber-400/20 px-3.5 py-1 text-[14px] font-bold tracking-[0.15em] text-amber-200"><span className="h-2 w-2 rounded-full bg-amber-300" />OPEN</span>}
              <Pill tone={st.tone} big>{st.label}</Pill>
              {c.routeLabel && <Pill tone={routeTone(c.routeLabel)} icon={IC.route} big title={c.route?.reason ?? c.skip?.reason}>{c.routeLabel}</Pill>}
            </div>
            <div className="flex items-end gap-5">
              <Stat k="grade" v={c.grade ?? '—'} tone={c.grade === 'F' ? 'text-rose-300' : undefined} />
              <Stat k="RR" v={f(c.rr, 1)} />
              {c.rtCard?.confidence ? <Stat k="conf" v={c.rtCard.confidence.score.toFixed(0)} /> : null}
              {c.rtCard?.odds?.pT1 != null ? <Stat k="P(T1)" v={`${c.rtCard.odds.pT1.toFixed(0)}%`} /> : null}
              {pnl !== null ? <Stat k={c.live ? 'P&L open' : 'P&L'} v={inr(pnl)} tone={pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'} /> : null}
            </div>
          </div>
        </div>

        {/* the one line to read first */}
        <div className={`mt-4 rounded-lg border-l-4 bg-slate-950/40 px-4 py-2.5 text-[15px] leading-relaxed ${whyTone[why.tone]}`}>{why.text}</div>
        {c.verdicts?.rtY || c.verdicts?.ctY || c.verdicts?.ctM ? <VerdictChips v={c.verdicts} /> : null}

        {/* SCAN: the trade map, and the readings beside it */}
        <div className="mt-4 grid grid-cols-[minmax(0,1.65fr)_minmax(280px,1fr)] gap-5">
          <TradeMap c={c} />
          <div className="flex flex-col justify-center gap-3.5 rounded-xl bg-slate-950/50 px-4 py-3">
            <SurgeMeter label="stock volume" t={eqSurge} t1={eqSurge1} title="trigger bar ÷ mean of T−2…T−7 · T / T−1" />
            <SurgeMeter label="future volume" t={futT} t1={futT1} />
            <BreadthMeter share={c.breadth?.share} names={c.breadth?.names} />
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5 px-1">
          <KV k="ATR 30m" v={f(c.atr)} />
          {c.stopPct > 0 && <KV k="stop" v={`${c.stopPct.toFixed(2)}% away`} tone={c.stopPct < 0.2 ? 'text-rose-300' : undefined} title={c.stopPct < 0.2 ? "inside one bar's noise" : ''} />}
          <KV k="first wall ahead" v={`${f(conf.fortress, 1)}`} tone={(conf.fortress ?? 0) >= 9 ? 'text-rose-300' : undefined} title="strength of the first wall ahead (≥ 9 blocks)" />
          <KV k="room" v={`${f(conf.room_ratio, 1)} ATR`} title="distance to the next wall" />
          <KV k="pivot clusters" v={`${behind.length} behind · ${ahead.length} ahead`} title={c.clusters.map((z) => `${z.price.toFixed(2)} (${z.strength.toFixed(1)}) ${z.members.join(',')}`).join('\n')} />
          <KV k="OI" v={<>{lakh(c.oi)}{c.oiChangePct ? <span className={c.oiChangePct > 0 ? 'text-emerald-300' : 'text-rose-300'}> {c.oiChangePct > 0 ? '+' : ''}{c.oiChangePct.toFixed(1)}%</span> : null}</>} />
          {c.route?.wall?.members?.length ? <KV k="wall ahead" v={`${f(c.route.wall.strength, 1)} · ${c.route.wall.timeframes}`} tone="text-orange-200" /> : null}
          <span className="text-[13.5px]"><span className="text-emerald-300">{c.pros.length} for</span><span className="text-slate-600"> · </span><span className="text-rose-300">{c.cons.length} against</span></span>
        </div>

        {c.books?.length ? <BookDots books={c.books} current={book} /> : null}

        <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-800 pt-4">
          {readOnly ? <span className="text-[12.5px] text-violet-300">read-only here — Take / Skip on the twin's own page</span> : <Cta book={book} c={c} onDone={refresh} />}
          <button onClick={onToggle} className="inline-flex flex-none items-center gap-1.5 rounded-lg px-3 py-1.5 text-[14px] font-semibold text-sky-300 hover:bg-sky-400/10 hover:text-sky-200" aria-expanded={open}>{open ? 'Hide details' : 'Details — levels, exit plan, reads, timeline'}<I d={IC.open} className={open ? 'rotate-180' : ''} /></button>
        </div>
      </div>
      {open && <Expanded book={book} c={c} />}
    </article>
  )
}

function Section({ icon, title, children }: { icon: string; title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-center gap-2 text-[12.5px] font-semibold uppercase tracking-wider text-slate-400"><I d={icon} className="text-slate-500" />{title}</div>
      {children}
    </div>
  )
}

/** STUDY: everything the card's face summarises, in full. */
function Expanded({ book, c }: { book: string; c: Card }) {
  const exitPlan = c.exitPlan ?? c.plan?.exitPlan ?? null
  return (
    <div className="border-t border-sky-400/30 bg-slate-950/70 px-7 py-6">
      <Section icon={IC.layers} title="levels · equity, future and option side by side"><Levels c={c} /></Section>
      <div className="mt-6 grid grid-cols-3 gap-7">
        <div className="flex flex-col gap-5">
          {exitPlan && <ExitPlan plan={exitPlan} />}
          <Section icon={IC.route} title="for and against">
            <div className="text-[14px] leading-relaxed">
              <div><b className="text-emerald-300">For:</b> <span className="text-slate-200">{c.pros.length ? c.pros.join(' · ') : '—'}</span></div>
              <div className="mt-1.5"><b className="text-rose-300">Against:</b> <span className="text-slate-200">{c.cons.length ? c.cons.join(' · ') : '—'}</span></div>
              {c.breadth && <div className="mt-1.5 text-slate-400"><b className="text-slate-300">Context:</b> trend efficiency {c.breadth.efficiency != null ? f(c.breadth.efficiency) : '—'} · own volatility {c.breadth.volBand ?? '—'} · gap {c.breadth.gapDatr != null ? `${f(c.breadth.gapDatr)} daily ATR` : '—'}{c.breadth.openBar ? ' · first bar (09:45)' : ''}</div>}
              {c.route && <div className="mt-1.5 text-slate-400">{c.route.reason}</div>}
            </div>
          </Section>
          {c.candle && (
            <Section icon={IC.chart} title="the trigger bar">
              <div className="flex flex-wrap gap-2">
                <Tile icon={IC.chart} k="open → close" v={`${f(c.candle.o)} → ${f(c.candle.c)}`} />
                <Tile icon={IC.activity} k="high · low" v={`${f(c.candle.h)} · ${f(c.candle.l)}`} />
                {c.atr ? <Tile icon={IC.activity} k="range" v={`${f((c.candle.h - c.candle.l) / c.atr, 1)} ATR · ${pct(c.candle.c, c.candle.o)}`} /> : null}
                <Tile icon={IC.bolt} k="volume" v={`${lakh(c.candle.v)} vs ${lakh(c.baseline)}`} />
              </div>
            </Section>
          )}
          {c.spark?.length > 1 && <Section icon={IC.chart} title="underlying since the trigger"><Spark c={c} /></Section>}
        </div>
        <div className="flex flex-col gap-5">
          <Section icon={IC.route} title="decision · both legs at the trigger close"><Reads r={c.route} /></Section>
          <Section icon={IC.layers} title="pivot clusters within 3 %">
            <table className="w-full border-collapse text-[13.5px]">
              <thead><tr className="text-[11.5px] uppercase tracking-wider text-slate-500">{['side', 'price', 'strength', 'members'].map((h) => <th key={h} className="border-b border-slate-800 px-2 py-1.5 text-left font-semibold">{h}</th>)}</tr></thead>
              <tbody>
                {c.clusters.map((z, i) => (
                  <tr key={i} className={`tabular-nums ${z.wall ? 'text-slate-100' : 'text-slate-400'}`}>
                    <td className={`px-2 py-1.5 ${z.side === 'ahead' ? 'text-emerald-300' : 'text-rose-300'}`}>{z.side}</td>
                    <td className="px-2 py-1.5">{f(z.price)} <span className="text-slate-500">{pct(z.price, c.entry)}</span></td>
                    <td className="px-2 py-1.5">{f(z.strength, 1)}{z.wall ? <span className="ml-1.5 rounded bg-orange-400/15 px-1.5 text-[11px] text-orange-200">WALL</span> : null}</td>
                    <td className="px-2 py-1.5 text-[12.5px]">{z.members.join(', ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          {c.rtCard && (
            <div className="flex flex-wrap gap-2">
              {c.rtCard.wallBehind && <Tile icon={IC.shield} k="wall behind" v={`${f(c.rtCard.wallBehind.price)} · ${f(c.rtCard.wallBehind.strength, 1)}`} tone="rose" />}
              {c.rtCard.wallAhead && <Tile icon={IC.flag} k="wall ahead" v={`${f(c.rtCard.wallAhead.price)} · ${f(c.rtCard.wallAhead.strength, 1)}`} tone="emerald" />}
              {c.rtCard.stop && <Tile icon={IC.shield} k="option stop (δ)" v={`${f(c.rtCard.stop.optionStop)} · δ ${f(c.rtCard.stop.delta)}`} tone="rose" />}
            </div>
          )}
          {c.fade && <div className="rounded-xl border border-orange-400/30 bg-orange-400/5 p-3 text-[14px] text-orange-100"><b>Fade plan (CT):</b> {c.fade.direction} · stop {f(c.fade.stop)} · T1 {f(c.fade.targets?.[0])} · grade {c.fade.grade} rr {f(c.fade.rr)}</div>}
        </div>
        <div className="flex flex-col gap-5">
          <Section icon={IC.wallet} title={`this book · ${BOOK_LABEL[book] ?? book}`}>
            {c.position ? (
              <div className="flex flex-wrap gap-2">
                <Tile icon={IC.ticket} k="fill" v={`${c.position.qty} @ ${f(c.position.entry)}`} tone="amber" />
                {c.live ? (<>
                  <Tile icon={IC.activity} k="mid · peak" v={`${f(c.live.mid)} · ${f(c.live.peak)}`} />
                  <Tile icon={IC.shield} k="line · option SL" v={`${f(c.live.line)} · ${f(c.live.optionSl)}`} tone="rose" />
                  <Tile icon={IC.trend} k="armed" v={c.live.armedTs ? `${c.live.armedBy} · ${ist(c.live.armedTs)}` : 'not yet'} tone={c.live.armedTs ? 'emerald' : undefined} />
                  <Tile icon={IC.layers} k="lots" v={`${c.live.qtyRemaining} of ${c.live.qty} · ${c.live.targetsHit} rung(s)`} />
                  <Tile icon={IC.wallet} k="P&L" v={`${inr(c.live.unrealised)} open · ${inr(c.live.realised)} booked`} tone={(c.live.unrealised ?? 0) + c.live.realised >= 0 ? 'emerald' : 'rose'} />
                  <Tile icon={IC.chart} k="underlying" v={f(c.live.underlying)} />
                  {c.live.edm ? <Tile icon={IC.activity} k="expected move" v={`${(c.live.edm * 100).toFixed(0)}% of premium`} tone="amber" /> : null}
                </>) : c.trade ? (<>
                  <Tile icon={IC.x} k="exit" v={`${f(c.trade.exit)} · ${c.trade.exit_reason}`} />
                  <Tile icon={IC.wallet} k="net · gross · charges" v={`${inr(c.trade.net)} · ${inr(c.trade.gross)} · ${inr0(c.trade.charges)}`} tone={c.trade.net >= 0 ? 'emerald' : 'rose'} />
                  <Tile icon={IC.activity} k="MFE · MAE" v={`${f(c.trade.mfe_r)}R · ${f(c.trade.mae_r)}R`} />
                  <Tile icon={IC.clock} k="held" v={`${Math.round(c.trade.duration_s / 60)} min`} />
                </>) : null}
                <div className="w-full text-[13px] text-slate-500">{c.position.note}</div>
              </div>
            ) : (
              <div className="text-[14px] text-slate-300">{stateOf(c.state).label} — {c.skip ? c.skip.reason : c.parentReason ?? c.route?.summary ?? ''}{c.plan && !c.plan.ok ? <div className="mt-1 text-slate-500">preview: {c.plan.reason}</div> : null}</div>
            )}
          </Section>
          {(c.pending || c.execLog || c.restingTarget || (c.restingTargets ?? []).length > 0) && (
            <Section icon={IC.ticket} title="order trail · limit orders">
              <div className="text-[13.5px] leading-relaxed">
                {(c.restingTargets && c.restingTargets.length > 0 ? c.restingTargets : c.restingTarget ? [c.restingTarget] : []).map((t) => <div key={`r${t.rung}`} className="text-emerald-300">RESTING · T{t.rung} sell {f(t.limit)} × {t.qty} ({t.lots} lot{t.lots === 1 ? '' : 's'}) since {ist(t.placedTs)} — fills on a touch</div>)}
                {c.pending && <div className="text-amber-300">PENDING · {contractName(c.pending.contract)} · limit {f(c.pending.limit)} resting {f(c.pending.restingS, 0)} s · {trail(c.pending)} · {c.pending.why}</div>}
                {c.execLog?.entry && <div className="text-slate-300"><b className="text-slate-200">Entry:</b> {trail(c.execLog.entry)}</div>}
                {(c.execLog?.exits ?? []).map((x, i) => <div key={i} className="text-slate-300"><b className="text-slate-200">Exit {x.reason ?? ''}{x.qty ? ` ×${x.qty}` : ''}:</b> {trail(x)}</div>)}
                {(c.execLog?.targets ?? []).filter((t) => t.outcome !== 'placed' && !t.outcome.startsWith('kept')).map((t, i) => <div key={`t${i}`} className="text-slate-400"><b className="text-slate-300">T{t.rung} resting sell {f(t.limit)} ×{t.qty}:</b> placed {ist(t.placedTs)} · {t.outcome} {ist(t.ts)}</div>)}
              </div>
            </Section>
          )}
          <Section icon={IC.clock} title="timeline"><Timeline c={c} /></Section>
        </div>
      </div>
    </div>
  )
}

/** ``apiBase`` '/api/peer' reads the twin engine's book through this one (phase 34 / phase 35 side by
 * side); ``readOnly`` hides Take / Skip, which would act on this engine, not the twin. */
export function BookCards({ book, apiBase = '/api', readOnly = false }: { book: string; apiBase?: string; readOnly?: boolean }) {
  // a past session's cards: /alerts?day=2026-09-29 (the page opens on today's)
  const day = new URLSearchParams(window.location.search).get('day')
  const { data, error, refresh } = usePoll<Resp>(`${apiBase}/books/${book}/cards${day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? `?day=${day}` : ''}`, 2000)
  const [filter, setFilter] = useState<string>('ALL')
  const [open, setOpen] = useState<string | null>(null)
  const cards = useMemo(() => (data?.cards ?? []).filter((c) => filter === 'ALL' || c.state === filter).slice().sort((a, b) => b.ts - a.ts), [data, filter])
  if (error && !data) return <div className="text-sm text-rose-400">Failed to load: {error}</div>
  if (!data) return <div className="text-sm text-slate-500">Loading…</div>
  const w = data.wallet
  const dayPnl = w ? w.balance - w.day_start_balance : 0
  const doRefresh = () => void refresh()
  return (
    <div className="max-w-[1500px]">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-[12.5px] font-semibold uppercase tracking-wider text-sky-300">book · {data.day}</div>
          <div className="text-[30px] font-bold tracking-tight text-slate-50">{BOOK_LABEL[book] ?? book}</div>
          <div className="mt-1 max-w-2xl text-[14px] text-slate-400">One card per FUDKII trigger, read for this book. Every trigger is scored the same way whether this book traded it or not, so a skip reads as “skipped, because”. Take / Skip are operator overrides, logged before they are attempted.</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Tile icon={IC.wallet} k="wallet" v={w ? inr0(w.balance) : '—'} />
          <Tile icon={IC.trend} k="day P&L" v={inr(dayPnl)} tone={dayPnl >= 0 ? 'emerald' : 'rose'} />
          <Tile icon={IC.ticket} k="trades" v={String(w?.trades ?? 0)} />
          <Tile icon={IC.layers} k="triggers" v={String(data.cards.length)} />
        </div>
      </div>
      <div className="mb-5 flex flex-wrap gap-2">
        <button onClick={() => setFilter('ALL')} className={`rounded-full border px-3.5 py-1.5 text-[13.5px] font-semibold ${filter === 'ALL' ? 'border-slate-400 bg-slate-800 text-white' : 'border-slate-700 text-slate-400'}`}>All · {data.cards.length}</button>
        {Object.entries(data.counts).sort().map(([s, n]) => {
          const st = stateOf(s)
          return <button key={s} onClick={() => setFilter(filter === s ? 'ALL' : s)} className={`rounded-full border px-3.5 py-1.5 text-[13.5px] font-semibold ${filter === s ? 'border-slate-300 bg-slate-800 text-white' : TONE[st.tone].chip}`}>{st.label} · {n}</button>
        })}
      </div>
      {cards.length === 0 ? (
        <div className="rounded-xl border border-slate-700/40 bg-slate-900/40 p-5 text-[15px] text-slate-400">No FUDKII trigger today yet. The book decides on each closed 30m bar.</div>
      ) : (
        <div className="flex flex-col gap-5">
          {cards.map((c) => <TriggerCard key={c.signalId} book={book} c={c} open={open === c.signalId} onToggle={() => setOpen(open === c.signalId ? null : c.signalId)} refresh={doRefresh} readOnly={readOnly} />)}
        </div>
      )}
    </div>
  )
}
