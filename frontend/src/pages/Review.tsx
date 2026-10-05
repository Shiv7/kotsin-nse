import { useMemo, useState } from 'react'
import { CandleChart, type ChartBar, type Marker, type PriceLine } from '../components/CandleChart'
import { Badge, Card, ErrorLine, Stat, StrategyBadge } from '../components/Ui'
import { cls, contractName, fmt, pnlColor } from '../lib/api'
import { useEngine } from '../lib/engine'
import { usePoll } from '../lib/usePoll'

// Trade review (operator, 2026-10-05: "see all detailed view of each trade and analytics on why it incurred a loss.
// did we have to wait before entering, was a pivot around, was it a counter-trend? no volume? no oi? etc and be
// accurate in identifying the root cause"). Every line comes from the record (api/review.py); a question the record
// cannot answer is shown as unknown, never guessed.

type Status = 'ok' | 'warn' | 'bad' | 'unknown'
type Check = { key: string; question: string; status: Status; detail: string }
type Mirror = { rule: 'E' | 'A'; book: string; status: string; exitReason: string | null; exitPrice: number | null; net: number | null }
type Trade = {
  id: string; book: string; label: string; symbol: string; contract: string | null; direction: 'BULLISH' | 'BEARISH'; grade: string | null
  counterTrend: boolean; status: 'OPEN' | 'CLOSED'; qty: number; entry: number; exit: number | null; exitReason: string | null
  openedTs: number; closedTs: number | null; signalTs: number | null; net: number | null; gross: number | null; charges: number | null
  r: number | null; mfeR: number; maeR: number; maxDdR: number | null
  stock: { triggerClose: number | null; atFill: number | null; atExit: number | null; entry: number | null; stop: number | null; targets: number[]
    atr: number | null; bestAtr: number | null; worstAtr: number | null; hitStop: boolean | null; laterBestAtr: number | null; laterReachedT1: boolean; laterHitStopTs: number | null }
  option: { entry: number; stop: number | null; delta: number | null; excessPct: number | null; ref: number | null; midAtPlace: number | null }
  execution: { waitS: number | null; reprices: number; entrySlipInr: number | null; exitSlipInr: number | null; entryOutcome: string | null
    stop: { level: number; triggerPrice: number; triggerOn: string; bidAtTrigger: number | null } | null }
  levels: { ahead: { label: string; price: number | null; atr: number | null } | null }
  checks: Check[]
  cause: { key: string; label: string; why: string; contributing: string[] }
  mirrors: Mirror[]
  chart: { ts: number; o: number; h: number; l: number; c: number }[]
}
type Day = {
  day: string; trades: Trade[]; thresholds: Record<string, number>
  summary: { trades: number; closed: number; open: number; losses: number; wins: number; net: number; causes: { key: string; label: string; trades: number; net: number }[] }
}

const IST_S = 19800 // the chart's clock reads UTC; shift so its axis shows IST
const ICON: Record<Status, string> = { ok: '✓', warn: '⚠', bad: '✗', unknown: '?' }
const TONE: Record<Status, string> = { ok: 'text-emerald-400', warn: 'text-amber-300', bad: 'text-rose-400', unknown: 'text-slate-500' }
const hm = (ts: number | null | undefined) =>
  ts ? new Date((ts + IST_S) * 1000).toISOString().slice(11, 16) : '—'
const dur = (a: number | null, b: number | null) => (a && b ? fmt.dur(b - a) : '—')
const todayIst = () => new Date(Date.now() + IST_S * 1000).toISOString().slice(0, 10)

function CauseBox({ t }: { t: Trade }) {
  const tone = t.cause.key === 'win' ? 'border-emerald-700 bg-emerald-950/30' : t.cause.key === 'open' ? 'border-slate-700 bg-slate-900/50' : 'border-rose-800 bg-rose-950/30'
  return (
    <div className={cls('rounded border px-3 py-2', tone)}>
      <div className="text-[11px] uppercase tracking-wide text-slate-400">{t.cause.key === 'win' || t.cause.key === 'open' ? 'outcome' : 'root cause'}</div>
      <div className="text-sm font-semibold text-slate-100">{t.cause.label}</div>
      <div className="mt-0.5 text-[13px] text-slate-300">{t.cause.why}</div>
      {t.cause.contributing.length > 0 && (
        <div className="mt-1.5 text-[12px] text-slate-400">
          <span className="text-slate-500">also against it: </span>
          {t.cause.contributing.join(' · ')}
        </div>
      )}
    </div>
  )
}

function TradeChart({ t }: { t: Trade }) {
  const bull = t.direction === 'BULLISH'
  const { bars, lines, marks } = useMemo(() => {
    const bars: ChartBar[] = t.chart.map((b) => ({ ts: b.ts + IST_S, o: b.o, h: b.h, l: b.l, c: b.c }))
    const lines: PriceLine[] = []
    if (t.stock.entry) lines.push({ price: t.stock.entry, color: '#38bdf8', title: 'entry', width: 1 })
    if (t.stock.stop) lines.push({ price: t.stock.stop, color: '#f43f5e', title: 'stop', width: 2 })
    t.stock.targets.forEach((p, i) => lines.push({ price: p, color: '#34d399', title: `T${i + 1}`, style: 2 }))
    if (t.levels.ahead?.price) lines.push({ price: t.levels.ahead.price, color: '#f59e0b', title: t.levels.ahead.label, style: 1 })
    const snap = (ts: number) => Math.floor(ts / 60) * 60 + IST_S
    const marks: Marker[] = []
    if (t.openedTs) marks.push({ ts: snap(t.openedTs), position: bull ? 'belowBar' : 'aboveBar', color: '#38bdf8', shape: bull ? 'arrowUp' : 'arrowDown', text: 'in' })
    if (t.closedTs) marks.push({ ts: snap(t.closedTs), position: bull ? 'aboveBar' : 'belowBar', color: (t.net ?? 0) >= 0 ? '#34d399' : '#f43f5e', shape: 'circle', text: t.exitReason ?? 'out' })
    if (t.stock.laterHitStopTs) marks.push({ ts: snap(t.stock.laterHitStopTs), position: bull ? 'belowBar' : 'aboveBar', color: '#f59e0b', shape: 'square', text: 'stock at stop' })
    const known = new Set(bars.map((b) => b.ts))
    return { bars, lines, marks: marks.filter((m) => known.has(m.ts)) }
  }, [t, bull])
  if (!bars.length) return <div className="text-xs text-slate-500">no minute bars recorded for {t.symbol} this session</div>
  return <CandleChart bars={bars} priceLines={lines} markers={marks} height={260} />
}

function Facts({ t }: { t: Trade }) {
  const s = t.stock
  const row = (k: string, v: React.ReactNode) => (
    <div className="flex justify-between gap-3 border-b border-slate-900 py-0.5">
      <span className="text-slate-500">{k}</span>
      <span className="text-right text-slate-300">{v}</span>
    </div>
  )
  return (
    <div className="grid gap-x-6 text-[12px] sm:grid-cols-2 xl:grid-cols-4">
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">stock</div>
        {row('trigger close → at fill', `${fmt.n(s.triggerClose)} → ${fmt.n(s.atFill)}`)}
        {row('at exit', fmt.n(s.atExit))}
        {row('stop · targets', `${fmt.n(s.stop)} · ${s.targets.map((x) => fmt.n(x)).join(' / ') || '—'}`)}
        {row('ATR (30m)', fmt.n(s.atr))}
        {row('best · worst in the trade', `${fmt.n(s.bestAtr)} · ${fmt.n(s.worstAtr)} ATR`)}
        {row('after the exit', s.laterBestAtr == null ? '—' : `${fmt.n(s.laterBestAtr)} ATR the trade's way${s.laterReachedT1 ? ', reached T1' : ''}${s.laterHitStopTs ? `, at its stop ${hm(s.laterHitStopTs)}` : ''}`)}
      </div>
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">option</div>
        {row('paid · sold', `${fmt.n(t.entry)} · ${fmt.n(t.exit)}`)}
        {row('mid when ordered · signal price', `${fmt.n(t.option.midAtPlace)} · ${fmt.n(t.option.ref)}`)}
        {row('first stop', fmt.n(t.option.stop))}
        {row('delta (estimate)', t.option.delta == null ? '—' : fmt.n(t.option.delta))}
        {row('vs the stock’s move', t.option.excessPct == null ? '—' : `${t.option.excessPct > 0 ? '+' : ''}${t.option.excessPct.toFixed(1)} % of its price`)}
      </div>
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">execution</div>
        {row('waited for the fill', t.execution.waitS == null ? '—' : `${t.execution.waitS.toFixed(0)} s${t.execution.reprices ? `, ${t.execution.reprices} re-price` : ''}`)}
        {row('entry over the mid', fmt.signedInr(t.execution.entrySlipInr))}
        {row('exit under the stop', t.execution.exitSlipInr == null ? '—' : fmt.inr(t.execution.exitSlipInr))}
        {row('stop fired on', t.execution.stop ? `${t.execution.stop.triggerOn} ${fmt.n(t.execution.stop.triggerPrice)} (level ${fmt.n(t.execution.stop.level)})` : '—')}
        {row('charges', fmt.inr(t.charges))}
      </div>
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">result</div>
        {row('gross · net', `${fmt.signedInr(t.gross)} · ${fmt.signedInr(t.net)}`)}
        {row('best · worst (R)', `${fmt.r(t.mfeR)} · ${fmt.r(t.maeR)}`)}
        {row('max drawdown', t.maxDdR == null ? '—' : `−${t.maxDdR.toFixed(2)}R`)}
        {t.mirrors.map((m) => row(m.rule === 'E' ? 'same trade, stop E' : 'same trade, stop adaptive', m.status === 'OPEN' ? 'open' : `${m.exitReason ?? ''} ${fmt.signedInr(m.net)}`))}
      </div>
    </div>
  )
}

function TradeCard({ t }: { t: Trade }) {
  const [open, setOpen] = useState((t.net ?? 0) < 0)
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      <button onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left">
        <StrategyBadge k={t.book} label={t.label} />
        <span className="font-medium text-slate-100">
          {t.symbol}
          <span className={`ml-1 text-[10px] ${t.direction === 'BULLISH' ? 'text-emerald-500' : 'text-rose-500'}`}>{t.direction === 'BULLISH' ? '▲' : '▼'}</span>
        </span>
        <span className="text-xs text-slate-400">{contractName(t.contract)}</span>
        {t.counterTrend && <Badge tone="amber">counter-trend</Badge>}
        {t.grade && <Badge tone="slate">grade {t.grade}</Badge>}
        <span className="text-xs text-slate-500">
          {hm(t.openedTs)} → {t.closedTs ? hm(t.closedTs) : 'open'} · {t.status === 'OPEN' ? 'running' : dur(t.openedTs, t.closedTs)}
          {t.exitReason ? ` · ${t.exitReason}` : ''}
        </span>
        <span className="ml-auto flex items-center gap-3">
          <span className={cls('text-xs', t.cause.key === 'win' ? 'text-emerald-400' : t.cause.key === 'open' ? 'text-slate-400' : 'text-rose-300')}>{t.cause.label}</span>
          <span className={cls('text-base font-semibold', pnlColor(t.net))}>{t.net == null ? 'open' : fmt.signedInr(t.net)}</span>
          <span className="text-slate-500">{open ? '▾' : '▸'}</span>
        </span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-slate-800 px-3 py-3">
          <CauseBox t={t} />
          <div className="grid gap-3 xl:grid-cols-2">
            <div className="divide-y divide-slate-800/70">
              {t.checks.map((c) => (
                <div key={c.key} className="flex gap-2 py-1.5 text-[13px]">
                  <span className={cls('w-4 shrink-0 text-center font-bold', TONE[c.status])} title={c.status}>{ICON[c.status]}</span>
                  <div className="min-w-0">
                    <div className="text-slate-200">{c.question}</div>
                    <div className="text-[12px] text-slate-400">{c.detail}</div>
                  </div>
                </div>
              ))}
            </div>
            <div className="min-w-0">
              <TradeChart t={t} />
              <div className="mt-1 text-[10px] text-slate-500">
                one-minute bars · <span className="text-sky-400">entry</span> · <span className="text-rose-400">stop</span> · <span className="text-emerald-400">targets</span> ·{' '}
                <span className="text-amber-400">nearest level the trade's way</span>
              </div>
            </div>
          </div>
          <Facts t={t} />
        </div>
      )}
    </section>
  )
}

const FILTERS = [
  ['losses', 'Losses'],
  ['all', 'All'],
  ['open', 'Open'],
  ['wins', 'Wins'],
] as const

export function Review() {
  const [day, setDay] = useState(todayIst())
  const [filter, setFilter] = useState<(typeof FILTERS)[number][0]>('losses')
  const engine = useEngine()
  const [side, setSide] = useState<'self' | 'peer'>('self')
  const peer = side === 'peer' && engine?.peer ? engine.peer : null
  const { data, error } = usePoll<Day>(`${peer ? '/api/peer' : '/api'}/review?day=${day}`, 20000)
  const shown = (data?.trades ?? [])
    .filter((t) => filter === 'all' || (filter === 'open' ? t.status === 'OPEN' : filter === 'wins' ? t.status === 'CLOSED' && (t.net ?? 0) >= 0 : t.status === 'CLOSED' && (t.net ?? 0) < 0))
    .sort((a, b) => (a.net ?? 0) - (b.net ?? 0))
  const s = data?.summary
  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-100">Trade review{peer ? ` · ${peer.name}` : engine?.name ? ` · ${engine.name}` : ''}</h1>
        <input type="date" value={day} max={todayIst()} onChange={(e) => e.target.value && setDay(e.target.value)} className="rounded bg-slate-900 px-2 py-1 text-xs text-slate-200" />
        {engine?.peer && (
          <span className="flex items-center gap-1">
            {([['self', engine.name || 'this engine'], ['peer', engine.peer.name || 'twin']] as const).map(([k, label]) => (
              <button key={k} onClick={() => setSide(k)} className={cls('rounded px-2 py-0.5 text-[11px]', side === k ? 'bg-violet-800 text-white' : 'bg-slate-800 text-slate-400 hover:text-white')}>
                {label}
              </button>
            ))}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1">
          {FILTERS.map(([k, label]) => (
            <button key={k} onClick={() => setFilter(k)} className={cls('rounded px-2.5 py-1 text-xs', filter === k ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400 hover:text-white')}>
              {label}
            </button>
          ))}
        </span>
      </div>
      {s && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <Stat label="Trades" value={s.trades} sub={`${s.closed} closed · ${s.open} open`} />
          <Stat label="Net (closed)" value={fmt.signedInr(s.net)} tone={pnlColor(s.net)} />
          <Stat label="Losses" value={s.losses} tone="text-rose-400" />
          <Stat label="Wins" value={s.wins} tone="text-emerald-400" />
          <Stat label="Books" value={new Set(data!.trades.map((t) => t.book)).size} sub="the stop-rule mirrors ride on each trade" />
        </div>
      )}
      {s && s.causes.length > 0 && (
        <Card title="Why the losses happened" right="each loss's root cause, from the record — the biggest first">
          <div className="flex flex-wrap gap-2">
            {s.causes.map((c) => (
              <span key={c.key} className="rounded border border-rose-900/70 bg-rose-950/30 px-2.5 py-1 text-xs text-slate-200">
                {c.label} <b className="ml-1 text-slate-100">{c.trades}</b> <span className={cls('ml-1', pnlColor(c.net))}>{fmt.signedInr(c.net)}</span>
              </span>
            ))}
          </div>
        </Card>
      )}
      {!data ? (
        <div className="text-sm text-slate-500">loading…</div>
      ) : shown.length === 0 ? (
        <div className="text-sm text-slate-500">no {filter === 'all' ? '' : filter} trade on {day}</div>
      ) : (
        <div className="space-y-2">
          {shown.map((t) => (
            <TradeCard key={t.id} t={t} />
          ))}
        </div>
      )}
      {data && (
        <div className="text-[11px] text-slate-500">
          Rules, in the order a root cause is chosen: costs · a rule's close · exit cost · trail at breakeven · option stop before the stock's · stop inside the noise · gave
          back · failed fade · stopped at the level ahead · chased · entry cost · right but early · market against · OI against · no volume — the first that holds. Thresholds:{' '}
          {Object.entries(data.thresholds).map(([k, v]) => `${k} ${v}`).join(' · ')}.
        </div>
      )}
    </div>
  )
}
