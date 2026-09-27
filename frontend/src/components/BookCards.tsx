import { useMemo, useState } from 'react'
import { postJson } from '../lib/api'
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
type Verdict = { action: string; state?: string; gate?: string | null; why: string[] | string; side?: string; stop?: number | null; targets?: number[]; rr?: number | null; grade?: string | null }
type TargetEv = { rung: number; limit: number; qty: number; placedTs: number; ts: number; outcome: string }
type Card = {
  verdicts?: { rtY: Verdict | null; ctY: Verdict | null }
  pending?: (ExecA & { restingS?: number; contract?: string; forTwins?: boolean; parentWhy?: string }) | null
  execLog?: { entry?: ExecA; exits?: (ExecA & { reason?: string; qty?: number })[]; targets?: TargetEv[] } | null
  // the next target SELL resting in advance (fills on a touch; any other exit cancels it first)
  restingTarget?: { rung: number; limit: number; qty: number; lots: number; placedTs: number } | null
  side?: 'CE' | 'PE' | 'LONG' | 'SHORT'
  breadth?: { share: number | null; names: number; efficiency?: number; volBand?: string | null; gapDatr?: number; pivotsAhead?: string[]; openBar?: boolean } | null
  books?: BookRow[]
  cta?: { action: 'take' | 'skip' | 'taken'; enabled: boolean; contract: string | null; type?: string | null; bid?: number | null; ask?: number | null; spreadPct?: number | null; reason: string | null }
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
  position: { id: string; entry: number; qty: number; qty_remaining: number; opened_ts: number; closed_ts: number | null; option_sl: number; option_targets: number[]; targets_hit: number; instrument: { name: string; lot_size: number; strike: number; option_type: string }; note: string; exit_reason?: string; exit_price?: number } | null
  trade: { net: number; gross: number; charges?: number; exit: number; exit_reason: string; mfe_r: number; mae_r: number; duration_s: number } | null
  exits: Order[]
  live: { mid: number; quoteOk: boolean | null; peak: number; line: number; optionSl: number; armedBy: string; armedTs: number | null; targetsHit: number; qtyRemaining: number; qty: number; ladder: number[]; edm: number; underlying: number | null; unrealised: number | null; realised: number } | null
  rtCard: { confidence?: { score: number }; odds?: { pT1: number | null; note?: string }; wallAhead?: { price: number; strength: number; members: string[] } | null; wallBehind?: { price: number; strength: number; members: string[] } | null; stop?: { equityStop: number; optionStop: number; delta: number } } | null
  pros: string[]; cons: string[]
}
type Resp = { book: string; day: string; wallet: { balance: number; day_start_balance: number; initial: number; trades: number } | null; counts: Record<string, number>; cards: Card[]; nowTs: number }

export const BOOK_TABS: [string, string][] = [
  ['FUDKII', 'FUDKII'], ['FUDKII_RT_X', 'FUDKII-RT-X'], ['FUDKII_RT_N', 'FUDKII-RT-N'], ['FUDKII_RT_Y', 'FUDKII-RT-Y'],
  ['FUDKII_CT_X', 'FUDKII-CT-X'], ['FUDKII_CT_Y', 'FUDKII-CT-Y'], ['FUDKII_RT_MCX', 'FUDKII-RT-MCX'],
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
  return <span title={title} className={`inline-flex items-center gap-1.5 rounded-full border font-semibold ${big ? 'px-3 py-1 text-[13px]' : 'px-2.5 py-[3px] text-[12px]'} ${TONE[tone].chip}`}>{icon && <I d={icon} className="opacity-90" />}{children}</span>
}
function Tile({ icon, k, v, tone, title }: { icon: string; k: string; v: React.ReactNode; tone?: Tone; title?: string }) {
  return (
    <div title={title} className="flex min-w-[120px] items-center gap-2.5 rounded-xl border border-slate-800 bg-slate-950/50 px-3 py-2">
      <I d={icon} className={`${tone ? TONE[tone].text : 'text-slate-500'}`} />
      <div className="flex flex-col leading-tight">
        <span className="text-[10.5px] uppercase tracking-wider text-slate-500">{k}</span>
        <span className={`font-mono text-[13.5px] font-medium tabular-nums ${tone ? TONE[tone].text : 'text-slate-100'}`}>{v}</span>
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
  const optHead = p ? `${p.instrument.name} · ${p.qty / p.instrument.lot_size} lots (${p.qty})` : plan?.ok ? `${plan.contract} · ${plan.lots} lots (${plan.qty})` : plan ? `no contract — ${plan.reason}` : 'no contract'
  const n = Math.max(c.targets?.length ?? 0, fut?.ahead.length ?? 0, optLadder.length, 1)
  const rows: { key: string; tone: Tone; icon: string; eq: React.ReactNode; fu: React.ReactNode; op: React.ReactNode }[] = []
  const cell = (price: number | null | undefined, sub?: string | null, ref?: number) => (
    <div className="flex flex-col leading-tight">
      <span className="font-mono text-[14px] font-medium tabular-nums">{f(price)}{ref && price ? <span className="ml-1.5 text-[11px] font-normal text-slate-500">{pct(price, ref)}</span> : null}</span>
      {sub ? <span className="text-[10.5px] text-slate-500">{sub}</span> : null}
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
          <tr className="bg-slate-950/70 text-[11px] uppercase tracking-wider text-slate-400">
            <th className="w-[92px] px-3 py-2 text-left font-semibold">{bull ? 'long' : 'short'} bias</th>
            <th className="px-3 py-2 text-left font-semibold"><span className="inline-flex items-center gap-1.5"><I d={IC.chart} className="text-sky-300" />Equity</span></th>
            <th className="px-3 py-2 text-left font-semibold"><span className="inline-flex items-center gap-1.5"><I d={IC.activity} className="text-sky-300" />Future</span>{fut ? <span className="ml-2 normal-case tracking-normal text-slate-500">vol {f(fut.surgeT)}× / {f(fut.surgeT1)}×</span> : null}</th>
            <th className="px-3 py-2 text-left font-semibold"><span className="inline-flex items-center gap-1.5"><I d={IC.ticket} className="text-amber-300" />Option</span><span className="ml-2 normal-case tracking-normal text-slate-500">{optHead}</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className={`border-t border-slate-800/80 ${TONE[r.tone].soft}`}>
              <td className={`px-3 py-2 text-[12px] font-semibold uppercase tracking-wide ${TONE[r.tone].text}`}><span className="inline-flex items-center gap-1.5"><I d={r.icon} />{r.key}</span></td>
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
      <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.route} className="text-slate-500" />exit plan <span className="normal-case tracking-normal text-slate-500">· {plan.policy}</span></div>
      <div className="flex flex-col gap-1.5">
        {plan.rows.map((r, i) => (
          <div key={i} className="flex items-center gap-3 text-[12.5px]">
            <I d={icon[r.kind]} className={TONE[tone[r.kind]].text} />
            <span className="flex-1 text-slate-200">{r.at}</span>
            <span className={`font-mono tabular-nums ${TONE[tone[r.kind]].text}`}>{r.action}{r.qty !== null ? ` · ${r.qty}` : ''}</span>
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
  if (c.position) rows.push({ ts: c.position.opened_ts, what: 'filled', detail: `${c.position.qty} × @ ${f(c.position.entry)} · ${c.position.instrument.name}`, tone: 'amber' })
  for (const o of c.exits) rows.push({ ts: o.ts, what: 'exit', detail: `${o.filled ?? o.qty} @ ${f(o.avg_price)} — ${o.reason}`, tone: 'emerald' })
  for (const e of c.operator) rows.push({ ts: e.ts, what: e.kind.replace('operator.', 'operator '), detail: 'override, audited', tone: 'amber' })
  if (c.trade) rows.push({ ts: c.position?.closed_ts ?? c.ts, what: 'closed', detail: `${c.trade.exit_reason} · net ${inr(c.trade.net)} · MFE ${f(c.trade.mfe_r)}R MAE ${f(c.trade.mae_r)}R`, tone: c.trade.net >= 0 ? 'emerald' : 'rose' })
  rows.sort((a, b) => a.ts - b.ts)
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r, i) => (
        <div key={i} className="flex items-start gap-3 text-[13px]">
          <span className={`w-[76px] flex-none font-mono ${TONE[r.tone].text}`}>{ist(r.ts)}</span>
          <span className={`w-[118px] flex-none font-semibold ${TONE[r.tone].text}`}>{r.what}</span>
          <span className="text-slate-300">{r.detail}</span>
        </div>
      ))}
    </div>
  )
}

function Reads({ r }: { r: Route | null }) {
  if (!r?.reads?.length) return <div className="text-[13px] text-slate-500">{r ? r.reason : 'no route recorded for this trigger (routes are kept from 23-Sep evening; MCX has none)'}</div>
  return (
    <table className="w-full border-collapse text-[12.5px]">
      <thead><tr className="text-[10.5px] uppercase tracking-wider text-slate-500">{['leg', 'nearest pivot', 'dist · crossed', 'volume T / T−1', 'score'].map((h) => <th key={h} className="border-b border-slate-800 px-2 py-1.5 text-left font-semibold">{h}</th>)}</tr></thead>
      <tbody>
        {r.reads.map((x) => (
          <tr key={x.leg} className="font-mono tabular-nums">
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
  const act = async (kind: 'take' | 'skip') => {
    const verb = kind === 'take' ? `Enter ${BOOK_LABEL[book] ?? book} on ${c.symbol} now — ${c.plan?.lots} lots of ${c.plan?.contract} at the market (≈ ${inr0(c.plan?.outlay)})?` : `Close ${BOOK_LABEL[book] ?? book}'s ${c.symbol} position now, at the market?`
    if (!window.confirm(verb)) return
    setBusy(kind); setMsg(null)
    try {
      const r = await postJson<Record<string, unknown>>(`/api/books/${book}/${kind}`, { signal_id: c.signalId })
      // the book's own decision and why — never a silent "not entered" (audit, 2026-09-26)
      const how = [r.decision, r.reason].filter(Boolean).join(' — ')
      setMsg(kind === 'take' ? (r.entered ? `entered — ${how}` : r.decision === 'RESTING' ? `limit resting — ${r.reason}` : `not entered — ${how || 'see the ledger decision'}`) : 'closing')
      onDone()
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)) } finally { setBusy(null) }
  }
  return (
    <div className="flex flex-wrap items-center gap-2.5">
      {held ? (
        <button disabled={busy !== null} onClick={() => act('skip')} className="inline-flex items-center gap-2 rounded-xl bg-rose-500 px-4 py-2 text-[13.5px] font-bold text-slate-950 shadow-[0_6px_20px_-8px_rgba(251,113,133,0.8)] hover:bg-rose-400 disabled:opacity-40"><I d={IC.x} />SKIP · close {c.live?.qtyRemaining ?? c.position?.qty_remaining} at market</button>
      ) : (
        canTake ? (
          <button disabled={busy !== null} onClick={() => act('take')} className="inline-flex items-center gap-2 rounded-xl bg-emerald-400 px-4 py-2 text-[13.5px] font-bold text-slate-950 shadow-[0_6px_20px_-8px_rgba(52,211,153,0.8)] hover:bg-emerald-300 disabled:opacity-40"><I d={IC.check} />TAKE {c.plan?.contract} · {c.plan?.lots} lots ({c.plan?.qty}) · {inr0(c.plan?.outlay)}</button>
        ) : (
          // Not takeable: still name the contract, greyed out, with the reason underneath.
          <button disabled aria-disabled className="inline-flex cursor-not-allowed items-center gap-2 rounded-xl border border-slate-700 bg-slate-800 px-4 py-2 text-[13.5px] font-bold text-slate-500"><I d={IC.check} />{c.cta?.action === 'taken' || c.state === 'TRADED' ? 'TAKEN' : 'TAKE'} {c.cta?.contract ?? c.plan?.contract ?? `${c.symbol} ${c.cta?.type ?? c.side ?? ''} — no listed contract`}</button>
        )
      )}
      {canTake && <span className="text-[12px] text-slate-500">ask {f(c.plan?.ask)} · spread {f(c.plan?.spreadPct, 1)}% · δ {f(c.plan?.delta)}</span>}
      {msg && <span className="text-[12px] text-amber-300">{msg}</span>}
      {!held && c.cta?.reason && (
        <div className={`basis-full text-[12px] ${canTake ? 'text-amber-300/80' : 'text-slate-400'}`}>
          {canTake ? '' : 'Why disabled: '}{c.cta.reason}
          {!canTake && c.cta.bid != null && <span className="text-slate-500"> · bid {f(c.cta.bid)} / ask {f(c.cta.ask)}{c.cta.spreadPct != null ? ` · spread ${f(c.cta.spreadPct, 1)}%` : ''}</span>}
        </div>
      )}
    </div>
  )
}

/** Every book on this trigger at a glance: a glowing circle (green CE / long, red PE / short) while it
 *  holds the trade, a dim filled one once it has exited, a hollow one when it never bought. */
// Gate B and the 09:45 gap fade, on EVERY book's card (operator, 2026-09-26: "mention on all respective
// strategies as label"). Rose = RT-Y stands aside, emerald = RT-Y takes, amber = CT-Y fades.
function VerdictChips({ v }: { v: { rtY: Verdict | null; ctY: Verdict | null } }) {
  const y = v.rtY
  const c = v.ctY
  const yWhy = y ? (Array.isArray(y.why) ? y.why : [y.why]).filter(Boolean).join(' · ') : ''
  const yLabel = y ? `RT-Y: ${y.action}${y.state && y.state.startsWith('would') ? ' (would)' : ''}${y.action !== 'TAKE' && yWhy ? ` — ${yWhy}` : ''}` : ''
  const cPlan = c && c.action !== 'NONE'
    ? `CT-Y: ${c.action} ${c.side ?? ''} · stop ${f(c.stop)} · T1 ${f(c.targets?.[0])} · RR ${f(c.rr, 1)}${c.grade ? ` (${c.grade})` : ''}`
    : ''
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-[11.5px] font-semibold">
      {y ? (
        <span title={yWhy} className={`rounded-md border px-2 py-0.5 ${y.action === 'SKIP' ? 'border-rose-400/40 bg-rose-400/10 text-rose-200' : y.action === 'TAKE' ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200' : 'border-slate-500/50 bg-slate-800/70 text-slate-300'}`}>{yLabel}</span>
      ) : null}
      {c && c.action !== 'NONE' ? (
        <span title={Array.isArray(c.why) ? c.why.join(' · ') : c.why} className="rounded-md border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-amber-200">{cPlan}</span>
      ) : c ? (
        <span title={Array.isArray(c.why) ? c.why.join(' · ') : c.why} className="rounded-md border border-slate-600/60 bg-slate-800/70 px-2 py-0.5 text-slate-400">CT-Y: no fade — {Array.isArray(c.why) ? c.why.join(' · ') : c.why}</span>
      ) : null}
    </div>
  )
}

function BookDots({ books, current }: { books: BookRow[]; current: string }) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5">
      {books.map((b) => {
        const long = b.side === 'CE' || b.side === 'LONG'
        const dot = b.status === 'OPEN'
          ? long ? 'animate-pulse bg-emerald-400 shadow-[0_0_10px_2px_rgba(52,211,153,0.75)]' : 'animate-pulse bg-rose-500 shadow-[0_0_10px_2px_rgba(244,63,94,0.75)]'
          : b.status === 'EXITED'
            ? long ? 'bg-emerald-400/35' : 'bg-rose-500/35'
            : 'border border-slate-600 bg-transparent'
        const tip = [
          `${b.label}: ${b.status === 'NONE' ? 'did not buy' : b.status === 'OPEN' ? 'holding' : 'exited'}`,
          b.side ?? '',
          b.openedTs ? `in ${ist(b.openedTs)}` : '',
          b.closedTs ? `out ${ist(b.closedTs)}${b.exitReason ? ` (${b.exitReason})` : ''}` : '',
          b.pnl !== null && b.pnl !== undefined ? inr(b.pnl) : '',
        ].filter(Boolean).join(' · ')
        return (
          <span key={b.book} title={tip} className={`inline-flex items-center gap-1.5 text-[11.5px] ${b.book === current ? 'font-bold text-slate-100' : b.status === 'NONE' ? 'text-slate-500' : 'text-slate-300'}`}>
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${dot}`} />{b.label}
          </span>
        )
      })}
    </div>
  )
}

function TriggerCard({ book, c, open, onToggle, refresh }: { book: string; c: Card; open: boolean; onToggle: () => void; refresh: () => void }) {
  const st = stateOf(c.state)
  const bull = c.direction === 'BULLISH'
  const conf = c.confluence ?? {}
  const eq = c.route?.reads?.find((r) => r.leg === 'equity'), fut = c.route?.reads?.find((r) => r.leg === 'future')
  const eqSurge = eq?.surgeT ?? c.surgeT, eqSurge1 = eq?.surgeT1 ?? c.surgeT1
  const volTone = (t: number | null | undefined, t1: number | null | undefined): Tone => (t === null || t === undefined ? 'slate' : t >= 2.5 ? 'emerald' : t < 0.85 && (t1 ?? 1) < 0.85 ? 'rose' : 'slate')
  const surgeTxt = (t: number | null | undefined, t1: number | null | undefined) => (t === null || t === undefined ? '—' : `${f(t)}× / ${f(t1)}×`)
  const behind = c.clusters.filter((z) => z.side === 'behind'), ahead = c.clusters.filter((z) => z.side === 'ahead')
  const exitPlan = c.exitPlan ?? c.plan?.exitPlan ?? null
  const pnl = c.live ? (c.live.unrealised ?? 0) + c.live.realised : c.trade ? c.trade.net : null
  const outcome = (() => {
    if (c.live) return <>Open · mid <b className="text-slate-100">{f(c.live.mid)}</b> ({pct(c.live.mid, c.position!.entry)}) · peak {f(c.live.peak)} · line {f(Math.max(c.live.line, c.live.optionSl))} · {c.live.armedBy ? `armed by ${c.live.armedBy}` : 'not armed'} · {c.live.qtyRemaining}/{c.live.qty} left</>
    if (c.trade) return <>{c.exits.map((o, i) => <span key={i}>{ist(o.ts)} · {o.filled ?? o.qty} @ {f(o.avg_price)} <span className="text-slate-500">{o.reason.split(' — ')[0].slice(0, 64)}</span>{i < c.exits.length - 1 ? ' → ' : ''}</span>)}</>
    if (c.skip) return <>Declined at {ist(c.skip.ts)}: {c.skip.reason}</>
    if (c.state === 'NO_FILL') return <>No entry — the trigger: {c.parentDecision} · {c.parentReason}</>
    if (c.state === 'IN_TREND') return <>Routed IN TREND — the fade books stay flat. {c.route?.summary}</>
    if (c.fade && !c.position) return <>Fade {c.fade.direction} planned (stop {f(c.fade.stop)}, T1 {f(c.fade.targets?.[0])}) — {c.fade.decision ?? 'pending'} {c.fade.decision_reason ?? ''}</>
    return <>{c.parentDecision} · {c.parentReason}</>
  })()
  const mine = c.books?.find((b) => b.book === book)
  const heldOpen = c.state === 'OPEN' || mine?.status === 'OPEN'
  const exited = !heldOpen && (mine?.status === 'EXITED' || c.state === 'TRADED')
  const exitWhy = mine?.exitReason ?? c.trade?.exit_reason ?? c.position?.exit_reason ?? ''
  return (
    <article className={`relative overflow-hidden rounded-2xl border bg-gradient-to-b from-slate-900 to-slate-900/60 shadow-[0_14px_40px_-20px_rgba(0,0,0,0.8)] ${open ? 'border-sky-400/60' : 'border-slate-800'}`}>
      {/* the side this book trades: CE / long green, PE / short red — never the state's colour */}
      <div className={`absolute bottom-0 left-0 top-0 w-1.5 ${c.side === 'CE' || c.side === 'LONG' ? 'bg-emerald-400' : c.side === 'PE' || c.side === 'SHORT' ? 'bg-rose-500' : 'bg-slate-600'}`} />
      {/* this book's trade is over: a corner ribbon, the exit reason on hover */}
      {exited && (
        <div title={`exited ${mine?.closedTs ? ist(mine.closedTs) : ''} · ${exitWhy}`} className="absolute right-[-44px] top-[18px] z-10 w-[160px] rotate-45 bg-slate-500 py-1 text-center text-[11px] font-bold tracking-[0.2em] text-slate-950 shadow-[0_4px_12px_-4px_rgba(0,0,0,0.8)]">EXITED</div>
      )}
      <div className="px-6 pb-5 pt-5">
        <button onClick={onToggle} className="block w-full text-left" aria-expanded={open}>
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              <span className={`mt-0.5 inline-flex h-9 w-9 items-center justify-center rounded-xl border ${bull ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-300' : 'border-rose-400/40 bg-rose-400/10 text-rose-300'}`}><I d={bull ? IC.up : IC.down} /></span>
              <div className="flex flex-col gap-1">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="text-[26px] font-bold tracking-tight text-slate-50">{c.symbol}</span>
                  <span className="font-mono text-[14px] text-slate-400">{c.position?.instrument.name ?? c.plan?.contract ?? c.cta?.contract ?? `${c.symbol} ${c.side ?? (bull ? 'CE' : 'PE')} — no listed contract`}</span>
                  <span className={`text-[13px] font-semibold ${bull ? 'text-emerald-300' : 'text-rose-300'}`}>{bull ? 'BULLISH · buy CE' : 'BEARISH · buy PE'}</span>
                </div>
                <div className="font-mono text-[12.5px] text-slate-400">trigger {ist(c.ts, false)}–{ist(c.ts + 1800, false)} · fired {ist(c.ts + 1800)}{c.position ? ` · filled ${ist(c.position.opened_ts)} (+${(c.position.opened_ts - c.ts - 1800).toFixed(1)} s)` : ''}</div>
              </div>
            </div>
            <div className={`flex flex-col items-end gap-2 ${exited ? 'pr-14' : ''}`}>
              <div className="flex flex-wrap items-center justify-end gap-1.5">
                {heldOpen && (
                  <span title={`held since ${mine?.openedTs ? ist(mine.openedTs) : c.position ? ist(c.position.opened_ts) : ''}`} className="inline-flex animate-pulse items-center gap-1.5 rounded-full border border-amber-300/70 bg-amber-400/15 px-3 py-1 text-[13px] font-bold tracking-[0.18em] text-amber-200 shadow-[0_0_18px_rgba(251,191,36,0.55)]"><span className="h-2 w-2 rounded-full bg-amber-300 shadow-[0_0_8px_rgba(251,191,36,0.9)]" />OPEN</span>
                )}
                <Pill tone={st.tone} big>{st.label}</Pill>
                {c.routeLabel && <Pill tone={routeTone(c.routeLabel)} icon={IC.route} big title={c.route?.reason ?? c.skip?.reason}>{c.routeLabel}</Pill>}
              </div>
              <span className="font-mono text-[12.5px] text-slate-400">grade <b className="text-slate-100">{c.grade ?? '—'}</b> · RR <b className="text-slate-100">{f(c.rr, 1)}</b>{c.rtCard?.confidence ? <> · conf <b className="text-slate-100">{c.rtCard.confidence.score.toFixed(0)}</b></> : null}{c.rtCard?.odds?.pT1 !== null && c.rtCard?.odds?.pT1 !== undefined ? <> · P(T1) <b className="text-slate-100">{c.rtCard.odds.pT1.toFixed(0)}%</b></> : null}{pnl !== null ? <> · <b className={pnl >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{inr(pnl)}</b></> : null}</span>
            </div>
          </div>
        </button>

        {c.books?.length ? <BookDots books={c.books} current={book} /> : null}
        {c.verdicts?.rtY || c.verdicts?.ctY ? <VerdictChips v={c.verdicts} /> : null}

        <div className="mt-4 flex flex-wrap gap-2">
          <Tile icon={IC.activity} k="ATR 30m" v={f(c.atr)} />
          <Tile icon={IC.bolt} k="EQ volume" v={surgeTxt(eqSurge, eqSurge1)} tone={volTone(eqSurge, eqSurge1)} title="trigger bar ÷ mean of T−2…T−7 · T / T−1" />
          <Tile icon={IC.bolt} k="FUT volume" v={fut ? surgeTxt(fut.surgeT, fut.surgeT1) : c.futLevels ? surgeTxt(c.futLevels.surgeT, c.futLevels.surgeT1) : '—'} tone={fut ? volTone(fut.surgeT, fut.surgeT1) : 'slate'} />
          <Tile icon={IC.pie} k="OI" v={<>{lakh(c.oi)}{c.oiChangePct ? <span className={c.oiChangePct > 0 ? 'text-emerald-300' : 'text-rose-300'}> {c.oiChangePct > 0 ? '+' : ''}{c.oiChangePct.toFixed(1)}%</span> : null}</>} />
          <Tile icon={IC.flag} k="fortress · room" v={`${f(conf.fortress, 1)} · ${f(conf.room_ratio, 1)} ATR`} tone={(conf.fortress ?? 0) >= 9 ? 'rose' : undefined} title="strength of the first wall ahead · distance to the next wall" />
          <Tile icon={IC.layers} k="pivot clusters" v={`${behind.length} behind · ${ahead.length} ahead`} title={c.clusters.map((z) => `${z.price.toFixed(2)} (${z.strength.toFixed(1)}) ${z.members.join(',')}`).join('\n')} />
          {c.stopPct > 0 && <Tile icon={IC.shield} k="stop distance" v={`${c.stopPct.toFixed(2)}%`} tone={c.stopPct < 0.2 ? 'rose' : undefined} title={c.stopPct < 0.2 ? 'inside one bar\'s noise' : ''} />}
          {c.route?.wall?.members?.length ? <Tile icon={IC.flag} k="wall ahead" v={`${f(c.route.wall.strength, 1)} · ${c.route.wall.timeframes}`} tone="orange" /> : null}
        </div>

        <div className="mt-4"><Levels c={c} /></div>

        <div className="mt-4 grid grid-cols-[1fr_260px] gap-4">
          <div className="flex flex-col gap-3">
            {exitPlan && <ExitPlan plan={exitPlan} />}
            <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
              <div className="mb-1.5 flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.clock} className="text-slate-500" />outcome</div>
              <div className="text-[13px] leading-relaxed text-slate-200">{outcome}</div>
              <div className="mt-2 text-[12.5px]"><span className="text-emerald-300">{c.pros.length} for</span> <span className="text-slate-600">·</span> <span className="text-rose-300">{c.cons.length} against</span>{(c.cons[0] ?? c.pros[0]) && <span className="text-slate-500"> — {c.cons[0] ?? c.pros[0]}</span>}</div>
            </div>
          </div>
          <div className="flex flex-col gap-2 rounded-xl border border-slate-800 bg-slate-950/40 p-3">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.chart} className="text-slate-500" />underlying since trigger</div>
            <Spark c={c} />
            <div className="flex items-center gap-3 text-[11px] text-slate-500"><span className="inline-flex items-center gap-1"><span className="inline-block h-[2px] w-4 bg-rose-400" />stop</span><span className="inline-flex items-center gap-1"><span className="inline-block h-[2px] w-4 bg-emerald-400" />T1</span><span className="inline-flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full bg-amber-400" />trigger close</span></div>
          </div>
        </div>

        <div className="mt-4 flex items-center justify-between gap-3 border-t border-slate-800 pt-4">
          <Cta book={book} c={c} onDone={refresh} />
          <button onClick={onToggle} className="inline-flex items-center gap-1.5 text-[13px] font-semibold text-sky-300 hover:text-sky-200">{open ? 'Close details' : 'Details'}<I d={IC.open} className={open ? 'rotate-180' : ''} /></button>
        </div>
      </div>
    </article>
  )
}

function Expanded({ book, c }: { book: string; c: Card }) {
  return (
    <div className="col-span-2 rounded-2xl border border-sky-400/40 bg-slate-950/80 p-6">
      <div className="grid grid-cols-3 gap-6">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.route} />decision · both legs at the trigger close</div>
          <Reads r={c.route} />
          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3.5 text-[13px] leading-relaxed">
            <div><b className="text-emerald-300">For:</b> {c.pros.length ? c.pros.join(' · ') : '—'}</div>
            <div className="mt-1"><b className="text-rose-300">Against:</b> {c.cons.length ? c.cons.join(' · ') : '—'}</div>
            {c.breadth && (
              // descriptive labels, logged on every trigger for the A/B; neither gates anything
              <div className="mt-1 text-slate-400"><b className="text-slate-300">Context:</b> trend efficiency {c.breadth.efficiency != null ? f(c.breadth.efficiency) : '—'} · own volatility {c.breadth.volBand ?? '—'} · gap {c.breadth.gapDatr != null ? `${f(c.breadth.gapDatr)} daily ATR` : '—'}{c.breadth.openBar ? ' · first bar (09:45)' : ''}</div>
            )}
            {c.route && <div className="mt-1.5 text-slate-400">{c.route.reason}</div>}
          </div>
          {(c.pending || c.execLog || c.restingTarget) && (
            <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3.5 text-[12.5px] leading-relaxed">
              <div className="mb-1 text-[11px] uppercase tracking-wider text-slate-400">order trail · limit orders</div>
              {c.restingTarget && (
                <div className="text-emerald-300">RESTING · T{c.restingTarget.rung} sell {f(c.restingTarget.limit)} × {c.restingTarget.qty} ({c.restingTarget.lots} lot{c.restingTarget.lots === 1 ? '' : 's'}) since {ist(c.restingTarget.placedTs)} — fills on a touch</div>
              )}
              {c.pending && (
                <div className="text-amber-300">PENDING · {c.pending.contract} · limit {f(c.pending.limit)} resting {f(c.pending.restingS, 0)} s · {trail(c.pending)} · {c.pending.why}</div>
              )}
              {c.execLog?.entry && <div><b className="text-slate-300">Entry:</b> {trail(c.execLog.entry)}</div>}
              {(c.execLog?.exits ?? []).map((x, i) => (
                <div key={i}><b className="text-slate-300">Exit {x.reason ?? ''}{x.qty ? ` ×${x.qty}` : ''}:</b> {trail(x)}</div>
              ))}
              {(c.execLog?.targets ?? []).filter((t) => t.outcome !== 'placed').map((t, i) => (
                <div key={`t${i}`} className="text-slate-400"><b className="text-slate-300">T{t.rung} resting sell {f(t.limit)} ×{t.qty}:</b> placed {ist(t.placedTs)} · {t.outcome} {ist(t.ts)}</div>
              ))}
            </div>
          )}
          {c.candle && (
            <div className="flex flex-wrap gap-2">
              <Tile icon={IC.chart} k="trigger 30m" v={`${f(c.candle.o)} → ${f(c.candle.c)}`} />
              <Tile icon={IC.activity} k="high · low" v={`${f(c.candle.h)} · ${f(c.candle.l)}`} />
              {c.atr ? <Tile icon={IC.activity} k="range" v={`${f((c.candle.h - c.candle.l) / c.atr, 1)} ATR · ${pct(c.candle.c, c.candle.o)}`} /> : null}
              <Tile icon={IC.bolt} k="volume" v={`${lakh(c.candle.v)} vs ${lakh(c.baseline)}`} />
            </div>
          )}
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.layers} />pivot clusters within 3% · official 22-Sep levels</div>
          <table className="w-full border-collapse text-[12.5px]">
            <thead><tr className="text-[10.5px] uppercase tracking-wider text-slate-500">{['side', 'price', 'strength', 'members'].map((h) => <th key={h} className="border-b border-slate-800 px-2 py-1.5 text-left font-semibold">{h}</th>)}</tr></thead>
            <tbody>
              {c.clusters.map((z, i) => (
                <tr key={i} className={`font-mono tabular-nums ${z.wall ? 'text-slate-100' : 'text-slate-400'}`}>
                  <td className={`px-2 py-1.5 ${z.side === 'ahead' ? 'text-emerald-300' : 'text-rose-300'}`}>{z.side}</td>
                  <td className="px-2 py-1.5">{f(z.price)} <span className="text-slate-500">{pct(z.price, c.entry)}</span></td>
                  <td className="px-2 py-1.5">{f(z.strength, 1)}{z.wall ? <span className="ml-1.5 rounded bg-orange-400/15 px-1 text-[10px] text-orange-200">WALL</span> : null}</td>
                  <td className="px-2 py-1.5 text-[11.5px]">{z.members.join(', ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {c.rtCard && (
            <div className="flex flex-wrap gap-2">
              {c.rtCard.wallBehind && <Tile icon={IC.shield} k="wall behind" v={`${f(c.rtCard.wallBehind.price)} · ${f(c.rtCard.wallBehind.strength, 1)}`} tone="rose" />}
              {c.rtCard.wallAhead && <Tile icon={IC.flag} k="wall ahead" v={`${f(c.rtCard.wallAhead.price)} · ${f(c.rtCard.wallAhead.strength, 1)}`} tone="emerald" />}
              {c.rtCard.stop && <Tile icon={IC.shield} k="option stop (δ)" v={`${f(c.rtCard.stop.optionStop)} · δ ${f(c.rtCard.stop.delta)}`} tone="rose" />}
            </div>
          )}
          {c.fade && <div className="rounded-xl border border-orange-400/30 bg-orange-400/5 p-3 text-[13px] text-orange-100"><b>Fade plan (CT):</b> {c.fade.direction} · stop {f(c.fade.stop)} · T1 {f(c.fade.targets?.[0])} · grade {c.fade.grade} rr {f(c.fade.rr)}</div>}
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.wallet} />this book · {BOOK_LABEL[book] ?? book}</div>
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
              <div className="w-full text-[12px] text-slate-500">{c.position.note}</div>
            </div>
          ) : (
            <div className="text-[13px] text-slate-400">{stateOf(c.state).label} — {c.skip ? c.skip.reason : c.parentReason ?? c.route?.summary ?? ''}{c.plan && !c.plan.ok ? <div className="mt-1 text-slate-500">preview: {c.plan.reason}</div> : null}</div>
          )}
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-slate-400"><I d={IC.clock} />timeline</div>
          <Timeline c={c} />
        </div>
      </div>
    </div>
  )
}

export function BookCards({ book }: { book: string }) {
  const { data, error, refresh } = usePoll<Resp>(`/api/books/${book}/cards`, 2000)
  const [filter, setFilter] = useState<string>('ALL')
  const [open, setOpen] = useState<string | null>(null)
  const cards = useMemo(() => (data?.cards ?? []).filter((c) => filter === 'ALL' || c.state === filter).slice().sort((a, b) => b.ts - a.ts), [data, filter])
  if (error && !data) return <div className="text-sm text-rose-400">Failed to load: {error}</div>
  if (!data) return <div className="text-sm text-slate-500">Loading…</div>
  const w = data.wallet
  const dayPnl = w ? w.balance - w.day_start_balance : 0
  const pairs: Card[][] = []
  for (let i = 0; i < cards.length; i += 2) pairs.push(cards.slice(i, i + 2))
  const doRefresh = () => void refresh()
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-sky-300">book · {data.day}</div>
          <div className="text-[28px] font-bold tracking-tight text-slate-50">{BOOK_LABEL[book] ?? book}</div>
          <div className="mt-1 max-w-2xl text-[13px] text-slate-500">One card per FUDKII trigger. Every trigger is scored the same way whether this book traded it or not, so a skip reads as “skipped, because”. Take / Skip are operator overrides, written to the event log before they are attempted.</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Tile icon={IC.wallet} k="wallet" v={w ? inr0(w.balance) : '—'} />
          <Tile icon={IC.trend} k="day P&L" v={inr(dayPnl)} tone={dayPnl >= 0 ? 'emerald' : 'rose'} />
          <Tile icon={IC.ticket} k="trades" v={String(w?.trades ?? 0)} />
          <Tile icon={IC.layers} k="triggers" v={String(data.cards.length)} />
        </div>
      </div>
      <div className="mb-5 flex flex-wrap gap-2">
        <button onClick={() => setFilter('ALL')} className={`rounded-full border px-3 py-1 font-mono text-[12px] ${filter === 'ALL' ? 'border-slate-400 bg-slate-800 text-white' : 'border-slate-700 text-slate-400'}`}>All · {data.cards.length}</button>
        {Object.entries(data.counts).sort().map(([s, n]) => {
          const st = stateOf(s)
          return <button key={s} onClick={() => setFilter(filter === s ? 'ALL' : s)} className={`rounded-full border px-3 py-1 font-mono text-[12px] ${filter === s ? 'border-slate-300 bg-slate-800 text-white' : TONE[st.tone].chip}`}>{st.label} · {n}</button>
        })}
      </div>
      {cards.length === 0 ? (
        <div className="rounded-xl border border-slate-700/40 bg-slate-900/40 p-5 text-[14px] text-slate-500">No FUDKII trigger today yet. The book decides on each closed 30m bar.</div>
      ) : (
        <div className="grid grid-cols-2 gap-5">
          {pairs.map((pair, i) => (
            <PairRow key={i} pair={pair} book={book} open={open} setOpen={setOpen} refresh={doRefresh} />
          ))}
        </div>
      )}
    </div>
  )
}

function PairRow({ pair, book, open, setOpen, refresh }: { pair: Card[]; book: string; open: string | null; setOpen: (s: string | null) => void; refresh: () => void }) {
  const opened = pair.find((c) => c.signalId === open)
  return (
    <>
      {pair.map((c) => <TriggerCard key={c.signalId} book={book} c={c} open={open === c.signalId} onToggle={() => setOpen(open === c.signalId ? null : c.signalId)} refresh={refresh} />)}
      {pair.length === 1 && <div />}
      {opened && <Expanded book={book} c={opened} />}
    </>
  )
}
