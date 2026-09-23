import { useMemo, useState } from 'react'
import { postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'

// One card per FUDKII trigger, read for one book. Every trigger is scored the same way whether the
// book traded it or not, so a skip reads as "skipped, because" — never as "missed". Two cards per
// row; a card opens in place, under its row. Take / Skip are operator overrides (audited).

type Read = { leg: string; members: string[]; level: number; strength: number; distAtr: number; crossedAtr: number; rejected: boolean; volume: string; surgeT: number | null; surgeT1: number | null; score: number }
type Route = { route: string; reason: string; summary?: string; wall?: { strength: number; members: string[]; timeframes: string; distAtr: number | null; grade: string; leg?: string }; reads?: Read[]; ts?: number }
type Order = { ts: number; avg_price: number | null; filled: number | null; qty: number; reason: string; slippage_bps?: number | null; purpose: string }
type Card = {
  signalId: string; symbol: string; direction: 'BULLISH' | 'BEARISH'; ts: number; grade: string | null; rr: number | null; reason: string
  entry: number; stop: number; targets: number[] | null; stopPct: number
  confluence: { stop_zone?: string; target_zones?: string[]; fortress?: number; room_ratio?: number; note?: string }
  evidence: Record<string, number>; parentDecision: string | null; parentReason: string | null
  candle: { o: number; h: number; l: number; c: number; v: number } | null; surgeT: number | null; surgeT1: number | null; baseline: number | null
  spark: [number, number][]; route: Route | null; skip: { reason: string; ts: number } | null; operator: { kind: string; ts: number }[]
  fade: { signal_id: string; direction: string; stop: number; targets: number[]; grade: string; rr: number; decision?: string; decision_reason?: string } | null
  state: string
  position: { id: string; entry: number; qty: number; qty_remaining: number; opened_ts: number; closed_ts: number | null; option_sl: number; option_targets: number[]; targets_hit: number; instrument: { name: string; lot_size: number; strike: number; option_type: string }; note: string; exit_reason?: string; exit_price?: number } | null
  trade: { net: number; gross: number; exit: number; exit_reason: string; mfe_r: number; mae_r: number; duration_s: number } | null
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

type Tone = 'emerald' | 'amber' | 'rose' | 'fuchsia' | 'slate' | 'indigo'
const TONE: Record<Tone, { chip: string; bar: string; text: string }> = {
  emerald: { chip: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300', bar: 'bg-emerald-400', text: 'text-emerald-300' },
  amber: { chip: 'border-amber-500/50 bg-amber-500/10 text-amber-300', bar: 'bg-amber-400', text: 'text-amber-300' },
  rose: { chip: 'border-rose-500/50 bg-rose-500/10 text-rose-300', bar: 'bg-rose-400', text: 'text-rose-300' },
  fuchsia: { chip: 'border-fuchsia-500/50 bg-fuchsia-500/10 text-fuchsia-200', bar: 'bg-fuchsia-400', text: 'text-fuchsia-200' },
  slate: { chip: 'border-slate-600/60 bg-slate-800/60 text-slate-400', bar: 'bg-slate-600', text: 'text-slate-400' },
  indigo: { chip: 'border-indigo-500/50 bg-indigo-500/10 text-indigo-200', bar: 'bg-indigo-400', text: 'text-indigo-200' },
}
const STATE: Record<string, { label: string; tone: Tone }> = {
  OPEN: { label: 'OPEN · live', tone: 'amber' }, TRADED: { label: 'TRADED · closed', tone: 'emerald' },
  SKIPPED: { label: 'SKIPPED', tone: 'rose' }, NO_FILL: { label: 'NO FILL TO MIRROR', tone: 'slate' },
  NOT_MIRRORED: { label: 'NOT MIRRORED', tone: 'slate' }, IN_TREND: { label: 'IN TREND · no fade', tone: 'slate' },
  COUNTER_NO_PLAN: { label: 'COUNTER · no plan', tone: 'fuchsia' }, NO_ROUTE: { label: 'NO ROUTE', tone: 'slate' },
  PAPER_FILLED: { label: 'FILLED · paper', tone: 'emerald' }, LIVE_FILLED: { label: 'FILLED · live', tone: 'emerald' },
  SHADOW_OK: { label: 'SHADOW', tone: 'slate' }, NO_INSTRUMENT: { label: 'NO INSTRUMENT', tone: 'slate' },
  REJECTED_BOOK: { label: 'REJECTED · book', tone: 'rose' }, NOT_SIZED: { label: 'NOT SIZED', tone: 'slate' },
  EXPOSURE: { label: 'EXPOSURE', tone: 'slate' }, WALLET_HALTED: { label: 'WALLET HALTED', tone: 'rose' },
}
const stateOf = (s: string) => STATE[s] ?? { label: s.replace(/_/g, ' '), tone: 'slate' as Tone }

const ist = (ts: number, secs = true) => {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  const t = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).formatToParts(d)
  const g = (k: string) => t.find((x) => x.type === k)?.value ?? '00'
  return secs ? `${g('hour')}:${g('minute')}:${g('second')}` : `${g('hour')}:${g('minute')}`.replace(/^(\d\d):(\d\d)$/, (_m, h, mi) => `${p(Number(h))}:${p(Number(mi))}`)
}
const f = (v: number | null | undefined, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(d))
const inr = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v < 0 ? '−' : v > 0 ? '+' : ''}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`)
const pct = (a: number, b: number) => (b ? `${(((a - b) / b) * 100).toFixed(2)}%` : '—')

function Chip({ tone, children, title }: { tone: Tone; children: React.ReactNode; title?: string }) {
  return <span title={title} className={`inline-flex items-center gap-1 rounded-full border px-2 py-[2px] font-mono text-[11px] font-semibold ${TONE[tone].chip}`}>{children}</span>
}
function KV({ k, v, tone }: { k: string; v: React.ReactNode; tone?: Tone }) {
  return (
    <div className="flex flex-col gap-[2px]">
      <span className="text-[10px] uppercase tracking-wide text-slate-500">{k}</span>
      <span className={`font-mono text-[12.5px] tabular-nums ${tone ? TONE[tone].text : 'text-slate-100'}`}>{v}</span>
    </div>
  )
}

/** The underlying since the trigger bar opened, with the stop and T1 as lines. */
function Spark({ c }: { c: Card }) {
  const w = 220, h = 56
  const pts = c.spark
  if (!pts || pts.length < 2) return <div className="flex h-14 items-center text-[11px] text-slate-600">no 1m bars in the store yet</div>
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
      <path d={d} fill="none" stroke={bull ? '#38bdf8' : '#f472b6'} strokeWidth="1.5" />
      <circle cx={X(c.ts)} cy={Y(c.entry)} r="3.5" fill="#fbbf24" stroke="#0f172a" strokeWidth="1.2" />
    </svg>
  )
}

/** Entry → rungs, with the stop/line and the current price marked. A bar, not a chart. */
function Ladder({ c }: { c: Card }) {
  const p = c.position
  if (!p) return null
  const rungs = (c.live?.ladder?.length ? c.live.ladder : p.option_targets) ?? []
  const sl = c.live ? Math.max(c.live.line, c.live.optionSl) : p.option_sl
  const now = c.live ? c.live.mid : (c.trade?.exit ?? p.exit_price ?? p.entry)
  const lo = Math.min(sl, p.entry, now) * 0.97, hi = Math.max(...rungs, p.entry, now, c.live?.peak ?? 0) * 1.03
  const X = (v: number) => `${(((v - lo) / (hi - lo || 1)) * 100).toFixed(1)}%`
  const hit = c.live?.targetsHit ?? p.targets_hit
  return (
    <div className="relative mt-1 h-12 w-full">
      <div className="absolute left-0 right-0 top-5 h-[3px] rounded bg-slate-700" />
      <div className="absolute top-5 h-[3px] rounded bg-emerald-500/60" style={{ left: X(p.entry), width: `calc(${X(now)} - ${X(p.entry)})` }} />
      <div className="absolute top-[14px] h-[15px] w-[2px] bg-rose-400" style={{ left: X(sl) }} title={`stop / line ${f(sl)}`} />
      <div className="absolute top-8 -translate-x-1/2 font-mono text-[10px] text-rose-300" style={{ left: X(sl) }}>SL {f(sl)}</div>
      <div className="absolute top-[12px] h-[19px] w-[2px] bg-amber-400" style={{ left: X(p.entry) }} title={`entry ${f(p.entry)}`} />
      <div className="absolute -top-0 -translate-x-1/2 font-mono text-[10px] text-amber-300" style={{ left: X(p.entry) }}>in {f(p.entry)}</div>
      {rungs.map((r, i) => (
        <div key={i} className="absolute" style={{ left: X(r) }}>
          <div className={`absolute top-[14px] h-[15px] w-[2px] -translate-x-1/2 ${i < hit ? 'bg-emerald-400' : 'bg-indigo-400/70'}`} title={`T${i + 1} ${f(r)}`} />
          <div className={`absolute top-8 -translate-x-1/2 font-mono text-[10px] ${i < hit ? 'text-emerald-300' : 'text-indigo-300'}`}>T{i + 1} {f(r)}</div>
        </div>
      ))}
      <div className="absolute top-[17px] h-[9px] w-[9px] -translate-x-1/2 rounded-full border-2 border-slate-900 bg-sky-300" style={{ left: X(now) }} title={`now ${f(now)}`} />
    </div>
  )
}

function Timeline({ c }: { c: Card }) {
  const rows: { ts: number; what: string; detail: string; tone: Tone }[] = [{ ts: c.ts + 1800, what: 'fired', detail: `${c.reason} · grade ${c.grade ?? '—'} rr ${f(c.rr)}`, tone: 'indigo' }]
  if (c.route) rows.push({ ts: c.route.ts ?? c.ts + 1800, what: `route ${c.route.route}`, detail: c.route.reason, tone: c.route.route === 'COUNTER' ? 'fuchsia' : 'slate' })
  if (c.skip) rows.push({ ts: c.skip.ts, what: 'skipped', detail: c.skip.reason, tone: 'rose' })
  if (c.position) rows.push({ ts: c.position.opened_ts, what: 'filled', detail: `${c.position.qty} × @ ${f(c.position.entry)} · ${c.position.instrument.name}`, tone: 'amber' })
  for (const o of c.exits) rows.push({ ts: o.ts, what: 'exit', detail: `${o.filled ?? o.qty} @ ${f(o.avg_price)} — ${o.reason}`, tone: 'emerald' })
  for (const e of c.operator) rows.push({ ts: e.ts, what: e.kind.replace('operator.', 'operator '), detail: 'override, audited', tone: 'amber' })
  if (c.trade) rows.push({ ts: c.position?.closed_ts ?? c.ts, what: 'closed', detail: `${c.trade.exit_reason} · net ${inr(c.trade.net)} · MFE ${f(c.trade.mfe_r)}R MAE ${f(c.trade.mae_r)}R`, tone: c.trade.net >= 0 ? 'emerald' : 'rose' })
  rows.sort((a, b) => a.ts - b.ts)
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r, i) => (
        <div key={i} className="flex items-start gap-3 text-[12px]">
          <span className={`w-[72px] flex-none font-mono ${TONE[r.tone].text}`}>{ist(r.ts)}</span>
          <span className={`w-[110px] flex-none font-semibold ${TONE[r.tone].text}`}>{r.what}</span>
          <span className="text-slate-300">{r.detail}</span>
        </div>
      ))}
    </div>
  )
}

function Reads({ r }: { r: Route | null }) {
  if (!r?.reads?.length) return <div className="text-[12px] text-slate-500">{r ? r.reason : 'no route recorded (before 23-Sep evening, or MCX)'}</div>
  return (
    <table className="w-full border-collapse text-[11.5px]">
      <thead><tr className="text-[10px] uppercase tracking-wide text-slate-500">{['leg', 'nearest pivot', 'dist · crossed', 'volume T / T−1', 'score'].map((h) => <th key={h} className="border-b border-slate-800 px-2 py-1 text-left font-semibold">{h}</th>)}</tr></thead>
      <tbody>
        {r.reads.map((x) => (
          <tr key={x.leg} className="font-mono tabular-nums">
            <td className="px-2 py-1 text-slate-300">{x.leg}</td>
            <td className="px-2 py-1 text-slate-200">{x.members.join(',')} {f(x.level)} <span className="text-slate-500">({f(x.strength, 1)})</span></td>
            <td className="px-2 py-1 text-slate-200">{f(x.distAtr)} · {x.crossedAtr >= 0 ? '+' : ''}{f(x.crossedAtr)} ATR{x.rejected ? ' · rejected' : ''}</td>
            <td className={`px-2 py-1 ${x.volume === 'dried' ? 'text-rose-300' : x.volume === 'surge' ? 'text-emerald-300' : 'text-slate-300'}`}>{x.volume} {x.surgeT !== null ? `${f(x.surgeT)}/${f(x.surgeT1)}` : ''}</td>
            <td className={`px-2 py-1 ${x.score >= 0.5 ? 'text-fuchsia-200' : 'text-slate-300'}`}>{f(x.score)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Actions({ book, c, onDone }: { book: string; c: Card; onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const canSkip = c.state === 'OPEN'
  const canTake = !['OPEN', 'TRADED', 'PAPER_FILLED', 'LIVE_FILLED'].includes(c.state)
  const act = async (kind: 'take' | 'skip') => {
    const verb = kind === 'take' ? `Enter ${BOOK_LABEL[book] ?? book} on ${c.symbol} now, at the market?` : `Close ${BOOK_LABEL[book] ?? book}'s ${c.symbol} position now, at the market?`
    if (!window.confirm(verb)) return
    setBusy(kind); setMsg(null)
    try {
      const r = await postJson<Record<string, unknown>>(`/api/books/${book}/${kind}`, { signal_id: c.signalId })
      setMsg(kind === 'take' ? (r.entered ? 'entered' : 'not entered — see the ledger decision') : 'closing')
      onDone()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally { setBusy(null) }
  }
  return (
    <div className="flex items-center gap-2">
      <button disabled={!canTake || busy !== null} onClick={() => act('take')} className="rounded border border-emerald-500/50 bg-emerald-500/10 px-3 py-1 text-[12px] font-semibold text-emerald-300 disabled:opacity-30">Take</button>
      <button disabled={!canSkip || busy !== null} onClick={() => act('skip')} className="rounded border border-rose-500/50 bg-rose-500/10 px-3 py-1 text-[12px] font-semibold text-rose-300 disabled:opacity-30">Skip</button>
      {msg && <span className="text-[11px] text-slate-400">{msg}</span>}
    </div>
  )
}

function TriggerCard({ c, open, onToggle }: { c: Card; open: boolean; onToggle: () => void }) {
  const st = stateOf(c.state)
  const bull = c.direction === 'BULLISH'
  const conf = c.confluence ?? {}
  const eq = c.route?.reads?.find((r) => r.leg === 'equity'), fut = c.route?.reads?.find((r) => r.leg === 'future')
  const volTone = (v?: string): Tone => (v === 'dried' ? 'rose' : v === 'surge' ? 'emerald' : 'slate')
  const surgeTxt = (t: number | null | undefined, t1: number | null | undefined) => (t === null || t === undefined ? '—' : `${f(t)}× / ${f(t1)}×`)
  const outcome = (() => {
    if (c.live) return <>Open · mid <b>{f(c.live.mid)}</b> ({pct(c.live.mid, c.position!.entry)}) · peak {f(c.live.peak)} · line {f(Math.max(c.live.line, c.live.optionSl))} · {c.live.armedBy ? `armed by ${c.live.armedBy}` : 'not armed'} · {c.live.qtyRemaining}/{c.live.qty} left · <b className={c.live.unrealised !== null && c.live.unrealised >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{inr((c.live.unrealised ?? 0) + c.live.realised)}</b></>
    if (c.trade) return <>{c.exits.map((o, i) => <span key={i}>{ist(o.ts)} {o.filled ?? o.qty} @ {f(o.avg_price)} <span className="text-slate-500">{o.reason.split(' — ')[0].slice(0, 60)}</span>{i < c.exits.length - 1 ? ' → ' : ''}</span>)} · <b className={c.trade.net >= 0 ? 'text-emerald-300' : 'text-rose-300'}>{inr(c.trade.net)}</b></>
    if (c.skip) return <>Declined at {ist(c.skip.ts)}: {c.skip.reason}</>
    if (c.state === 'NO_FILL') return <>Nothing to mirror — the parent: {c.parentDecision} · {c.parentReason}</>
    if (c.state === 'IN_TREND') return <>Routed IN_TREND — the fade books stay flat. {c.route?.summary}</>
    if (c.fade && !c.position) return <>Fade {c.fade.direction} planned (stop {f(c.fade.stop)}, T1 {f(c.fade.targets?.[0])}) — {c.fade.decision ?? 'pending'} {c.fade.decision_reason ?? ''}</>
    return <>{c.parentDecision} · {c.parentReason}</>
  })()
  return (
    <article className={`relative overflow-hidden rounded-2xl border bg-slate-900/60 px-5 pb-4 pt-4 ${open ? 'border-sky-500/50' : 'border-slate-800'}`}>
      <div className={`absolute bottom-0 left-0 top-0 w-[5px] ${TONE[st.tone].bar}`} />
      <button onClick={onToggle} className="block w-full text-left" aria-expanded={open}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-1">
            <div className="flex items-baseline gap-2.5">
              <span className="text-[21px] font-bold tracking-tight text-slate-100">{c.symbol}</span>
              <span className="font-mono text-[12.5px] text-slate-400">{c.position?.instrument.name ?? (c.fade ? `fade · ${c.fade.direction}` : `${c.direction === 'BULLISH' ? 'CE' : 'PE'} side`)}</span>
              <Chip tone={bull ? 'emerald' : 'rose'}>{bull ? '▲ BULL' : '▼ BEAR'}</Chip>
            </div>
            <div className="font-mono text-[11.5px] text-slate-400">trigger {ist(c.ts, false)}–{ist(c.ts + 1800, false)} · fired {ist(c.ts + 1800)}{c.position ? ` · filled ${ist(c.position.opened_ts)} (+${(c.position.opened_ts - c.ts - 1800).toFixed(1)} s)` : ''}</div>
          </div>
          <div className="flex flex-col items-end gap-1.5">
            <Chip tone={st.tone}>{st.label}</Chip>
            <span className="font-mono text-[11.5px] text-slate-400">grade <b className="text-slate-100">{c.grade ?? '—'}</b> · RR <b className="text-slate-100">{f(c.rr)}</b>{c.rtCard?.confidence ? <> · conf <b className="text-slate-100">{c.rtCard.confidence.score.toFixed(0)}</b></> : null}{c.rtCard?.odds?.pT1 !== null && c.rtCard?.odds?.pT1 !== undefined ? <> · P(T1) <b className="text-slate-100">{c.rtCard.odds.pT1.toFixed(0)}%</b></> : null}</span>
          </div>
        </div>
      </button>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {c.route ? <Chip tone={c.route.route === 'COUNTER' ? 'fuchsia' : 'indigo'} title={c.route.reason}>{c.route.route} · {c.route.summary ?? ''}</Chip> : <Chip tone="slate">no route</Chip>}
        <Chip tone={volTone(eq?.volume ?? (c.surgeT !== null && c.surgeT !== undefined ? (c.surgeT >= 2.5 ? 'surge' : 'average') : undefined))} title="trigger bar volume ÷ mean of T−2…T−7">EQ vol {surgeTxt(eq?.surgeT ?? c.surgeT, eq?.surgeT1 ?? c.surgeT1)}</Chip>
        {fut && <Chip tone={volTone(fut.volume)}>FUT vol {surgeTxt(fut.surgeT, fut.surgeT1)}</Chip>}
        {c.stopPct > 0 && c.stopPct < 0.2 && <Chip tone="rose">stop {c.stopPct.toFixed(2)}% — inside noise</Chip>}
        {c.operator.length > 0 && <Chip tone="amber">operator {c.operator.map((o) => o.kind.replace('operator.', '')).join(', ')}</Chip>}
      </div>
      <div className="mt-3 border-t border-slate-800 pt-2.5">
        <div className="mb-1.5 text-[10px] uppercase tracking-wide text-slate-500">trigger candle</div>
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <KV k="30m" v={c.candle ? `${f(c.candle.o)} → ${f(c.candle.c)} · H ${f(c.candle.h)} · L ${f(c.candle.l)}` : `close ${f(c.entry)}`} />
          {c.candle && c.evidence?.atr ? <KV k="range" v={`${f((c.candle.h - c.candle.l) / c.evidence.atr, 1)} ATR · ${pct(c.candle.c, c.candle.o)}`} /> : null}
          {c.evidence?.bb_upper !== undefined && <KV k={bull ? 'vs BB upper' : 'vs BB lower'} v={f(bull ? c.entry - c.evidence.bb_upper : c.evidence.bb_lower - c.entry)} tone={bull ? 'emerald' : 'rose'} />}
          <KV k="ST" v={`flip · ${c.evidence?.bars_in_trend ?? '—'} bar`} />
        </div>
      </div>
      <div className="mt-2.5 border-t border-slate-800 pt-2.5">
        <div className="mb-1.5 text-[10px] uppercase tracking-wide text-slate-500">levels · official</div>
        <div className="flex flex-wrap gap-x-5 gap-y-2">
          <KV k="stop" v={`${f(c.stop)} · ${conf.stop_zone || '1 ATR fallback'} (${pct(c.stop, c.entry)})`} tone="rose" />
          <KV k="T1" v={`${f(c.targets?.[0])} · ${conf.target_zones?.[0] ?? '—'} (${c.targets?.[0] ? pct(c.targets[0], c.entry) : '—'})`} tone="emerald" />
          <KV k="fortress / room" v={`${f(conf.fortress, 1)} / ${f(conf.room_ratio, 1)} ATR`} />
          {c.route?.wall?.members?.length ? <KV k="wall ahead" v={`${c.route.wall.grade} ${f(c.route.wall.strength, 1)} (${c.route.wall.timeframes}) ${f(c.route.wall.distAtr)} ATR`} tone="fuchsia" /> : null}
        </div>
      </div>
      {c.position && (
        <div className="mt-2.5 border-t border-slate-800 pt-2.5">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">option · {c.position.instrument.name} · {c.position.qty / c.position.instrument.lot_size} lots @ {f(c.position.entry)}</div>
          <Ladder c={c} />
        </div>
      )}
      <div className="mt-3 flex items-stretch gap-4 border-t border-slate-800 pt-2.5">
        <div className="flex flex-1 flex-col gap-1"><span className="text-[10px] uppercase tracking-wide text-slate-500">outcome</span><div className="text-[12.5px] leading-relaxed text-slate-200">{outcome}</div></div>
        <div className="flex w-[220px] flex-none flex-col gap-1"><span className="text-[10px] uppercase tracking-wide text-slate-500">underlying since the trigger</span><Spark c={c} /></div>
      </div>
      <div className="mt-2.5 flex items-center justify-between">
        <div className="text-[12px] text-slate-400"><span className="text-emerald-300">{c.pros.length} for</span> · <span className="text-rose-300">{c.cons.length} against</span>{c.cons[0] ? <span className="text-slate-500"> — {c.cons[0]}</span> : c.pros[0] ? <span className="text-slate-500"> — {c.pros[0]}</span> : null}</div>
        <button onClick={onToggle} className="text-[12px] font-semibold text-sky-300">{open ? 'Close ↑' : 'Open details →'}</button>
      </div>
    </article>
  )
}

function Expanded({ book, c, refresh }: { book: string; c: Card; refresh: () => void }) {
  return (
    <div className="col-span-2 rounded-2xl border border-sky-500/40 bg-slate-950/70 p-5">
      <div className="grid grid-cols-3 gap-5">
        <div className="flex flex-col gap-3">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">decision · both legs at the trigger close</div>
          <Reads r={c.route} />
          <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-[12.5px] leading-relaxed">
            <div><b className="text-emerald-300">For:</b> {c.pros.length ? c.pros.join(' · ') : '—'}</div>
            <div className="mt-1"><b className="text-rose-300">Against:</b> {c.cons.length ? c.cons.join(' · ') : '—'}</div>
            {c.route && <div className="mt-1 text-slate-400">{c.route.reason}</div>}
          </div>
        </div>
        <div className="flex flex-col gap-3">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">levels, walls, option stop</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2">
            <KV k="entry (underlying)" v={f(c.entry)} />
            <KV k="stop" v={`${f(c.stop)} · ${c.confluence.stop_zone ?? ''}`} tone="rose" />
            {(c.targets ?? []).map((t, i) => <KV key={i} k={`T${i + 1}`} v={`${f(t)} · ${c.confluence.target_zones?.[i] ?? ''} (${pct(t, c.entry)})`} tone="emerald" />)}
            {c.rtCard?.wallBehind && <KV k="wall behind" v={`${f(c.rtCard.wallBehind.price)} · ${f(c.rtCard.wallBehind.strength, 1)} · ${c.rtCard.wallBehind.members.join(',')}`} />}
            {c.rtCard?.wallAhead && <KV k="wall ahead" v={`${f(c.rtCard.wallAhead.price)} · ${f(c.rtCard.wallAhead.strength, 1)} · ${c.rtCard.wallAhead.members.join(',')}`} />}
            {c.rtCard?.stop && <KV k="option stop (δ)" v={`${f(c.rtCard.stop.optionStop)} · δ ${f(c.rtCard.stop.delta)} · equity ${f(c.rtCard.stop.equityStop)}`} tone="rose" />}
            {c.live && <KV k="expected daily move" v={`${(c.live.edm * 100).toFixed(0)}% of premium`} tone="amber" />}
            {c.fade && <KV k="fade plan (CT)" v={`${c.fade.direction} · stop ${f(c.fade.stop)} · T1 ${f(c.fade.targets?.[0])} · grade ${c.fade.grade} rr ${f(c.fade.rr)}`} tone="fuchsia" />}
            {c.evidence?.volume !== undefined && <KV k="trigger volume" v={`${c.evidence.volume.toLocaleString('en-IN')} vs baseline ${c.baseline ? Math.round(c.baseline).toLocaleString('en-IN') : '—'}`} />}
          </div>
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between"><span className="text-[10px] uppercase tracking-wide text-slate-500">this book · {BOOK_LABEL[book] ?? book}</span><Actions book={book} c={c} onDone={refresh} /></div>
          {c.position ? (
            <div className="grid grid-cols-2 gap-x-4 gap-y-2">
              <KV k="contract" v={c.position.instrument.name} />
              <KV k="fill" v={`${c.position.qty} @ ${f(c.position.entry)} · ${ist(c.position.opened_ts)}`} tone="amber" />
              {c.live ? (<>
                <KV k="mid / peak" v={`${f(c.live.mid)} / ${f(c.live.peak)}`} />
                <KV k="line / option SL" v={`${f(c.live.line)} / ${f(c.live.optionSl)}`} tone="rose" />
                <KV k="armed" v={c.live.armedTs ? `${c.live.armedBy} · ${ist(c.live.armedTs)}` : 'not yet'} tone={c.live.armedTs ? 'emerald' : 'slate'} />
                <KV k="lots" v={`${c.live.qtyRemaining} of ${c.live.qty} left · ${c.live.targetsHit} rung(s) taken`} />
                <KV k="P&L" v={`${inr(c.live.unrealised)} open · ${inr(c.live.realised)} booked`} tone={(c.live.unrealised ?? 0) + c.live.realised >= 0 ? 'emerald' : 'rose'} />
                <KV k="underlying now" v={f(c.live.underlying)} />
              </>) : c.trade ? (<>
                <KV k="exit" v={`${f(c.trade.exit)} · ${c.trade.exit_reason}`} />
                <KV k="net / gross" v={`${inr(c.trade.net)} / ${inr(c.trade.gross)}`} tone={c.trade.net >= 0 ? 'emerald' : 'rose'} />
                <KV k="MFE / MAE" v={`${f(c.trade.mfe_r)}R / ${f(c.trade.mae_r)}R`} />
                <KV k="held" v={`${Math.round(c.trade.duration_s / 60)} min`} />
              </>) : null}
              <div className="col-span-2 text-[11px] text-slate-500">{c.position.note}</div>
            </div>
          ) : (
            <div className="text-[12.5px] text-slate-400">{stateOf(c.state).label} — {c.skip ? c.skip.reason : c.parentReason ?? c.route?.summary ?? ''}</div>
          )}
          <div className="text-[10px] uppercase tracking-wide text-slate-500">timeline</div>
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
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-indigo-300">book · {data.day}</div>
          <div className="text-2xl font-bold tracking-tight text-slate-100">{BOOK_LABEL[book] ?? book}</div>
          <div className="mt-1 max-w-2xl text-[12px] text-slate-500">One card per FUDKII trigger. Every trigger is scored the same way whether this book traded it or not, so a skip reads as “skipped, because”. Take / Skip are operator overrides and are written to the event log.</div>
        </div>
        <div className="flex gap-4">
          <KV k="wallet" v={w ? `₹${w.balance.toLocaleString('en-IN', { maximumFractionDigits: 0 })}` : '—'} />
          <KV k="day P&L" v={inr(dayPnl)} tone={dayPnl >= 0 ? 'emerald' : 'rose'} />
          <KV k="trades" v={String(w?.trades ?? 0)} />
          <KV k="triggers" v={String(data.cards.length)} />
        </div>
      </div>
      <div className="mb-4 flex flex-wrap gap-1.5">
        <button onClick={() => setFilter('ALL')} className={`rounded-full border px-2.5 py-[3px] font-mono text-[11px] ${filter === 'ALL' ? 'border-slate-500 bg-slate-800 text-white' : 'border-slate-700 text-slate-400'}`}>All · {data.cards.length}</button>
        {Object.entries(data.counts).sort().map(([s, n]) => {
          const st = stateOf(s)
          return <button key={s} onClick={() => setFilter(filter === s ? 'ALL' : s)} className={`rounded-full border px-2.5 py-[3px] font-mono text-[11px] ${filter === s ? 'border-slate-400 bg-slate-800 text-white' : TONE[st.tone].chip}`}>{st.label} · {n}</button>
        })}
      </div>
      {cards.length === 0 ? (
        <div className="rounded border border-slate-700/40 bg-slate-900/40 p-4 text-sm text-slate-500">No FUDKII trigger today yet. The book decides on each closed 30m bar.</div>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {pairs.map((pair, i) => (
            <FragmentRow key={i} pair={pair} book={book} open={open} setOpen={setOpen} refresh={refresh} />
          ))}
        </div>
      )}
    </div>
  )
}

function FragmentRow({ pair, book, open, setOpen, refresh }: { pair: Card[]; book: string; open: string | null; setOpen: (s: string | null) => void; refresh: () => void }) {
  const opened = pair.find((c) => c.signalId === open)
  return (
    <>
      {pair.map((c) => <TriggerCard key={c.signalId} c={c} open={open === c.signalId} onToggle={() => setOpen(open === c.signalId ? null : c.signalId)} />)}
      {pair.length === 1 && <div />}
      {opened && <Expanded book={book} c={opened} refresh={() => void refresh()} />}
    </>
  )
}
