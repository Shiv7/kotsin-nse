import { useMemo, useState } from 'react'
import { CandleChart, type ChartBar, type Marker, type PriceLine } from '../components/CandleChart'
import { Chip, Funnel, Group, toggle } from '../components/LiveTradeCards'
import { RULE_LABEL } from '../components/StopRuleGrid'
import { Badge, Card, ErrorLine, Stat, StrategyBadge } from '../components/Ui'
import { cls, contractName, fmt, pnlColor } from '../lib/api'
import { useEngine } from '../lib/engine'
import {
  BLANK, IST_S, QUICK, activeCount, bestBetter, chipOptions, hm, lessonLevel, loadFilters, maxTo, passes, quickOf, resultOf, saveFilters,
  type Filters, type Side,
} from '../lib/reviewFilters'
import { usePoll } from '../lib/usePoll'
import type { StopRule } from '../types'

// Trade review (operator, 2026-10-05: "see all detailed view of each trade and analytics on why it incurred a loss.
// did we have to wait before entering, was a pivot around, was it a counter-trend? no volume? no oi? etc and be
// accurate in identifying the root cause"). Every line comes from the record (api/review.py); a question the record
// cannot answer is shown as unknown, never guessed.
//
// Operator, 2026-10-09: "add strategy and other filters ... also add there insights/pnl from another engine or/and strategy
// that traded the same OTM at that time and if it made better profit than that startegy on the card. mention its
// features/parameters that led to better outcomes ... focus is on choosing the right direction of trade". So: a filter
// (the Options page's pattern), the same option elsewhere on every card (api/review_peers.py: exact rupee parts that add up
// to the gap, the lesson graded by the pooled record) and the direction block (api/review_direction.py). Read-only.
//
// 10 Oct review: the filters live in lib/reviewFilters.ts (tested by node: tests/reviewFilters.test.ts); a past range is
// read once, not every 20 s, and the twins only when they are shown; a card says when the twin was not read, when its own
// result rests on a refused paper exit, and how sure the lesson behind its "better" chip is.

type Status = 'ok' | 'warn' | 'bad' | 'unknown'
type Check = { key: string; question: string; status: Status; detail: string }
type Mirror = { rule: 'E' | 'A'; book: string; status: string; exitReason: string | null; exitPrice: number | null; net: number | null }
type Stance = 'with' | 'against' | 'neutral' | 'unknown'
type Read = { key: string; label: string; stance: Stance; detail: string }
type SkipVerdict = { name: string | null; source: string | null; skipped: boolean; rules: { id: string; label: string; fired: boolean | null }[] }
type Direction = {
  trigger: { direction: string; closeTs: number | null; close: number | null; atr: number | null }
  trade: 'BULLISH' | 'BEARISH'; fade: boolean; reads: Read[]; with: number; against: number; againstKeys: string[]
  label: { first: 'up' | 'down' | 'none' | 'ambiguous' | null; ts: number | null; minutes: number | null; how: string; level: number
    triggerRight: boolean | null; tradeRight: boolean | null; text: string; basis?: string }
  skip: SkipVerdict | null
}
type PeerRow = {
  engine: string; positionId: string; tradeId: string | null; book: string; label: string; baseBook: string; variant: StopRule; cls: string
  status: string; openedTs: number; closedTs: number | null; entry: number; exitAvg: number | null; finalExit: number | null
  lots: number; qty: number; net: number | null; exitReason: string | null; mech: string | null; refused: number; sameEngine: boolean
}
type PartLine = { key: string; label: string; inr: number; rupees: number; text: string; major: boolean }
type ExitSide = {
  who: string; mech: string; reason: string | null; ts: number | null; decision: number | null; fill: number | null; avgExit: number | null
  level: number | null; triggerOn: string | null; triggerPx: number | null; optionSlInitial: number | null; optionSlFinal: number | null
  armedBy: string | null; armedMin: number | null; peakMid: number | null; trailDwell: number | null; equitySl: number | null
  rungs: { price: number; qty: number; ts: number }[]; rungShare: number; heldMin: number; audit: boolean
  finalLegs?: { reason: string | null; qty: number; fill: number; ref: number; ts: number }[]
  refused: { ts: number; kind: string; executable: number | null; ageMs: number | null; limitMs: number | null; text: string } | null
}
type Evidence = { what: string; text: string; strength: string; grade: string; n: number; params?: ParamDiff[] }
type ParamDiff = { key: string; label: string; this: string; peer: string }
type Lesson = {
  key: string; grade: string | null; gradeWhy: string; strength: string; text: string; params: ParamDiff[]
  decided: Evidence | null; systematic: Evidence | null; also?: Evidence | null
}
type BetterGroup = {
  peer: PeerRow; members: PeerRow[]; gap: number; gapRupees: number; rupees: Record<string, number>; residual: number; lines: PartLine[]
  relation: string; chainVia: PeerRow | null; note: string; meaningful: boolean; exits: { this: ExitSide; peer: ExitSide; thisIsChain: boolean }
  paramDiffs: ParamDiff[]; lesson: Lesson
}
type SelfRefusal = { effect: number; netIfFilled: number; artefact: boolean; kind: string; ts: number; executable: number | null; text: string }
type Peers = {
  contract: string; scripCode?: string; rows: PeerRow[]; n: number; better: BetterGroup[]; betterExists: boolean; betterCount: number; class: string
  headline: string; outcome: string | null; pooledTo: string | null; delta?: number
  // 10 Oct: "" when every engine was read, else what the comparison covers; this trade's own refused exit
  scope?: string; selfRefusal?: SelfRefusal | null
}
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
  // 9 Oct 2026: absent from an engine that runs the older review (its cards then show without them)
  mirror?: boolean; variant?: StopRule; sourceBook?: string; sourceLabel?: string; shadow?: boolean; sourceShadow?: boolean; triggerDirection?: string | null
  engine?: string; day?: string; directionRead?: Direction; peers?: Peers | null
}
type Day = {
  day: string; to?: string; trades: Trade[]; thresholds: Record<string, number>; engine?: string; notes?: string[]
  twin?: { name: string | null; read: boolean; note: string } | null; skipRules?: boolean
  summary: { trades: number; closed: number; open: number; losses: number; wins: number; net: number; causes: { key: string; label: string; trades: number; net: number }[] }
}
type Row = Trade & { origin: Side; engineName: string }
const ICON: Record<Status, string> = { ok: '✓', warn: '⚠', bad: '✗', unknown: '?' }
const TONE: Record<Status, string> = { ok: 'text-emerald-400', warn: 'text-amber-300', bad: 'text-rose-400', unknown: 'text-slate-500' }
const STANCE_TONE: Record<Stance, string> = { with: 'border-emerald-700/70 bg-emerald-950/30 text-emerald-200', against: 'border-rose-700/70 bg-rose-950/40 text-rose-200', neutral: 'border-slate-700 bg-slate-900 text-slate-300', unknown: 'border-slate-800 bg-slate-950 text-slate-500' }
const STANCE_ICON: Record<Stance, string> = { with: '✓', against: '✗', neutral: '·', unknown: '?' }
const hms = (ts: number | null | undefined) => (ts ? new Date((ts + IST_S) * 1000).toISOString().slice(11, 19) : '—')
const dur = (a: number | null, b: number | null) => (a && b ? fmt.dur(b - a) : '—')
const todayIst = () => new Date(Date.now() + IST_S * 1000).toISOString().slice(0, 10)
/** A new first day keeps the range's end while it is still a range of at most 10 sessions from it. */
const dayChange = (days: { day: string; to: string }, day: string) => ({ day, to: days.to && (days.to < day || days.to > maxTo(day, todayIst())) ? '' : days.to })
const px = (v: number | null | undefined) => (v == null ? '—' : Math.abs(v * 100 - Math.round(v * 100)) > 1e-6 ? v.toFixed(3) : v.toFixed(2))
const lotsTxt = (n: number) => `${Number.isInteger(n) ? n : n.toFixed(2)} lot${n === 1 ? '' : 's'}`
const GRADE_TONE = (g: string | null | undefined): 'green' | 'amber' | 'slate' => (g?.startsWith('systematic') ? 'green' : g === 'suggestive' ? 'amber' : 'slate')
/** A net to the paisa, signed: the line that shows how a gap is made adds up exactly, where whole rupees can be ₹1 off. */
const inrP = (v: number | null | undefined) =>
  v == null ? 'DM' : `${v >= 0 ? '+' : '−'}₹${Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

// -- the filters (lib/reviewFilters.ts) -----------------------------------------------------------------------------

function FilterPanel({ f, set, opts, engine, days, setDays }: {
  f: Filters; set: (f: Filters) => void; engine: ReturnType<typeof useEngine>
  opts: { books: [string, string][]; reasons: string[]; grades: string[]; causes: [string, string][] }
  days: { day: string; to: string }; setDays: (d: { day: string; to: string }) => void
}) {
  const many = (key: 'result' | 'trigger' | 'rightDir' | 'exitReasons' | 'grades' | 'causes', items: [string, string][]) =>
    items.map(([v, label]) => <Chip key={v} on={f[key].includes(v)} onClick={() => set({ ...f, [key]: toggle(f[key], v) })}>{label}</Chip>)
  return (
    <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-900/70 p-3">
      {engine?.peer && (
        <Group label="Engines">
          {([['self', engine.name || 'this engine'], ['peer', engine.peer.name || 'twin'], ['both', 'both']] as const).map(([v, label]) => (
            <Chip key={v} on={f.engines === v} onClick={() => set({ ...f, engines: v })}>{label}</Chip>
          ))}
        </Group>
      )}
      <Group label="Strategies">
        {opts.books.map(([b, label]) => <Chip key={b} on={f.books.includes(b)} onClick={() => set({ ...f, books: toggle(f.books, b) })}>{label}</Chip>)}
        {!opts.books.length && <span className="text-[12px] text-slate-600">none on these days</span>}
      </Group>
      <Group label="Variants">
        {(['current', 'E', 'A'] as StopRule[]).map((v) => <Chip key={v} on={f.variants.includes(v)} onClick={() => set({ ...f, variants: toggle(f.variants, v) })}>{RULE_LABEL[v]}</Chip>)}
      </Group>
      <Group label="Twins · shadows">
        <Chip on={f.mirrors} onClick={() => set({ ...f, mirrors: !f.mirrors })} title="the twins: each book's stop-rule mirrors, the same fill under stop E and the adaptive stop, as cards of their own">{f.mirrors ? 'twins shown' : 'twins hidden'}</Chip>
        <Chip on={f.shadows} onClick={() => set({ ...f, shadows: !f.shadows })} title="the shadow books: the wide stop, graded F, CT-M, CT-Y-F">{f.shadows ? 'shadows shown' : 'shadows hidden'}</Chip>
      </Group>
      <Group label="Result">{many('result', [['win', 'win'], ['loss', 'loss'], ['open', 'open']])}</Group>
      <Group label="Trigger">{many('trigger', [['BULLISH', '▲ bullish'], ['BEARISH', '▼ bearish']])}</Group>
      <Group label="Direction was">{many('rightDir', [['right', 'right for the trade'], ['wrong', 'wrong for the trade'], ['neither', 'neither / not measured']])}</Group>
      <Group label="Exit reason">{many('exitReasons', opts.reasons.map((r) => [r, r]))}</Group>
      <Group label="Grade">{many('grades', opts.grades.map((g) => [g, g === '—' ? 'none' : g]))}</Group>
      <Group label="Root cause">{many('causes', opts.causes)}</Group>
      <Group label="Contract">
        <input value={f.search} onChange={(e) => set({ ...f, search: e.target.value })} placeholder="LTF, CE 270 …" className="w-56 rounded border border-slate-700 bg-slate-950 px-2 py-0.5 text-[12px] text-slate-200" />
      </Group>
      <Group label="Entry time">
        <input type="time" value={f.from} onChange={(e) => set({ ...f, from: e.target.value })} className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[12px] text-slate-200" />
        <span className="text-[12px] text-slate-500">to</span>
        <input type="time" value={f.to} onChange={(e) => set({ ...f, to: e.target.value })} className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[12px] text-slate-200" />
        <span className="text-[11px] text-slate-600">IST</span>
      </Group>
      <Group label="Days">
        <input type="date" value={days.day} max={todayIst()} onChange={(e) => e.target.value && setDays(dayChange(days, e.target.value))} className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[12px] text-slate-200" />
        <span className="text-[12px] text-slate-500">to</span>
        <input type="date" value={days.to} min={days.day} max={maxTo(days.day, todayIst())} onChange={(e) => setDays({ ...days, to: e.target.value && e.target.value > maxTo(days.day, todayIst()) ? maxTo(days.day, todayIst()) : e.target.value })} className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[12px] text-slate-200" />
        <span className="text-[11px] text-slate-600">a range of up to 10 sessions; blank = the one day</span>
      </Group>
      <Group label="Same option">
        {([['any', 'a better peer exists', 'another book or engine made meaningfully more (₹100 or 0.5 % of the premium) on the same contract while this trade was held'],
          ['lesson', '… with a lesson', 'a better peer whose difference is not a stale-book refusal or a halt (those are paper artefacts or operator actions, never a lesson)'],
          ['systematic', '… a systematic lesson', 'a better peer whose lesson held across both windows and both engines (lessons.md L1)']] as const).map(([v, label, title]) => (
          <Chip key={v} on={f.better === v} onClick={() => set({ ...f, better: f.better === v ? '' : v })} title={title}>{label}</Chip>
        ))}
        <Chip on={!f.artefacts} onClick={() => set({ ...f, artefacts: !f.artefacts })} title="a card whose own result rests on an exit the paper matcher refused on a stale book (modelled at the price the engine recorded then)">
          {f.artefacts ? 'paper artefacts shown' : 'paper artefacts hidden'}
        </Chip>
      </Group>
      <div className="pt-1">
        <button type="button" onClick={() => { set(BLANK); setDays({ day: days.day, to: '' }) }} className="text-[12px] text-slate-400 underline decoration-dotted hover:text-white">show everything</button>
      </div>
    </div>
  )
}

// -- the card's parts ------------------------------------------------------------------------------------------------

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

function DirectionBadge({ d }: { d: Direction | undefined }) {
  if (!d) return null
  const r = d.label.tradeRight
  if (r == null) return <Badge tone="slate">direction {d.label.first === 'none' ? 'went nowhere' : d.label.first === 'ambiguous' ? 'ambiguous' : 'not measured'}</Badge>
  return <Badge tone={r ? 'green' : 'red'}>{r ? 'right direction' : 'wrong direction'}</Badge>
}

/** Was the direction right, should the trigger have been skipped: the reads at the decision and the outcome label. */
function DirectionBox({ d }: { d: Direction }) {
  const r = d.label.tradeRight
  const tone = r == null ? 'border-slate-700 bg-slate-900/50' : r ? 'border-emerald-800 bg-emerald-950/20' : 'border-rose-800 bg-rose-950/25'
  return (
    <div className={cls('rounded border px-3 py-2', tone)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-slate-400">direction &amp; skip</span>
        <DirectionBadge d={d} />
        <span className="text-[12px] text-slate-400">
          trigger {d.trigger.direction === 'BULLISH' ? '▲' : '▼'} {d.trigger.direction?.toLowerCase()} · this trade {d.trade === 'BULLISH' ? '▲' : '▼'} {d.trade.toLowerCase()}{d.fade ? ' (it fades the trigger)' : ''}
        </span>
        <span className="ml-auto text-[12px] text-slate-400">
          at the decision: <b className="text-emerald-300">{d.with} with</b> · <b className="text-rose-300">{d.against} against</b>
        </span>
      </div>
      <div className="mt-1 text-[13px] text-slate-200">{d.label.text}</div>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {d.reads.map((x) => (
          <span key={x.key} title={x.detail} className={cls('rounded border px-2 py-0.5 text-[11.5px]', STANCE_TONE[x.stance])}>
            {STANCE_ICON[x.stance]} {x.label}
          </span>
        ))}
      </div>
      <ul className="mt-1.5 space-y-0.5 text-[12px] text-slate-400">
        {d.reads.filter((x) => x.stance === 'against' || x.stance === 'with').map((x) => (
          <li key={x.key}><span className={x.stance === 'against' ? 'text-rose-300' : 'text-emerald-300'}>{x.label}:</span> {x.detail}</li>
        ))}
      </ul>
      {/* the candidate skip rules: shown only once the research has written its rules file (api/review_direction.py candidate_skip) */}
      {d.skip && (
        <div className={cls('mt-2 rounded border px-2 py-1 text-[12px]', d.skip.skipped ? 'border-amber-700 bg-amber-950/30 text-amber-200' : 'border-slate-700 text-slate-300')}>
          <b>{d.skip.skipped ? 'The candidate skip rules would have skipped it' : 'The candidate skip rules would have taken it'}</b>
          {d.skip.name ? ` (${d.skip.name})` : ''}:{' '}
          {d.skip.rules.map((x) => `${x.id} ${x.fired === true ? 'fired' : x.fired === false ? 'no' : 'unknown'}`).join(' · ')}
        </div>
      )}
    </div>
  )
}

function ExitColumn({ e, title }: { e: ExitSide; title: string }) {
  const row = (k: string, v: React.ReactNode) => (
    <div className="flex justify-between gap-3 border-b border-slate-900 py-0.5">
      <span className="text-slate-500">{k}</span>
      <span className="text-right text-slate-300">{v}</span>
    </div>
  )
  return (
    <div className="min-w-0 text-[12px]">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{title}</div>
      {row('rule that fired', `${e.mech} at ${hms(e.ts)}`)}
      {row('decision · fill', `${px(e.decision)} · ${px(e.fill)}`)}
      {row('average exit', px(e.avgExit))}
      {row('stop level · on', e.level == null ? '—' : `${px(e.level)} · ${e.triggerOn ?? '—'}${e.triggerPx != null ? ` ${px(e.triggerPx)}` : ''}`)}
      {row('option stop first → last', `${px(e.optionSlInitial)} → ${px(e.optionSlFinal)}`)}
      {row('armed', e.armedBy ? `${e.armedBy}, ${e.armedMin ?? '—'} min in` : 'never')}
      {row('peak mid · dwell', `${e.peakMid ? px(e.peakMid) : '—'} · ${e.trailDwell ?? '—'}`)}
      {row('rungs sold', e.rungs.length ? `${e.rungs.map((r) => `${px(r.price)} ×${r.qty}`).join(', ')} (${(e.rungShare * 100).toFixed(0)} %)` : 'none')}
      {e.finalLegs && new Set(e.finalLegs.map((l) => l.reason)).size > 1 &&
        row('final legs', e.finalLegs.map((l) => `${l.reason ?? '—'} ${px(l.fill)} ×${l.qty}`).join(', '))}
      {row('held', `${e.heldMin} min`)}
      {e.refused && row('refused exit', `${e.refused.kind} at ${hms(e.refused.ts)}, book ${e.refused.ageMs != null ? `${(e.refused.ageMs / 1000).toFixed(1)} s` : '—'} old, executable ${px(e.refused.executable)}`)}
      {!e.audit && <div className="mt-0.5 text-[11px] text-amber-300">no exit audit: one leg at the ledger's average exit</div>}
    </div>
  )
}

function LessonBox({ l }: { l: Lesson }) {
  const line = (title: string, ev: Evidence) => (
    <div className="mt-1">
      <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
        <span className="text-slate-500">{title}</span>
        <Badge tone={GRADE_TONE(ev.grade)}>{ev.grade}</Badge>
        <span className="text-slate-500">{ev.strength} · {ev.n} case{ev.n === 1 ? '' : 's'}</span>
      </div>
      <div className="text-[12.5px] text-slate-300">{ev.text}.</div>
      {ev.params && ev.params.length > 0 && (
        <ul className="mt-0.5 text-[12px] text-slate-400">
          {ev.params.map((d) => <li key={d.key}>{d.label}: <span className="text-slate-300">{d.this}</span> → <span className="text-sky-200">{d.peer}</span></li>)}
        </ul>
      )}
    </div>
  )
  return (
    <div className="rounded border border-slate-700 bg-slate-950/50 px-2.5 py-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">lesson</span>
        {l.grade && <Badge tone={GRADE_TONE(l.grade)}>{l.grade}</Badge>}
        {l.gradeWhy && <span className="text-[11.5px] text-slate-500">{l.gradeWhy}</span>}
      </div>
      {l.decided || l.systematic ? (
        <>
          {l.decided && line('what decided this pair', l.decided)}
          {l.also && line('this rule pair across the record', l.also)}
          {l.systematic && line('systematic across the record', l.systematic)}
        </>
      ) : (
        <div className="mt-0.5 text-[12.5px] text-slate-300">{l.text}</div>
      )}
    </div>
  )
}

function BetterBox({ g, t, open, toggleOpen }: { g: BetterGroup; t: Trade; open: boolean; toggleOpen: () => void }) {
  const sum = Object.values(g.rupees).reduce((a, b) => a + b, 0)
  return (
    <div className="rounded border border-sky-900/60 bg-sky-950/15">
      <button type="button" onClick={toggleOpen} className="flex w-full flex-wrap items-center gap-2 px-2 py-1.5 text-left sm:px-2.5">
        <span className="text-[13px] font-semibold text-slate-100">{g.peer.engine}&apos;s {g.peer.label}</span>
        {g.members.length > 1 && <span className="text-[11.5px] text-slate-500">+ {g.members.length - 1} identical ({g.members.slice(1).map((m) => `${m.engine === g.peer.engine ? '' : `${m.engine} `}${m.label}`).join(', ')})</span>}
        <span className="text-[12px] text-slate-400">{g.relation}</span>
        <span className="ml-auto text-[13px]"><b className="text-emerald-300">{fmt.signedInr(g.gapRupees)}</b> <span className="text-slate-500">more</span></span>
        <span className="text-slate-500">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-sky-900/40 px-2 py-2 sm:px-2.5">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">where the {fmt.signedInr(g.gapRupees)} came from — exact, from the record</div>
            {/* the rupees above their words at phone width, beside them from sm up */}
            <div className="mt-1 space-y-1 text-[12.5px]">
              {g.lines.map((l) => (
                <div key={l.key} className={cls('flex flex-col sm:flex-row sm:gap-3', l.major ? 'text-slate-200' : 'text-slate-500')}>
                  <span className={cls('shrink-0 whitespace-nowrap font-semibold tabular-nums sm:w-24 sm:text-right', pnlColor(l.rupees))}>{fmt.signedInr(l.rupees)}</span>
                  <span className="min-w-0">{l.text}</span>
                </div>
              ))}
              <div className="flex flex-col border-t border-slate-800 pt-1 text-slate-300 sm:flex-row sm:gap-3">
                <span className="shrink-0 whitespace-nowrap font-bold tabular-nums sm:w-24 sm:text-right">{fmt.signedInr(sum)}</span>
                <span className="min-w-0">
                  = {g.peer.engine}&apos;s {g.peer.label} {inrP(g.peer.net)} − this trade {inrP(t.net)} = {inrP(g.gap)}
                  <span className="text-slate-500"> (to the paisa; the parts are whole rupees that add up to it)</span>{g.note ? ` · ${g.note}` : ''}
                </span>
              </div>
            </div>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <ExitColumn e={g.exits.this} title={g.exits.thisIsChain ? `this fill under the peer's stop rule (${g.exits.this.who})` : 'this trade'} />
            <ExitColumn e={g.exits.peer} title={g.exits.peer.who} />
          </div>
          {g.paramDiffs.length > 0 && (
            <div className="text-[12px]">
              <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">parameters that differ (this → peer)</div>
              <div className="mt-0.5 flex flex-wrap gap-x-4 gap-y-0.5 text-slate-400">
                {g.paramDiffs.map((d) => <span key={d.key}>{d.label}: <span className="text-slate-300">{d.this}</span> → <span className="text-sky-200">{d.peer}</span></span>)}
              </div>
            </div>
          )}
          <LessonBox l={g.lesson} />
        </div>
      )}
    </div>
  )
}

const OUTCOME_TONE: Record<string, 'red' | 'green' | 'amber'> = { 'every variant lost': 'red', 'every variant won': 'green', mixed: 'amber' }

/** Every other book and engine on this same contract while this trade was held, and for each that made more, why. */
function PeersBox({ p, t }: { p: Peers; t: Trade }) {
  const [openIdx, setOpenIdx] = useState<number | null>(0)
  const [all, setAll] = useState(false)
  const rows = all ? p.rows : p.rows.slice(0, 12)
  return (
    <div className="rounded border border-slate-700 bg-slate-900/40 px-2 py-2 sm:px-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-slate-400">same option elsewhere</span>
        <span className="text-[12px] text-slate-500">{contractName(p.contract)} · {p.n} peer{p.n === 1 ? '' : 's'}</span>
        {p.outcome && <Badge tone={OUTCOME_TONE[p.outcome] ?? 'slate'}>{p.outcome}{p.scope ? ' here' : ''}</Badge>}
        {p.scope && <span className="text-[11.5px] text-amber-300/80">{p.scope}</span>}
        {p.pooledTo && <span className="ml-auto text-[11px] text-slate-600">lessons pooled to {p.pooledTo}</span>}
      </div>
      {p.selfRefusal && (
        <div className={cls('mt-1 rounded border px-2 py-1 text-[12px]', p.selfRefusal.artefact ? 'border-amber-700 bg-amber-950/30 text-amber-200' : 'border-slate-700 text-slate-400')}>
          {p.selfRefusal.artefact && <b>Paper artefact. </b>}{p.selfRefusal.text}
        </div>
      )}
      <div className="mt-1 text-[13px] text-slate-200">{p.headline}</div>
      {p.outcome === 'every variant lost' && (
        <div className="mt-0.5 text-[12px] text-rose-300">
          {p.scope
            ? `No exit or stop rule on this engine saved this option (${p.scope}): only skipping the trigger would have.`
            : 'No exit, stop rule or engine saved this option: only skipping the trigger would have.'}
        </div>
      )}
      {p.rows.length > 0 && (
        <div className="mt-1.5 overflow-x-auto">
          <table className="w-full text-[12px] tabular-nums">
            <thead>
              <tr className="text-left text-slate-500">
                <th className="pr-2 font-normal">engine</th><th className="pr-2 font-normal">book</th><th className="pr-2 font-normal">variant</th>
                <th className="pr-2 font-normal">in → out</th><th className="pr-2 text-right font-normal">entry</th><th className="pr-2 text-right font-normal">exit (avg)</th>
                <th className="pr-2 text-right font-normal">lots</th><th className="pr-2 text-right font-normal">net</th><th className="font-normal">exit</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const better = r.net != null && t.net != null && r.net > t.net + 1
                return (
                  <tr key={`${r.engine}:${r.positionId}`} className={cls('border-t border-slate-900', better ? 'text-slate-100' : 'text-slate-400')}>
                    <td className="pr-2">{r.engine || '—'}</td>
                    <td className="pr-2">{r.label}{r.cls === 'shadow' ? ' ·shadow' : ''}</td>
                    <td className="pr-2">{RULE_LABEL[r.variant] ?? r.variant}</td>
                    <td className="pr-2 whitespace-nowrap">{hms(r.openedTs)} → {r.closedTs ? hms(r.closedTs) : 'open'}</td>
                    <td className="pr-2 text-right">{px(r.entry)}</td>
                    <td className="pr-2 text-right">{px(r.exitAvg)}</td>
                    <td className="pr-2 text-right">{lotsTxt(r.lots)}</td>
                    <td className={cls('pr-2 text-right font-semibold', pnlColor(r.net))}>{r.net == null ? 'open' : fmt.signedInr(r.net)}</td>
                    <td className="whitespace-nowrap">{r.exitReason ?? '—'}{r.mech ? ` · ${r.mech}` : ''}{r.refused ? ` · ${r.refused} refused` : ''}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {p.rows.length > 12 && (
            <button type="button" onClick={() => setAll(!all)} className="mt-0.5 text-[11.5px] text-slate-400 underline decoration-dotted hover:text-white">
              {all ? 'fewer' : `all ${p.rows.length} peers`}
            </button>
          )}
        </div>
      )}
      {p.better.length > 0 && (
        <div className="mt-2 space-y-1.5">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            made more on this option — {p.betterCount} trade{p.betterCount === 1 ? '' : 's'}, identical outcomes grouped
          </div>
          {p.better.map((g, i) => <BetterBox key={`${g.peer.engine}:${g.peer.positionId}`} g={g} t={t} open={openIdx === i} toggleOpen={() => setOpenIdx(openIdx === i ? null : i)} />)}
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
    const lab = t.directionRead?.label
    if (lab?.ts && (lab.first === 'up' || lab.first === 'down'))
      marks.push({ ts: snap(lab.ts), position: lab.first === 'up' ? 'aboveBar' : 'belowBar', color: '#a78bfa', shape: 'circle', text: `0.5 ATR ${lab.first}` })
    const known = new Set(bars.map((b) => b.ts))
    return { bars, lines, marks: marks.filter((m) => known.has(m.ts)).sort((a, b) => a.ts - b.ts) }
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

/** The "better on this option" chip says how sure its lesson is: a refusal or a halt is never one (greyed), else the grade. */
const LEVEL_CHIP: Record<string, { tone: string; say: string }> = {
  artefact: { tone: 'border-slate-600 bg-slate-800/60 text-slate-400', say: 'no lesson: a refusal or a halt' },
  systematic: { tone: 'border-emerald-500/60 bg-emerald-500/10 text-emerald-200', say: 'systematic lesson' },
  suggestive: { tone: 'border-amber-500/50 bg-amber-500/10 text-amber-200', say: 'suggestive lesson' },
  'one-off': { tone: 'border-sky-500/40 bg-sky-500/5 text-sky-200/80', say: 'one-off' },
}

function TradeCard({ t, showEngine, showDay }: { t: Row; showEngine: boolean; showDay: boolean }) {
  const [open, setOpen] = useState((t.net ?? 0) < 0)
  const better = bestBetter(t)
  const level = lessonLevel(better)
  const chip = level ? LEVEL_CHIP[level] : null
  const artefact = t.peers?.selfRefusal?.artefact ? t.peers.selfRefusal : null
  return (
    <section className={cls('rounded-lg border bg-slate-900/40', t.mirror ? 'border-slate-800/70 border-dashed' : 'border-slate-800')}>
      <button onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left">
        {showEngine && (
          <span className={cls('rounded px-1.5 py-0.5 text-[11px] font-semibold', t.origin === 'peer' ? 'bg-violet-900/70 text-violet-100' : 'bg-sky-900/60 text-sky-100')}>{t.engineName}</span>
        )}
        <StrategyBadge k={t.book} label={t.mirror ? t.sourceLabel : t.label} />
        {t.mirror && <Badge tone={t.variant === 'E' ? 'blue' : 'violet'}>twin · {RULE_LABEL[t.variant ?? 'current']}</Badge>}
        {t.shadow && !t.mirror && <Badge tone="amber">shadow</Badge>}
        <span className="font-medium text-slate-100">
          {t.symbol}
          <span className={`ml-1 text-[10px] ${t.direction === 'BULLISH' ? 'text-emerald-500' : 'text-rose-500'}`}>{t.direction === 'BULLISH' ? '▲' : '▼'}</span>
        </span>
        <span className="text-xs text-slate-400">{contractName(t.contract)}</span>
        {t.counterTrend && <Badge tone="amber">counter-trend</Badge>}
        {t.grade && <Badge tone="slate">grade {t.grade}</Badge>}
        <DirectionBadge d={t.directionRead} />
        <span className="text-xs text-slate-500">
          {t.day && (showDay || showEngine) ? `${t.day.slice(5)} ` : ''}{hm(t.openedTs)} → {t.closedTs ? hm(t.closedTs) : 'open'} · {t.status === 'OPEN' ? 'running' : dur(t.openedTs, t.closedTs)}
          {t.exitReason ? ` · ${t.exitReason}` : ''}
        </span>
        {better && chip && (
          <span title={`${t.peers?.headline ?? ''} ${better.lesson.gradeWhy || better.lesson.text}`} className={cls('rounded-full border px-2 py-0.5 text-[11px]', chip.tone)}>
            {better.peer.engine && better.peer.engine !== t.engineName ? `${better.peer.engine}'s ` : ''}{better.peer.label} {fmt.signedInr(better.gapRupees)} better on this option · {chip.say}
          </span>
        )}
        {artefact && (
          <span title={artefact.text} className="rounded-full border border-amber-600/60 bg-amber-950/40 px-2 py-0.5 text-[11px] text-amber-200">
            paper artefact: {fmt.signedInr(artefact.netIfFilled)} had the refused exit filled
          </span>
        )}
        {t.peers?.outcome === 'every variant lost' && (
          <span title={t.peers.scope ? `every book on this engine lost on this option (${t.peers.scope})` : 'every book and engine on this option lost'}
            className="rounded-full border border-rose-500/50 bg-rose-500/10 px-2 py-0.5 text-[11px] text-rose-200">every variant lost{t.peers.scope ? ' here' : ''}</span>
        )}
        <span className="ml-auto flex items-center gap-3">
          <span className={cls('text-xs', t.cause.key === 'win' ? 'text-emerald-400' : t.cause.key === 'open' ? 'text-slate-400' : 'text-rose-300')}>{t.cause.label}</span>
          <span className={cls('text-base font-semibold', pnlColor(t.net))}>{t.net == null ? 'open' : fmt.signedInr(t.net)}</span>
          <span className="text-slate-500">{open ? '▾' : '▸'}</span>
        </span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-slate-800 px-3 py-3">
          <CauseBox t={t} />
          {t.directionRead && <DirectionBox d={t.directionRead} />}
          {t.peers && <PeersBox p={t.peers} t={t} />}
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
                <span className="text-amber-400">nearest level the trade's way</span> · <span className="text-violet-400">0.5 ATR30 first reached</span>
              </div>
            </div>
          </div>
          <Facts t={t} />
        </div>
      )}
    </section>
  )
}

/** The review's summary over the cards the filters keep (the result buttons aside): the twins re-trade a fill and stay out. */
function summarize(rows: Row[]) {
  const own = rows.filter((r) => !r.mirror)
  const closed = own.filter((r) => r.status === 'CLOSED')
  const causes = new Map<string, { key: string; label: string; trades: number; net: number }>()
  for (const r of closed) {
    if (r.cause.key === 'win' || r.cause.key === 'open') continue
    const c = causes.get(r.cause.key) ?? { key: r.cause.key, label: r.cause.label, trades: 0, net: 0 }
    c.trades += 1
    c.net += r.net ?? 0
    causes.set(r.cause.key, c)
  }
  const right = own.filter((r) => r.directionRead?.label.tradeRight === true).length
  const wrong = own.filter((r) => r.directionRead?.label.tradeRight === false).length
  const allLost = own.filter((r) => r.peers?.outcome === 'every variant lost')
  return {
    trades: own.length, closed: closed.length, open: own.length - closed.length, net: closed.reduce((a, r) => a + (r.net ?? 0), 0),
    losses: closed.filter((r) => (r.net ?? 0) < 0).length, wins: closed.filter((r) => (r.net ?? 0) >= 0).length,
    causes: [...causes.values()].sort((a, b) => a.net - b.net), books: new Set(own.map((r) => r.sourceBook || r.book)).size,
    right, wrong, allLostN: allLost.length, allLostNet: allLost.reduce((a, r) => a + (r.net ?? 0), 0),
    betterN: own.filter((r) => r.peers?.betterExists).length,
  }
}

export function Review() {
  const [days, setDays] = useState({ day: todayIst(), to: '' })
  const engine = useEngine()
  const [filters, setFiltersState] = useState<Filters>(loadFilters)
  const [panel, setPanel] = useState(false)
  const setFilters = (f: Filters) => {
    setFiltersState(f)
    saveFilters(f)
  }
  const f: Filters = engine?.peer ? filters : { ...filters, engines: 'self' }
  const ranged = !!days.to && days.to !== days.day
  // the twins (stop-rule mirrors as cards) only when they are shown: 8 Oct is 4.2 MB with them, 0.6 MB without
  const q = `day=${days.day}${ranged ? `&to=${days.to}` : ''}${f.mirrors ? '&mirrors=1' : ''}&peers=1`
  // a range that ends before today no longer changes: read it once (the server keeps its answer); today's every 20 s
  const every = (ranged ? days.to : days.day) >= todayIst() ? 20000 : 0
  const ownPath = f.engines !== 'peer' ? `/api/review?${q}` : null
  const twinPath = engine?.peer && f.engines !== 'self' ? `/api/peer/review?${q}` : null
  const own = usePoll<Day>(ownPath, every)
  const twin = usePoll<Day>(twinPath, every)
  // only the answer to the question asked now: an error on a new range must not leave the old range's cards under its header
  const ownData = ownPath && own.dataPath === ownPath ? own.data : null
  const twinData = twinPath && twin.dataPath === twinPath && !twin.error ? twin.data : null
  const rows: Row[] = useMemo(() => {
    const tag = (d: Day | null, side: Side, fallback: string) => (d?.trades ?? []).map((t) => ({ ...t, origin: side, engineName: t.engine || d?.engine || fallback }))
    return [...tag(ownData, 'self', engine?.name || 'this engine'), ...tag(twinData, 'peer', engine?.peer?.name || 'twin')]
  }, [ownData, twinData, engine])
  const opts = useMemo(() => chipOptions(rows, filters), [rows, filters])
  const kept = rows.filter((t) => passes(t, f, { skipResult: true }))
  const shown = kept.filter((t) => !f.result.length || f.result.includes(resultOf(t))).sort((a, b) => (a.net ?? 0) - (b.net ?? 0))
  const s = summarize(kept)
  const n = activeCount(f, ranged)
  const quick = quickOf(f)
  const data = ownData ?? twinData
  const twinName = engine?.peer?.name || 'The twin'
  const notes = [...new Set([...(ownData?.notes ?? []), ...(twinData?.notes ?? []).map((x) => `${twinData?.engine || 'twin'}: ${x}`)])]
  // the error that blocks what was asked: this engine's when its cards are asked for, else the twin's
  const blocking = ownPath ? own.error : twin.error
  const loading = !blocking && ((!!ownPath && !ownData) || (!ownPath && !!twinPath && !twinData))
  const showEngine = f.engines === 'both'
  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={blocking ? (ownPath ? blocking : `${twinName}'s cards are not available: ${blocking}`) : null} />
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-100">
          Trade review{f.engines === 'peer' && engine?.peer ? ` · ${engine.peer.name}` : f.engines === 'both' ? ' · both engines' : engine?.name ? ` · ${engine.name}` : ''}
        </h1>
        <input type="date" value={days.day} max={todayIst()} onChange={(e) => e.target.value && setDays(dayChange(days, e.target.value))} className="rounded bg-slate-900 px-2 py-1 text-xs text-slate-200" />
        {ranged && <span className="text-xs text-slate-400">to {days.to}</span>}
        {engine?.peer && (
          <span className="flex items-center gap-1">
            {([['self', engine.name || 'this engine'], ['peer', engine.peer.name || 'twin'], ['both', 'both']] as const).map(([k, label]) => (
              <button key={k} onClick={() => setFilters({ ...filters, engines: k })} className={cls('rounded px-2 py-0.5 text-[11px]', f.engines === k ? 'bg-violet-800 text-white' : 'bg-slate-800 text-slate-400 hover:text-white')}>
                {label}
              </button>
            ))}
          </span>
        )}
        <button type="button" onClick={() => setPanel(!panel)} title="filter the cards"
          className={cls('relative inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[13px]', panel || n ? 'border-sky-400/60 bg-sky-400/10 text-sky-100' : 'border-slate-700 bg-slate-900 text-slate-300 hover:text-white')}>
          <Funnel /> Filter
          {n > 0 && <span className="ml-0.5 rounded-full bg-sky-500 px-1.5 text-[11px] font-bold text-white">{n}</span>}
        </button>
        <span className="ml-auto flex items-center gap-1">
          {QUICK.map(([k, label, result]) => (
            <button key={k} onClick={() => setFilters({ ...filters, result: [...result] })} className={cls('rounded px-2.5 py-1 text-xs', quick === k ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400 hover:text-white')}>
              {label}
            </button>
          ))}
        </span>
      </div>
      {panel && <FilterPanel f={f} set={setFilters} opts={opts} engine={engine} days={days} setDays={setDays} />}
      {ownPath && twinPath && twin.error && (
        <div className="text-[12px] text-slate-500">{twinName}&apos;s cards are not shown: {twin.error}</div>
      )}
      {notes.map((x) => <div key={x} className="text-[12px] text-slate-500">{x}</div>)}
      {data && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
          <Stat label="Trades" value={s.trades} sub={`${s.closed} closed · ${s.open} open`} />
          <Stat label="Net (closed)" value={fmt.signedInr(s.net)} tone={pnlColor(s.net)} />
          <Stat label="Losses" value={s.losses} tone="text-rose-400" />
          <Stat label="Wins" value={s.wins} tone="text-emerald-400" />
          <Stat label="Books" value={s.books} sub={f.mirrors ? 'the twins are shown as cards, never counted' : 'the stop-rule mirrors ride on each trade'} />
          <Stat label="Direction right · wrong" value={`${s.right} · ${s.wrong}`} sub="0.5 ATR30 first after the trigger's close" />
          <Stat label="Every variant lost" value={s.allLostN} sub={`${fmt.signedInr(s.allLostNet)} — no exit would have saved these`} tone={s.allLostN ? 'text-rose-300' : undefined} />
        </div>
      )}
      {s.causes.length > 0 && (
        <Card title="Why the losses happened" right="each loss's root cause, from the record — the biggest first">
          <div className="flex flex-wrap gap-2">
            {s.causes.map((c) => (
              <button type="button" key={c.key} onClick={() => setFilters({ ...filters, causes: toggle(filters.causes, c.key) })}
                className={cls('rounded border px-2.5 py-1 text-xs text-slate-200', filters.causes.includes(c.key) ? 'border-sky-500 bg-sky-950/40' : 'border-rose-900/70 bg-rose-950/30')}>
                {c.label} <b className="ml-1 text-slate-100">{c.trades}</b> <span className={cls('ml-1', pnlColor(c.net))}>{fmt.signedInr(c.net)}</span>
              </button>
            ))}
          </div>
        </Card>
      )}
      {loading ? (
        <div className="text-sm text-slate-500">loading…</div>
      ) : blocking && !data ? null : shown.length === 0 ? (
        <div className="text-sm text-slate-500">no trade passes the filters on {days.day}{ranged ? ` to ${days.to}` : ''}</div>
      ) : (
        <div className="space-y-2">
          {shown.map((t) => (
            <TradeCard key={`${t.origin}:${t.id}`} t={t} showEngine={showEngine} showDay={ranged} />
          ))}
        </div>
      )}
      {data && (
        <div className="space-y-1 text-[11px] text-slate-500">
          <div>
            Rules, in the order a root cause is chosen: costs · a rule's close · exit cost · trail at breakeven · option stop before the stock's · stop inside the noise · gave
            back · failed fade · stopped at the level ahead · chased · entry cost · right but early · market against · OI against · no volume — the first that holds. Thresholds:{' '}
            {Object.entries(data.thresholds).map(([k, v]) => `${k} ${v}`).join(' · ')}.
          </div>
          <div>
            Same option elsewhere: every other book and engine on this contract while the trade was held. Each better peer&apos;s gap is split exactly — entry timing and
            fill, size, target rungs, the exit decision and its fill, the stop rule, a stale-book refusal (modelled: the refused exit at the price the engine recorded)
            and charges — and the parts add up to the gap to the rupee. The lesson is graded on the pooled same-option study: systematic only when it held across
            both windows and both engines; a refusal or a halt is never a lesson. The best of up to 30 peers is a hindsight upper bound, not a missed profit.
          </div>
          <div>
            Direction: each read at the decision is marked with or against the trade by a threshold the review or the engine already uses; the label is which side
            the stock reached 0.5 ATR30 first after the trigger&apos;s close. The 6 Oct reversal study and the 8 Oct loss forensics found no decision-time read that picks the
            direction reliably out of sample; these are shown as measured, not as a verdict.
            {data.skipRules ? ' The candidate skip rules are read from the engine\'s rules file.' : ''}
          </div>
        </div>
      )}
    </div>
  )
}
