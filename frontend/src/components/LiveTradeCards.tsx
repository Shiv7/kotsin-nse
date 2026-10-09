import { useMemo, useState } from 'react'
import { cls, contractName, fmt, pnlColor } from '../lib/api'
import type { EngineId } from '../lib/engine'
import { usePoll } from '../lib/usePoll'
import type { LivePositions, LiveTradeRow, StopRule } from '../types'
import { RULE_LABEL } from './StopRuleGrid'
import { TradeMap } from './TradeMap'
import { Badge, ErrorLine } from './Ui'

// Every open trade on both engines, one card each (operator, 2026-10-09: "a dedicated page for all actove trades
// ... in the form of cards wich can be fileterd using a filter icon"). Read-only: the cards come from
// /api/positions/live on this engine and, through the read-only forwarder, on its twin; nothing here can place,
// move or close anything. Every number is worked out by the engine (api/live_trades.py), not here.

type Side = 'self' | 'peer'
type Row = LiveTradeRow & { origin: Side; engineName: string }

const POLL_MS = 3000
const STORE = 'kn.options.activeFilters.v1'

type Filters = {
  engines: 'both' | Side
  books: string[]
  variants: StopRule[]
  mirrors: boolean
  shadows: boolean
  pnl: string[]
  trigger: string[]
  winning: string[]
  vsTrade: string[]
  aligned: string[]
  from: string
  to: string
  search: string
  staleOnly: boolean
}

const BLANK: Filters = {
  engines: 'both', books: [], variants: [], mirrors: true, shadows: true, pnl: [], trigger: [], winning: [], vsTrade: [], aligned: [],
  from: '', to: '', search: '', staleOnly: false,
}

/** The filters kept from the last visit. Storage can be missing or refuse (a private window): the page works without it. A
 *  saved field is kept only with the type it should have: an older or hand-edited entry ({"books": null}) took the page
 *  down on every visit, with "show everything" out of reach (review, 2026-10-09). */
function loadFilters(): Filters {
  try {
    const raw = window.localStorage.getItem(STORE)
    const saved: unknown = raw ? JSON.parse(raw) : null
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return BLANK
    const s = saved as Record<string, unknown>
    const out: Record<string, unknown> = { ...BLANK }
    for (const [k, want] of Object.entries(BLANK)) {
      const v = s[k]
      if (Array.isArray(want) ? Array.isArray(v) && v.every((x) => typeof x === 'string') : typeof v === typeof want) out[k] = v
    }
    if (!['both', 'self', 'peer'].includes(out.engines as string)) out.engines = 'both'
    return out as Filters
  } catch {
    return BLANK
  }
}

function saveFilters(f: Filters) {
  try {
    window.localStorage.setItem(STORE, JSON.stringify(f))
  } catch {
    /* not kept: the filters still apply on this page */
  }
}

/** How many filters differ from showing everything — the number on the filter icon. */
function activeCount(f: Filters): number {
  const lists = [f.books, f.variants, f.pnl, f.trigger, f.winning, f.vsTrade, f.aligned].filter((x) => x.length).length
  return lists + (f.engines !== 'both' ? 1 : 0) + (!f.mirrors ? 1 : 0) + (!f.shadows ? 1 : 0) + (f.from || f.to ? 1 : 0) + (f.search.trim() ? 1 : 0) + (f.staleOnly ? 1 : 0)
}

const notFresh = (r: LiveTradeRow) => r.orderBook.state !== 'fresh'
/** The IST day of an epoch second, YYYY-MM-DD: an entry from an earlier day sorts before any time today. */
const istDay = (ts: number) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(ts * 1000))

function passes(r: Row, f: Filters, today: string): boolean {
  if (f.engines !== 'both' && r.origin !== f.engines) return false
  if (f.books.length && !f.books.includes(r.source_book || r.book)) return false
  if (f.variants.length && !f.variants.includes(r.variant)) return false
  if (!f.mirrors && r.mirror) return false
  // a mirror goes with its book: hidden with the shadows when the book it copies is one (the wide stop's two)
  if (!f.shadows && (r.mirror ? r.sourceShadow : r.shadow)) return false
  if (f.pnl.length && !f.pnl.includes(r.pnl.state ?? '')) return false
  if (f.trigger.length && !f.trigger.includes(r.triggerDirection)) return false
  if (f.winning.length && !f.winning.includes(r.winning.direction ?? '')) return false
  if (f.vsTrade.length && !f.vsTrade.includes(r.winning.vsTrade ?? '')) return false
  if (f.aligned.length && !f.aligned.includes(r.market.aligned)) return false
  // the entry-time range is today's: a trade carried from an earlier day is before any "from"
  const at = `${r.openedDay ?? today} ${r.openedHm}`
  if (f.from && at < `${today} ${f.from}`) return false
  if (f.to && at > `${today} ${f.to}`) return false
  // every word, in any order: "270 CE" finds "LTF 27 OCT 2026 CE 270"
  const words = f.search.trim().toLowerCase().split(/[\s,]+/).filter(Boolean)
  const hay = `${r.underlying} ${r.contract} ${contractName(r.contract)}`.toLowerCase()
  if (words.length && !words.every((w) => hay.includes(w))) return false
  if (f.staleOnly && !notFresh(r) && !r.staleExits) return false
  return true
}

const hms = (ts: number | null | undefined) =>
  ts == null ? 'DM' : new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(ts * 1000))
/** Rupees: paise kept under ₹100, whole rupees above — the charges are tens of rupees, the P&L thousands. */
const rs = (v: number | null | undefined) => (v == null ? 'DM' : fmt.inr(v, Math.abs(v) < 100 ? 2 : 0))
/** A cost in rupees, signed the way it hurts: "−₹1,350" when the exit gives it up, "+₹200" when it gains. */
const cost = (v: number | null | undefined) => (v == null ? 'DM' : v === 0 ? '₹0' : `${v > 0 ? '−' : '+'}${rs(Math.abs(v))}`)
/** A price with the paise it has: the signal's premium can be a mid (5.325), every fill is on the tick. */
const px = (v: number | null | undefined) => (v == null ? 'DM' : Math.abs(v * 100 - Math.round(v * 100)) > 1e-6 ? v.toFixed(3) : v.toFixed(2))
/** A slippage in rupees and which way it went: worse (a cost) or better than the price it is measured against. */
const slip = (v: number | null | undefined) => (v == null ? 'DM' : v === 0 ? '₹0' : `${cost(v)} ${v > 0 ? 'worse' : 'better'}`)
const lotsTxt = (n: number) => `${Number.isInteger(n) ? n : n.toFixed(2)} lot${n === 1 ? '' : 's'}`
const dirArrow = (d: string | null | undefined) => (d === 'BULLISH' ? '▲ bullish' : d === 'BEARISH' ? '▼ bearish' : d === 'FLAT' ? '◆ flat' : 'DM')
const dirTone = (d: string | null | undefined): 'green' | 'red' | 'slate' => (d === 'BULLISH' ? 'green' : d === 'BEARISH' ? 'red' : 'slate')

function Funnel() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 4h18l-7 9v6l-4 2v-8L3 4z" />
    </svg>
  )
}

function Chip({ on, onClick, children, title }: { on: boolean; onClick: () => void; children: React.ReactNode; title?: string }) {
  return (
    <button type="button" title={title} onClick={onClick}
      className={cls('rounded-full border px-2.5 py-0.5 text-[12px]', on ? 'border-sky-400/60 bg-sky-400/15 text-sky-100' : 'border-slate-700 bg-slate-900 text-slate-400 hover:text-slate-200')}>
      {children}
    </button>
  )
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="w-28 shrink-0 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</span>
      {children}
    </div>
  )
}

const toggle = <T,>(xs: T[], x: T) => (xs.includes(x) ? xs.filter((y) => y !== x) : [...xs, x])

function FilterPanel({ f, set, books, engine }: { f: Filters; set: (f: Filters) => void; books: [string, string][]; engine: EngineId | null }) {
  const many = (key: 'pnl' | 'trigger' | 'winning' | 'vsTrade' | 'aligned', opts: [string, string][]) =>
    opts.map(([v, label]) => <Chip key={v} on={f[key].includes(v)} onClick={() => set({ ...f, [key]: toggle(f[key], v) })}>{label}</Chip>)
  return (
    <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-900/70 p-3">
      {engine?.peer && (
        <Group label="Engines">
          {([['both', 'both'], ['self', engine.name || 'this engine'], ['peer', engine.peer.name || 'other engine']] as const).map(([v, label]) => (
            <Chip key={v} on={f.engines === v} onClick={() => set({ ...f, engines: v })}>{label}</Chip>
          ))}
        </Group>
      )}
      <Group label="Strategies">
        {books.map(([b, label]) => <Chip key={b} on={f.books.includes(b)} onClick={() => set({ ...f, books: toggle(f.books, b) })}>{label}</Chip>)}
        {!books.length && <span className="text-[12px] text-slate-600">none open</span>}
      </Group>
      <Group label="Variants">
        {(['current', 'E', 'A'] as StopRule[]).map((v) => <Chip key={v} on={f.variants.includes(v)} onClick={() => set({ ...f, variants: toggle(f.variants, v) })}>{RULE_LABEL[v]}</Chip>)}
      </Group>
      <Group label="Twins · shadows">
        <Chip on={f.mirrors} onClick={() => set({ ...f, mirrors: !f.mirrors })} title="the twins: each book's stop-rule mirrors, the same fill under stop E and the adaptive stop">{f.mirrors ? 'twins shown' : 'twins hidden'}</Chip>
        <Chip on={f.shadows} onClick={() => set({ ...f, shadows: !f.shadows })} title="the other shadow books: the wide stop, graded F, CT-M">{f.shadows ? 'shadows shown' : 'shadows hidden'}</Chip>
      </Group>
      <Group label="P&L">{many('pnl', [['profit', 'profit'], ['loss', 'loss'], ['neutral', 'neutral']])}</Group>
      <Group label="Trigger">{many('trigger', [['BULLISH', 'bullish'], ['BEARISH', 'bearish']])}</Group>
      <Group label="Winning way">
        {many('winning', [['BULLISH', 'bullish'], ['BEARISH', 'bearish'], ['FLAT', 'flat']])}
        <span className="mx-1 text-slate-700">|</span>
        {many('vsTrade', [['with', 'with the trade'], ['against', 'against it']])}
      </Group>
      <Group label="Market aligned">{many('aligned', [['yes', 'yes'], ['no', 'no'], ['unchanged', 'unchanged'], ['unknown', 'unknown']])}</Group>
      <Group label="Entry time">
        <input type="time" value={f.from} onChange={(e) => set({ ...f, from: e.target.value })} className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[12px] text-slate-200" />
        <span className="text-[12px] text-slate-500">to</span>
        <input type="time" value={f.to} onChange={(e) => set({ ...f, to: e.target.value })} className="rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[12px] text-slate-200" />
        <span className="text-[11px] text-slate-600">today, IST</span>
      </Group>
      <Group label="Scrip / OTM">
        <input value={f.search} onChange={(e) => set({ ...f, search: e.target.value })} placeholder="LTF, CE 270 …" className="w-56 rounded border border-slate-700 bg-slate-950 px-2 py-0.5 text-[12px] text-slate-200" />
      </Group>
      <Group label="Order book">
        <Chip on={f.staleOnly} onClick={() => set({ ...f, staleOnly: !f.staleOnly })} title="a book older than the paper matcher's limit, no book at all, or exits already refused for a stale book">stale or no book only</Chip>
      </Group>
      <div className="pt-1">
        <button type="button" onClick={() => set(BLANK)} className="text-[12px] text-slate-400 underline decoration-dotted hover:text-white">show everything</button>
      </div>
    </div>
  )
}

/** "book 4 s old" while the paper matcher would trade on it; red STALE past its limit; NO BOOK without one. */
function BookChip({ r }: { r: LiveTradeRow }) {
  const b = r.orderBook
  if (b.state === 'none')
    return <span title="no depth book for this contract: an exit would trade on the quote's touch, or not at all" className="rounded-full border border-rose-400/50 bg-rose-500/15 px-2.5 py-0.5 text-[12px] font-semibold text-rose-200">NO BOOK</span>
  const title = `the order book was last refreshed ${hms(b.ts)} — the paper matcher refuses a fill on a book older than ${b.limitS} s now`
  return b.stale ? (
    <span title={title} className="rounded-full border border-rose-400/60 bg-rose-500/20 px-2.5 py-0.5 text-[12px] font-bold text-rose-100">STALE {fmt.n(b.ageS, 0)} s (limit {b.limitS} s)</span>
  ) : (
    <span title={title} className="rounded-full border border-emerald-400/40 bg-emerald-400/10 px-2.5 py-0.5 text-[12px] font-semibold text-emerald-200">book {fmt.n(b.ageS, 0)} s old</span>
  )
}

function Tile({ k, v, sub, tone, title }: { k: string; v: React.ReactNode; sub?: React.ReactNode; tone?: string; title?: string }) {
  return (
    <div title={title} className="rounded-lg border border-slate-800 bg-slate-950/50 px-2.5 py-1.5">
      <div className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">{k}</div>
      <div className={cls('text-[14px] font-semibold tabular-nums', tone ?? 'text-slate-100')}>{v}</div>
      {sub != null && <div className="text-[11px] leading-snug text-slate-500">{sub}</div>}
    </div>
  )
}

const STATE_TONE = { profit: 'green', loss: 'red', neutral: 'slate' } as const

export function LiveTradeCard({ r, neutralRule, other, today }: { r: Row; neutralRule: string; other: boolean; today: string }) {
  const [open, setOpen] = useState(false)
  const m = r.money
  const se = m.slippage.entry
  const ex = m.exitNow
  const w = ex?.walk
  const bad = r.orderBook.state !== 'fresh'
  const lots = (q: number | null | undefined) => (q ? lotsTxt(q / Math.max(1, r.lotSize)) : 'DM')
  return (
    <article className={cls('rounded-xl border bg-slate-900/50 p-3', bad ? 'border-rose-500/60' : 'border-slate-800')}>
      {/* who: the engine, the book and its variant, the contract and its size */}
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={cls('rounded px-1.5 py-0.5 text-[11px] font-semibold', other ? 'bg-violet-900/70 text-violet-100' : 'bg-sky-900/60 text-sky-100')} title={other ? 'the other engine, read through /api/peer' : 'this engine'}>
              {r.engineName || (other ? 'other engine' : 'this engine')}
            </span>
            <span className="text-[14px] font-semibold text-slate-100">{r.mirror ? r.source_label : r.bookLabel}</span>
            <Badge tone={r.variant === 'current' ? 'slate' : r.variant === 'E' ? 'blue' : 'violet'}>{RULE_LABEL[r.variant] ?? r.variant}</Badge>
            {r.mirror && <Badge tone="amber">twin · mirror of {r.source_label}</Badge>}
            {r.shadow && !r.mirror && <Badge tone="amber">shadow</Badge>}
            {r.trend === 'counter-trend' && <Badge tone="amber">counter-trend</Badge>}
            {r.grade && <Badge>grade {r.grade}</Badge>}
          </div>
          <div className="text-[15px] font-bold text-white">
            {contractName(r.contract)}
            <span className="ml-2 text-[12.5px] font-medium text-slate-400">
              {lotsTxt(r.lots)} × {r.lotSize.toLocaleString('en-IN')} = {r.qtyRemaining.toLocaleString('en-IN')}
              {r.qtyRemaining !== r.qty && <span className="text-slate-500"> (of {lotsTxt(r.lotsAtEntry)} bought)</span>}
            </span>
          </div>
          <div className="text-[12.5px] text-slate-400">
            entry <b className="text-slate-200">₹{px(r.entry)}</b> at {r.openedDay && r.openedDay !== today ? `${r.openedDay} ` : ''}{hms(r.openedTs)}
            {r.entryFrom && <span className="text-slate-500"> · the fill {r.entryFromLabel || r.entryFrom} took</span>}
            {' · '}trigger <Badge tone={dirTone(r.triggerDirection)}>{dirArrow(r.triggerDirection)}</Badge>
            {r.triggerTs != null && <span> · bar {hms(r.triggerTs).slice(0, 5)}–{hms(r.triggerTs + 1800).slice(0, 5)}</span>}
            {r.triggerClose != null && <span> · close {fmt.n(r.triggerClose)}</span>}
          </div>
        </div>
        <BookChip r={r} />
      </header>

      {/* the price now and the money */}
      <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[13px]">
        <span className="text-slate-400">LTP <b className="text-slate-100">{fmt.n(r.quote.ltp)}</b></span>
        <span className="text-slate-400">bid <b className="text-slate-100">{fmt.n(r.quote.bid)}</b> / ask <b className="text-slate-100">{fmt.n(r.quote.ask)}</b></span>
        <span className="text-slate-400">mid {r.quote.mid == null ? 'DM' : px(r.quote.mid)}</span>
        <span className={cls('text-[12px]', r.quote.ageS != null && r.quote.ageS > r.quote.limitS ? 'text-rose-300' : 'text-slate-500')} title="LTP, bid, ask and mid are the quote's: the one the exit loop judges the stop on and marks the position at">quote {fmt.n(r.quote.ageS, 0)} s</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <span className={cls('text-[22px] font-bold tabular-nums', pnlColor(r.pnl.unrealised))} title={r.pnl.basis === 'last' ? "the open lots at the last trade: the quote has no fresh bid, as the engine marks them then" : `the open lots at the quote's ${r.pnl.basis ?? 'last price'}, as the engine marks them: what they could be sold for now`}>
          {fmt.signedInr(r.pnl.unrealised)}
        </span>
        {r.pnl.state && <span title={neutralRule}><Badge tone={STATE_TONE[r.pnl.state]}>{r.pnl.state.toUpperCase()}</Badge></span>}
        <span className="text-[12px] text-slate-400" title={neutralRule}>
          {r.pnl.realisedGross !== 0 && <>sold so far {fmt.signedInr(r.pnl.realisedGross)} · </>}
          neutral band ±{rs(r.pnl.band)} · net if closed now <b className={pnlColor(r.pnl.netIfExitNow)}>{fmt.signedInr(r.pnl.netIfExitNow)}</b>
        </span>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-4">
        <Tile k="Charges so far" v={rs(m.chargesSoFar.total)} sub={`entry ${rs(m.chargesSoFar.entry)}${m.chargesSoFar.exits ? ` · slices ${rs(m.chargesSoFar.exits)}` : ''}`} tone="text-amber-300" />
        <Tile k="Slippage so far" v={slip(m.slippage.totalVsMidRs)} tone={m.slippage.totalVsMidRs > 0 ? 'text-rose-300' : m.slippage.totalVsMidRs < 0 ? 'text-emerald-300' : 'text-slate-100'}
          title="the entry fill against the mid of the book when its order was placed, and every slice sold against the mid when its order was placed; the signal's premium beside it"
          sub={se ? <>fill {px(se.fill)} vs signal {px(se.ref)}: {slip(se.vsRefRs)} · vs mid {px(se.midAtPlace)}: {slip(se.vsMidRs)}{se.copiedFrom ? ` · the fill ${se.copiedFromLabel || se.copiedFrom} took` : ''}</> : 'no order trail'} />
        <Tile k="Charges at exit" v={rs(m.exitCharges.total)} tone="text-amber-300" title="the charge model (data/charges.toml) on the open lots sold at the mark now: the quote's bid"
          sub={`${lots(m.exitCharges.qty)} at ${fmt.n(m.exitCharges.price)}`} />
        <Tile k="Exit now · slippage" v={ex?.basis === 'none' || !ex ? 'DM' : cost(ex.vsLtpRs)}
          tone={ex?.vsLtpRs != null && ex.vsLtpRs > 0 ? 'text-rose-300' : ex?.vsLtpRs != null && ex.vsLtpRs < 0 ? 'text-emerald-300' : 'text-slate-100'}
          title="the open lots sold at market now, against the last trade and the mid: a fresh book walked down its bids (against its own mid); a stale book's shape moved to the quote's bid and walked (against the quote's mid, an approximation); no book, the quote's bid alone"
          sub={!ex || ex.basis === 'none' ? 'no bid to sell into' : (
            <>
              vs LTP · {cost(ex.vsMidRs)} vs {ex.feed === 'book' ? "the book's" : "the quote's"} mid {ex.mid == null ? 'DM' : px(ex.mid)} · avg {fmt.n(ex.avg)}
              {ex.basis === 'shape' && <span className="text-rose-300"> · the book is stale: its shape under the quote&apos;s bid, an approximation (the matcher would refuse an exit now)</span>}
              {ex.basis === 'touch' && (
                <span className={ex.bookState === 'stale' ? 'text-rose-300' : 'text-amber-300'}>
                  {ex.bookState === 'stale' ? ' · the book is stale: the quote\'s bid alone (the matcher would refuse it now)' : ' · no depth: the quote\'s bid alone'}
                </span>
              )}
              {ex.feed === 'book' && ex.quoteTouch != null && ex.touch != null && Math.abs(ex.quoteTouch - ex.touch) > 1e-6 && (
                <span className="text-amber-300"> · the book&apos;s {r.side === 'LONG' ? 'bid' : 'ask'} {px(ex.touch)}, the quote&apos;s {px(ex.quoteTouch)}</span>
              )}
              {w && w.beyondQty > 0 && <span className="text-rose-300"> · {lots(w.beyondQty)} past the 5 levels</span>}
              {w && w.matcherQty < w.qty && <span className="text-amber-300"> · one cross sells {lots(w.matcherQty)}</span>}
            </>
          )} />
      </div>

      {r.staleExits && (
        <div className="mt-2 rounded-md border border-rose-500/50 bg-rose-950/40 px-2.5 py-1.5 text-[12.5px] text-rose-200">
          Order book stale: {r.staleExits.count} exit{r.staleExits.count > 1 ? 's' : ''} refused (oldest book {(r.staleExits.maxAgeMs / 1000).toFixed(1)} s
          {r.staleExits.limitMs ? `, limit ${(r.staleExits.limitMs / 1000).toFixed(0)} s` : ''}), last at {hms(r.staleExits.lastTs)}
        </div>
      )}

      {/* which way it is going and why */}
      <div className="mt-2 grid gap-2 md:grid-cols-2">
        <div className="rounded-lg border border-slate-800 bg-slate-950/40 px-2.5 py-1.5 text-[12.5px]">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">winning way</span>
            <Badge tone={dirTone(r.winning.direction)}>{dirArrow(r.winning.direction)}</Badge>
            {r.winning.vsTrade && r.winning.vsTrade !== 'flat' && <Badge tone={r.winning.vsTrade === 'with' ? 'green' : 'red'}>{r.winning.vsTrade === 'with' ? 'with the trade' : 'against the trade'}</Badge>}
          </div>
          {r.winning.reasons.length ? (
            <ul className="mt-1 list-disc space-y-0.5 pl-4 text-slate-300">{r.winning.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
          ) : (
            <div className="mt-1 text-slate-500">{r.winning.why || 'no reading held'}</div>
          )}
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-950/40 px-2.5 py-1.5 text-[12.5px]">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">market aligned</span>
            <Badge tone={r.market.aligned === 'yes' ? 'green' : r.market.aligned === 'no' ? 'red' : 'slate'}>{r.market.aligned}</Badge>
          </div>
          <div className="mt-1 text-slate-400">{r.market.why}</div>
        </div>
      </div>

      {/* the targets and stops, then the rail */}
      <div className="mt-2 space-y-1 text-[12.5px]">
        {r.targets.option.length > 0 && (
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">option</span>
            {r.targets.option.map((t) => (
              <span key={t.rung} className={t.hit ? 'text-emerald-300' : 'text-amber-200'} title={t.slippage ? `approximation: today's book moved to the rung — ${lots(t.slippage.touchQty)} at the rung, avg ${fmt.n(t.slippage.avg)} for the share` : undefined}>
                {t.hit ? '✔ ' : ''}T{t.rung} {fmt.n(t.price)}
                {!t.hit && (
                  <span className="text-slate-500">
                    {' · '}{t.share ?? ''}{t.qty && !/lots?$/.test(t.share ?? '') ? ` (${lots(t.qty)})` : ''}
                    {t.slippage ? (t.slippage.rs ? ` · ≈ ${cost(t.slippage.rs)}` : ' · ≈ all at the rung') : ''}
                  </span>
                )}
              </span>
            ))}
          </div>
        )}
        {(r.targets.equity.length > 0 || r.targets.future.length > 0) && (
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            <span className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">equity</span>
            {r.targets.equity.map((t, i) => <span key={i} className={t.passed ? 'text-emerald-300' : 'text-sky-200'}>{t.passed ? '✔ ' : ''}E{i + 1} {fmt.n(t.price)}</span>)}
            {r.targets.future.length > 0 && <span className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">future</span>}
            {r.targets.future.map((t, i) => <span key={i} className={t.passed ? 'text-emerald-300' : 'text-violet-200'} title={t.label}>{t.passed ? '✔ ' : ''}F{i + 1} {fmt.n(t.price)}</span>)}
          </div>
        )}
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-slate-400">
          <span className="text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">stops</span>
          <span>option <b className="text-rose-300">{fmt.n(r.targets.stopNow)}</b>{r.targets.stopNow !== r.targets.initialOptionStop && <span className="text-slate-500"> (first {fmt.n(r.targets.initialOptionStop)})</span>}</span>
          <span>equity <b className="text-rose-300">{fmt.n(r.targets.equityStop)}</b></span>
          {r.targets.line != null && r.targets.line > 0 && <span>line <b className="text-sky-300">{fmt.n(r.targets.line)}</b></span>}
          {r.targets.mirror && <span>premium cap {fmt.n(r.targets.mirror.premiumCap)}{r.targets.mirror.through && <span className="text-rose-300"> · through {r.targets.mirror.through.seconds} s</span>}</span>}
        </div>
      </div>
      <div className="mt-2">
        <TradeMap c={r.map} />
      </div>

      <button type="button" onClick={() => setOpen(!open)} className="mt-2 text-[12px] text-slate-400 underline decoration-dotted hover:text-white">
        {open ? 'hide' : 'show'} the order book and the exit plan
      </button>
      {open && (
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-2 text-[12px]">
            <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">order book · {fmt.n(r.orderBook.ageS, 1)} s old</div>
            {r.orderBook.depth ? (
              <table className="w-full tabular-nums">
                <thead><tr className="text-slate-500"><th className="text-right font-normal">lots</th><th className="text-right font-normal">bid</th><th className="text-left font-normal pl-3">ask</th><th className="text-left font-normal">lots</th></tr></thead>
                <tbody>
                  {Array.from({ length: Math.max(r.orderBook.depth.bids.length, r.orderBook.depth.asks.length) }).map((_, i) => {
                    const b = r.orderBook.depth!.bids[i], a = r.orderBook.depth!.asks[i]
                    return (
                      <tr key={i}>
                        <td className="text-right text-slate-400">{b ? lots(b[1]) : ''}</td>
                        <td className="text-right text-emerald-300">{b ? fmt.n(b[0]) : ''}</td>
                        <td className="pl-3 text-rose-300">{a ? fmt.n(a[0]) : ''}</td>
                        <td className="text-slate-400">{a ? lots(a[1]) : ''}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            ) : <div className="text-slate-500">no depth book held for this contract</div>}
          </div>
          <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-2 text-[12px]">
            <div className="mb-1 text-[10.5px] font-semibold uppercase tracking-wide text-slate-500">exit plan</div>
            {r.targets.exitPlan ? (
              <>
                <div className="mb-1 text-slate-400">{r.targets.exitPlan.policy}</div>
                <ul className="space-y-0.5 text-slate-300">{r.targets.exitPlan.rows.map((x, i) => <li key={i}>{x.at} — {x.action}{x.qty ? ` (${lots(x.qty)})` : ''}</li>)}</ul>
              </>
            ) : <div className="text-slate-500">DM</div>}
          </div>
        </div>
      )}
    </article>
  )
}

/** The Options page's first tab: every open trade on this engine and its twin, as cards, with the filter icon. */
export function LiveTradeCards({ engine }: { engine: EngineId | null }) {
  const own = usePoll<LivePositions>('/api/positions/live', POLL_MS)
  const twin = usePoll<LivePositions>(engine?.peer ? '/api/peer/positions/live' : null, POLL_MS)
  const [filters, setFiltersState] = useState<Filters>(loadFilters)
  const [panel, setPanel] = useState(false)
  const setFilters = (f: Filters) => {
    setFiltersState(f)
    saveFilters(f)
  }

  // a card only from a read that answered: an engine that stops answering keeps its last read in ``usePoll``, and its cards
  // would freeze on screen (their book age, their P&L) and count as open after they closed (review, 2026-10-09)
  const ownData = own.error ? null : own.data
  const twinData = engine?.peer && !twin.error ? twin.data : null
  const rows: Row[] = useMemo(() => {
    const tag = (d: LivePositions | null, side: Side, fallback: string) => (d?.rows ?? []).map((r) => ({ ...r, origin: side, engineName: r.engine || d?.engine || fallback }))
    return [...tag(ownData, 'self', engine?.name ?? ''), ...tag(twinData, 'peer', engine?.peer?.name ?? 'other engine')].sort((a, b) => b.openedTs - a.openedTs)
  }, [ownData, twinData, engine])
  const books = useMemo(() => {
    const seen = new Map<string, string>()
    for (const r of rows) seen.set(r.source_book || r.book, r.source_label || r.bookLabel)
    for (const b of filters.books) if (!seen.has(b)) seen.set(b, b)
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))
  }, [rows, filters.books])
  const today = istDay(ownData?.nowTs ?? twinData?.nowTs ?? Date.now() / 1000)
  const shown = rows.filter((r) => passes(r, filters, today))
  const n = activeCount(filters)
  const staleN = shown.filter((r) => r.orderBook.state === 'stale').length
  const noBookN = shown.filter((r) => r.orderBook.state === 'none').length
  // the open P&L of the books that trade, engine by engine: a mirror re-trades its book's fill and a shadow is a comparison,
  // so summing them in would count one fill several times (the Trades page's rule)
  const openBy = new Map<string, number>()
  for (const r of shown) if (!r.mirror && !r.shadow) openBy.set(r.engineName, (openBy.get(r.engineName) ?? 0) + (r.pnl.unrealised ?? 0))
  const shownEngines = [...new Set(shown.map((r) => r.engineName))]
  const neutralRule = ownData?.neutralRule ?? twinData?.neutralRule ?? ''
  const twinDown = engine?.peer && twin.error
  const tagged = (d: LivePositions | null, name: string) =>
    d ? [...d.notes, ...d.skipped.map((s) => `${s.book} ${s.positionId}: not shown — ${s.error}`)].map((x) => (engine?.peer ? `${d.engine || name}: ${x}` : x)) : []
  const messages = [...tagged(ownData, engine?.name || 'this engine'), ...tagged(twinData, engine?.peer?.name || 'the other engine')]

  return (
    <div className="space-y-3 p-4">
      <ErrorLine error={own.error} />
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => setPanel(!panel)} title="filter the cards"
          className={cls('relative inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[13px]', panel || n ? 'border-sky-400/60 bg-sky-400/10 text-sky-100' : 'border-slate-700 bg-slate-900 text-slate-300 hover:text-white')}>
          <Funnel /> Filter
          {n > 0 && <span className="ml-0.5 rounded-full bg-sky-500 px-1.5 text-[11px] font-bold text-white">{n}</span>}
        </button>
        <span className="text-[13px] text-slate-400">
          {shown.length} of {rows.length} open
          {shownEngines.length > 0 && (
            <span title="the open lots at the bid of the books that trade, each engine on its own: the twins (stop-rule mirrors) re-trade a book's fill and the shadows are comparisons, so they are left out, as on the Trades page">
              {' · open P&L of the trading books '}
              {shownEngines.map((name, i) => {
                const v = openBy.get(name)
                return (
                  <span key={name}>
                    {i > 0 && ' · '}{shownEngines.length > 1 || engine?.peer ? `${name} ` : ''}
                    {v != null ? <b className={pnlColor(v)}>{fmt.signedInr(v)}</b> : <span className="text-slate-500">none open (twins and shadows only)</span>}
                  </span>
                )
              })}
            </span>
          )}
          {staleN > 0 && <span className="text-rose-300"> · {staleN} on a stale book</span>}
          {noBookN > 0 && <span className="text-rose-300"> · {noBookN} with no book</span>}
        </span>
        <span className="text-[11.5px] text-slate-500">
          read-only · every {POLL_MS / 1000} s · book limit now {own.data ? `${own.data.bookLimit.nowMs / 1000} s` : 'DM'}
          {own.data ? ` (${own.data.bookLimit.maxMs / 1000} s, ${own.data.bookLimit.openMaxMs / 1000} s in ${own.data.bookLimit.openWindow})` : ''}
        </span>
      </div>
      {twinDown && (
        <div className="text-[12px] text-slate-500">
          {engine?.peer?.name || 'The other engine'}&apos;s trades are not shown: {twin.error}
        </div>
      )}
      {messages.map((x) => (
        <div key={x} className="text-[12px] text-amber-300">{x}</div>
      ))}
      {panel && <FilterPanel f={filters} set={setFilters} books={books} engine={engine} />}
      {!rows.length && !own.error && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-8 text-center text-[13px] text-slate-500">
          {ownData ? 'no open trades on ' + (engine?.peer ? 'either engine' : 'this engine') + ' right now' : 'loading…'}
        </div>
      )}
      {rows.length > 0 && !shown.length && <div className="text-[13px] text-slate-500">no open trade passes the filters</div>}
      <div className="grid gap-3 2xl:grid-cols-2">
        {shown.map((r) => <LiveTradeCard key={`${r.origin}:${r.id}`} r={r} neutralRule={neutralRule} other={r.origin === 'peer'} today={today} />)}
      </div>
    </div>
  )
}
