import { useState } from 'react'
import { usePoll } from '../lib/usePoll'

// Realtime firings, one tab per book. Polls every 3s while the market is open.
//
// These are advisory: nothing on this page can place an order. FUDKII — the only book here with a
// real artefact — backtests at −1.40R over 481 trades, so a freshly ported book goes to a page
// before it goes to a gateway.

type Alert = {
  book: string
  symbol: string
  scripCode: string
  tf: string
  ts: number
  direction: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  score: number
  reason: string
  price: number
  kind: 'TRIGGER' | 'KEEPALIVE' | 'EXPIRED'
  evidence: Record<string, unknown>
  company: string
  exchange: string
  barClose: number
  firedAt: number
  cta: { action: string; text: string }
  plan: Plan | null
  card: RtCard | null
}

type WallJ = {
  price: number; strength: number; members: string[]; timeframes: string[]
  levels: number; distAtr: number; distPct: number; grade: string
  qualifies: boolean; min: number; side: string
}

type RtCard = {
  wallAhead: WallJ | null
  wallBehind: WallJ | null
  odds: { pT1: number | null; risk?: number; reward?: number; model?: string; note?: string }
  confidence: { score: number; components: { factor: string; points: number; detail: string }[]; note: string }
  stop: {
    basis: string; constantForSession: boolean; equityStop: number; equityMove: number
    equityDistPct: number | null; optionStop: number | null; optionDistPct: number | null
    delta: number; deltaRefreshS: number; triggersFirst: string | null
    equityHit: boolean; optionHit: boolean; triggered: boolean; triggeredBy: string | null; rule: string
  } | null
  optionLadder: { n: number; equity: number; option: number; equityMove: number; optionGainPct: number | null; source: string }[]
  volumeBaseline: { ratio: number; median: number; sessions: number; slot: string } | null
  greeks: { delta: number; deltaSource: string; dte: number | null; gamma: null; theta: null; iv: null; unavailable: string }
  sizing: {
    lots: number; qty: number; capital: number; costPerLot: number; binding: string
    rejected: boolean; reason: string; slotsLeft: number; maxConcurrent: number
    unusedReturnedToWallet: number; capPerTrade: number; maxLots: number
  }
  atr30m: number | null
  liveEquity: number | null
  marksTs?: number
  exitWalks?: {
    t1_1lot: Walk
    trail_3lots: Walk
  }
}

type Walk = {
  lots: number; qty: number; fill: number | null; source: string
  levelsWalked: number; capped: boolean
  slippageVsMidPct: number | null; slippageVsLtpPct: number | null; proceeds: number | null
}

type Plan = {
  entry: number
  sl: number | null
  t1: number | null
  t2: number | null
  t3: number | null
  t4: number | null
  rr: number
  grade: string
  atr: number | null
  hasPivots: boolean
  optionType: 'CE' | 'PE'
  strike: number
  strikeInterval: number
  deltaApprox: number
  fortress: number
  roomAtr: number
  stopZone: string
  targetZones: string[]
  note: string
  listed: {
    scripCode: string
    symbol: string
    strike: number
    type: string
    expiry: string
    lotSize: number
    ltp: number | null
    oi: number | null
    quotes: boolean
    strikeGapFromTheoretical: number
    entry: {
      ts: number
      lagFromBarCloseS: number
      lagFromFiredS: number
      lots: number
      qty: number
      ltp: number | null
      bid: number | null
      ask: number | null
      quoteAgeS: number | null
      fill: number | null
      fillSource: string
      levelsWalked: number
      capped: boolean
      slippageVsLtpPct: number | null
      notional: number | null
      stale: boolean
      note: string
    } | null
  } | null
}

type Resp = {
  alerts: Alert[]
  counts: Record<string, number>
  evaluated: Record<string, number>
  living: number
  books: string[]
  marksAgeS: number | null
  refreshes: number
  suppressedByCap: Record<string, number>
  capReached: Record<string, boolean>
  uptime_s: number
  now_ist: string
}

const LABEL: Record<string, string> = {
  FUDKII_RT: 'FUDKII-RT',
  FUDKOI: 'FUDKOI',
  PIVOTBOSS: 'PIVOTBOSS',
  MCX_BB_30: 'MCX_BB30',
  MCX_BB_15: 'MCX_BB15',
  NSE_BB_30: 'NSE_BB30',
}
const ORDER = ['FUDKII_RT', 'FUDKOI', 'PIVOTBOSS', 'NSE_BB_30', 'MCX_BB_30', 'MCX_BB_15']

const ist = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })

/** Bar timestamps land on bucket boundaries, so they never carry a second. The moment a book
 *  fired does — and it is the only one of the three that answers "when did this happen". */
const istPrecise = (ts: number) => {
  const d = new Date(ts * 1000)
  const hhmmss = d.toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })
  return `${hhmmss}.${String(d.getMilliseconds()).padStart(3, '0')}`
}

function dirTone(d: Alert['direction']) {
  return d === 'BULLISH' ? 'text-emerald-400' : d === 'BEARISH' ? 'text-rose-400' : 'text-slate-400'
}

function kindTone(k: Alert['kind']) {
  if (k === 'TRIGGER') return 'border-sky-500/40 bg-sky-500/10 text-sky-300'
  if (k === 'KEEPALIVE') return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400'
  return 'border-slate-600/40 bg-slate-800 text-slate-400'
}

const CTA_TONE: Record<string, string> = {
  PRIMARY: 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300',
  HOLD: 'border-indigo-500/40 bg-indigo-500/10 text-indigo-300',
  OBSERVE: 'border-sky-500/40 bg-sky-500/10 text-sky-300',
  WAIT_PULLBACK: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  AVOID: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
  STAND_DOWN: 'border-slate-600/50 bg-slate-800 text-slate-400',
}

const GRADE_TONE: Record<string, string> = {
  A: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
  B: 'bg-sky-500/20 text-sky-300 border-sky-500/40',
  C: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  F: 'bg-rose-500/20 text-rose-300 border-rose-500/40',
}

const f = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : v.toFixed(d)

/** Score dial 0-100. Half the card's information is "how strongly", so it gets a shape. */
function Score({ v }: { v: number }) {
  const pct = Math.max(0, Math.min(100, v))
  const tone = pct >= 70 ? 'text-emerald-400' : pct >= 40 ? 'text-sky-400' : 'text-slate-400'
  return (
    <div className="shrink-0 text-center">
      <div className={`text-2xl font-bold leading-none tabular-nums ${tone}`}>{pct.toFixed(0)}</div>
      <div className="mt-1 h-1 w-12 overflow-hidden rounded bg-slate-800">
        <div
          className={pct >= 70 ? 'h-full bg-emerald-500' : pct >= 40 ? 'h-full bg-sky-500' : 'h-full bg-slate-500'}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-0.5 text-[9px] uppercase tracking-wide text-slate-600">score</div>
    </div>
  )
}

function Ladder({ plan, direction }: { plan: Plan; direction: string }) {
  const ts = [plan.t1, plan.t2, plan.t3, plan.t4].filter((t): t is number => t !== null)
  return (
    <div className="rounded bg-slate-950/50 p-2">
      <div className="mb-1 flex items-baseline gap-2">
        <span className="text-[10px] uppercase tracking-wide text-slate-500">
          {plan.stopZone === 'inherited from the signal' ? 'trade plan (inherited)' : 'trade plan'}
        </span>
        {plan.grade && (
          <span className={`rounded border px-1 py-0.5 text-[9px] ${GRADE_TONE[plan.grade] ?? GRADE_TONE.F}`}>
            grade {plan.grade}
          </span>
        )}
        <span className="text-[10px] tabular-nums text-slate-400">{f(plan.rr)}R</span>
        {plan.atr !== null && (
          <span className="text-[10px] text-slate-600">ATR {f(plan.atr)}</span>
        )}
      </div>
      <div className="grid grid-cols-3 gap-x-3 gap-y-0.5 text-[11px] sm:grid-cols-6">
        <div>
          <span className="text-slate-500">entry </span>
          <span className="tabular-nums text-slate-200">{f(plan.entry)}</span>
        </div>
        <div>
          <span className="text-slate-500">SL </span>
          <span className="tabular-nums text-rose-400">{f(plan.sl)}</span>
        </div>
        {ts.map((t, i) => (
          <div key={i}>
            <span className="text-slate-500">T{i + 1} </span>
            <span className="tabular-nums text-emerald-400">{f(t)}</span>
          </div>
        ))}
        {ts.length === 0 && (
          <div className="col-span-4 text-slate-600">no wall ahead — {plan.note || 'no target'}</div>
        )}
      </div>
      <div className="mt-1 text-[10px] text-slate-600">
        {plan.hasPivots ? `stop at ${plan.stopZone}` : 'no zone behind close — stop fell back to ATR'}
        {plan.targetZones.length > 0 && ` · T1 at ${plan.targetZones[0]}`}
        {` · fortress ${f(plan.fortress)} · room ${f(plan.roomAtr)} ATR`}
        {direction ? '' : ''}
      </div>
    </div>
  )
}

function Option({ plan }: { plan: Plan }) {
  const l = plan.listed
  return (
    <div className="rounded bg-slate-950/50 p-2">
      <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">
        option leg — one strike OTM
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px]">
        <span className="rounded border border-slate-600/50 bg-slate-800 px-1.5 py-0.5 font-semibold text-slate-200">
          {plan.optionType} {plan.strike}
        </span>
        <span className="text-slate-600">step {plan.strikeInterval}</span>
        <span className="text-slate-500">
          δ≈<span className="tabular-nums text-slate-300">{f(plan.deltaApprox)}</span>
        </span>
        {l ? (
          <>
            <span className="text-slate-500">
              listed <span className="text-slate-300">{l.strike}</span>
              {l.strikeGapFromTheoretical !== 0 && (
                <span className="text-amber-400"> ({l.strikeGapFromTheoretical > 0 ? '+' : ''}{l.strikeGapFromTheoretical})</span>
              )}
            </span>
            <span className="text-slate-500">
              LTP{' '}
              {l.quotes ? (
                <span className="tabular-nums text-slate-200">{f(l.ltp)}</span>
              ) : (
                <span className="text-rose-400">no quote</span>
              )}
            </span>
            <span className="text-slate-600">lot {l.lotSize}</span>
            <span className="text-slate-600">exp {l.expiry}</span>
            {l.oi !== null && <span className="text-slate-600">OI {l.oi.toLocaleString('en-IN')}</span>}
          </>
        ) : (
          <span className="text-slate-600">no listed contract in the subscribed chain</span>
        )}
      </div>
      {l && !l.quotes && (
        <div className="mt-1 text-[10px] text-rose-400/80">
          This contract is not quoting — a stop checked against it could not be evaluated.
        </div>
      )}
      {l?.entry && (
        <div className="mt-2 border-t border-slate-800 pt-2">
          <div className="mb-1 flex items-baseline gap-2">
            <span className="text-[10px] uppercase tracking-wide text-slate-500">
              modelled entry
            </span>
            <span className="font-mono text-[10px] text-slate-500">
              {istPrecise(l.entry.ts)} IST
            </span>
            <span className="text-[10px] text-slate-600">
              +{l.entry.lagFromBarCloseS.toFixed(1)}s after the bar closed
            </span>
          </div>
          {l.entry.stale || l.entry.fill === null ? (
            <div className="text-[11px] text-rose-400/90">
              No entry price — {l.entry.note}. Pricing this off a stale quote would put the wrong
              premium on every downstream number.
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px]">
                <span className="rounded border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 font-semibold tabular-nums text-emerald-300">
                  fill {f(l.entry.fill)}
                </span>
                <span className="text-slate-500">
                  bid <span className="tabular-nums text-slate-400">{f(l.entry.bid)}</span>
                  {' / ask '}
                  <span className="tabular-nums text-slate-300">{f(l.entry.ask)}</span>
                  {' / ltp '}
                  <span className="tabular-nums text-slate-400">{f(l.entry.ltp)}</span>
                </span>
                {l.entry.slippageVsLtpPct !== null && (
                  <span className={l.entry.slippageVsLtpPct > 0 ? 'text-amber-400' : 'text-slate-500'}>
                    {l.entry.slippageVsLtpPct > 0 ? '+' : ''}
                    {l.entry.slippageVsLtpPct.toFixed(2)}% vs last trade
                  </span>
                )}
                <span className="text-slate-600">
                  {l.entry.qty} qty ({l.entry.lots} lot) · ₹
                  {l.entry.notional?.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                </span>
              </div>
              <div className="mt-0.5 text-[10px] text-slate-600">
                {l.entry.fillSource === 'ladder'
                  ? `walked ${l.entry.levelsWalked} ask level${l.entry.levelsWalked === 1 ? '' : 's'}`
                  : l.entry.fillSource}
                {l.entry.capped && <span className="text-amber-400"> · capped by the ladder ceiling</span>}
                {l.entry.quoteAgeS !== null && ` · quote ${l.entry.quoteAgeS.toFixed(1)}s old`}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}


const WALL_TONE: Record<string, string> = {
  FORTRESS: 'border-amber-400/50 bg-amber-400/15 text-amber-200',
  STRONG: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  AVERAGE: 'border-slate-500/40 bg-slate-700/50 text-slate-300',
  WEAK: 'border-slate-600/40 bg-slate-800/60 text-slate-400',
}

function WallChip({ w }: { w: WallJ }) {
  const supportive = w.side === 'BEHIND'
  return (
    <div
      className={`rounded border px-2 py-1.5 ${WALL_TONE[w.grade] ?? WALL_TONE.WEAK}`}
      title={`${w.levels} level${w.levels === 1 ? '' : 's'} · ${w.timeframes.join(', ')} · ${w.members.join(', ')} · needs ${w.min} to qualify`}
    >
      <div className="flex items-baseline gap-1.5 text-[11px]">
        <span className="font-semibold">
          {supportive ? 'Support behind' : 'Wall ahead'}
        </span>
        <span className="tabular-nums">{w.strength.toFixed(2)}</span>
        <span className="opacity-80">{w.grade}</span>
        {!w.qualifies && <span className="font-normal opacity-70">· needs {w.min}</span>}
      </div>
      <div className="mt-0.5 text-[10px] opacity-80">
        {w.price.toFixed(2)} · {w.distAtr.toFixed(2)}× ATR · {w.levels} lvl · {w.timeframes.join('+')}
      </div>
      <div className="text-[10px] opacity-60">
        {supportive ? 'structure under the stop' : 'price must pay through this'}
      </div>
    </div>
  )
}

function RtPanel({ c }: { c: RtCard }) {
  const st = c.stop
  const conf = c.confidence
  const sz = c.sizing
  return (
    <div className="mt-2 space-y-2 rounded border border-indigo-500/25 bg-indigo-500/[0.04] p-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[10px] uppercase tracking-wide text-indigo-300">FUDKII-RT</span>
        <span className="rounded border border-indigo-500/40 bg-indigo-500/10 px-1.5 py-0.5 text-[11px] font-bold tabular-nums text-indigo-200">
          confidence {conf.score.toFixed(0)}
        </span>
        {c.odds.pT1 !== null && (
          <span
            className="rounded bg-slate-800 px-1.5 py-0.5 text-[11px] tabular-nums text-slate-300"
            title={c.odds.note}
          >
            {c.odds.pT1.toFixed(0)}% to T1 first
          </span>
        )}
        {c.atr30m !== null && <span className="text-[10px] text-slate-600">ATR30m {f(c.atr30m)}</span>}
        {c.volumeBaseline && (
          <span
            className={`text-[10px] ${c.volumeBaseline.ratio > 1.5 ? 'text-emerald-400' : c.volumeBaseline.ratio < 0.85 ? 'text-rose-400' : 'text-slate-500'}`}
            title={`vs the median of ${c.volumeBaseline.sessions} earlier sessions at the same ${c.volumeBaseline.slot} slot`}
          >
            vol {c.volumeBaseline.ratio.toFixed(2)}× its own slot
          </span>
        )}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {c.wallBehind ? <WallChip w={c.wallBehind} /> : (
          <div className="rounded border border-rose-500/30 bg-rose-500/5 px-2 py-1.5 text-[11px] text-rose-300/80">
            No wall behind — nothing structural under the stop.
          </div>
        )}
        {c.wallAhead ? <WallChip w={c.wallAhead} /> : (
          <div className="rounded border border-emerald-500/30 bg-emerald-500/5 px-2 py-1.5 text-[11px] text-emerald-300/80">
            Clear ahead — no zone between price and open air.
          </div>
        )}
      </div>

      {st && (
        <div className={`rounded p-2 ${st.triggered ? 'border border-rose-500/50 bg-rose-500/10' : 'bg-slate-950/50'}`}>
          <div className="mb-1 flex flex-wrap items-baseline gap-2 text-[10px]">
            <span className="uppercase tracking-wide text-slate-500">stop — whichever hits first</span>
            {st.triggered ? (
              <span className="rounded bg-rose-500/25 px-1.5 py-0.5 font-bold text-rose-200">
                TRIGGERED by {st.triggeredBy}
              </span>
            ) : (
              <span className="text-slate-600">nearer: {st.triggersFirst ?? '—'}</span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2 text-[11px]">
            <div className={st.equityHit ? 'text-rose-300' : ''}>
              <span className="text-slate-500">equity </span>
              <span className="tabular-nums text-slate-200">{f(st.equityStop)}</span>
              {st.equityDistPct !== null && (
                <span className="text-slate-500"> · {st.equityDistPct.toFixed(2)}% away</span>
              )}
              <div className="text-[10px] text-slate-600">
                {st.basis} · {st.constantForSession ? 'fixed for the session' : 'refreshed'}
              </div>
            </div>
            <div className={st.optionHit ? 'text-rose-300' : ''}>
              <span className="text-slate-500">option </span>
              <span className="tabular-nums text-slate-200">{f(st.optionStop)}</span>
              {st.optionDistPct !== null && (
                <span className="text-slate-500"> · {st.optionDistPct.toFixed(2)}% away</span>
              )}
              <div className="text-[10px] text-slate-600">
                δ {st.delta.toFixed(2)} · restamped every {st.deltaRefreshS}s
              </div>
            </div>
          </div>
          <div className="mt-1 text-[10px] text-slate-600">
            The option leg stops the trade out on adverse movement even before the equity reaches
            its own level.
          </div>
        </div>
      )}

      {c.optionLadder.length > 0 && (
        <div className="rounded bg-slate-950/50 p-2">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">
            target ladder — pivot confluence walls
          </div>
          <div className="grid gap-x-3 gap-y-0.5 text-[11px] sm:grid-cols-2">
            {c.optionLadder.map((t) => (
              <div key={t.n}>
                <span className="text-slate-500">T{t.n} </span>
                <span className="tabular-nums text-emerald-400">{f(t.equity)}</span>
                <span className="text-slate-600"> → opt </span>
                <span className="tabular-nums text-emerald-300">{f(t.option)}</span>
                {t.optionGainPct !== null && (
                  <span className="text-slate-600"> (+{t.optionGainPct.toFixed(0)}%)</span>
                )}
              </div>
            ))}
          </div>
          <div className="mt-0.5 text-[10px] text-slate-600">
            delta-projected, linear — gamma makes this a floor on the upside, not a forecast
          </div>
        </div>
      )}

      <div className="rounded bg-slate-950/50 p-2">
        <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500">sizing</div>
        {sz.rejected ? (
          <div className="text-[11px] text-amber-300">
            Not sizeable — {sz.reason}
          </div>
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px]">
            <span className="rounded border border-slate-600/50 bg-slate-800 px-1.5 py-0.5 font-semibold text-slate-200">
              {sz.lots} lot{sz.lots === 1 ? '' : 's'} · {sz.qty} qty
            </span>
            <span className="text-slate-500">
              ₹{sz.capital.toLocaleString('en-IN', { maximumFractionDigits: 0 })} of ₹
              {sz.capPerTrade.toLocaleString('en-IN')}
            </span>
            <span className="text-slate-600">bound by {sz.binding}</span>
            <span className="text-slate-600">
              ₹{sz.unusedReturnedToWallet.toLocaleString('en-IN', { maximumFractionDigits: 0 })} back
              to the wallet
            </span>
            <span className="text-slate-600">
              {sz.slotsLeft}/{sz.maxConcurrent} slots free
            </span>
          </div>
        )}
      </div>

      {c.exitWalks && (
        <div className="rounded bg-slate-950/50 p-2">
          <div className="mb-1 flex items-baseline gap-2">
            <span className="text-[10px] uppercase tracking-wide text-slate-500">
              exit slippage — walked down the bid
            </span>
            {c.marksTs && (
              <span className="font-mono text-[10px] text-slate-600">
                marked {istPrecise(c.marksTs)}
              </span>
            )}
          </div>
          <div className="grid gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2">
            {([['T1 · 1 lot', c.exitWalks.t1_1lot], ['trail · 3 lots', c.exitWalks.trail_3lots]] as const).map(
              ([label, w]) => (
                <div key={label}>
                  <span className="text-slate-500">{label} </span>
                  {w.fill === null ? (
                    <span className="text-rose-400">no bid</span>
                  ) : (
                    <>
                      <span className="tabular-nums text-slate-200">{f(w.fill)}</span>
                      {w.slippageVsMidPct !== null && (
                        <span className={w.slippageVsMidPct < -1 ? 'text-amber-400' : 'text-slate-500'}>
                          {' '}{w.slippageVsMidPct.toFixed(2)}% vs mid
                        </span>
                      )}
                      <div className="text-[10px] text-slate-600">
                        {w.source === 'ladder'
                          ? `${w.levelsWalked} bid level${w.levelsWalked === 1 ? '' : 's'}`
                          : w.source}
                        {w.capped && <span className="text-amber-400"> · capped</span>}
                        {w.proceeds !== null &&
                          ` · ₹${w.proceeds.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`}
                      </div>
                    </>
                  )}
                </div>
              ),
            )}
          </div>
          <div className="mt-1 text-[10px] text-slate-600">
            An entry lifts the ask, an exit hits the bid — and the bid side thins first when the
            trade goes against you. Recomputed every second, not frozen at entry.
          </div>
        </div>
      )}

      <div className="text-[10px] text-slate-600">
        δ {c.greeks.delta.toFixed(2)} ({c.greeks.deltaSource})
        {c.greeks.dte !== null && ` · DTE ${c.greeks.dte}d`} · γ θ IV: {c.greeks.unavailable}
      </div>
    </div>
  )
}

function Row({ a }: { a: Alert }) {
  const [open, setOpen] = useState(false)
  const ev = Object.entries(a.evidence ?? {}).filter(([, v]) => v !== null && v !== undefined)
  const tone = CTA_TONE[a.cta?.action] ?? CTA_TONE.OBSERVE
  return (
    <div className="rounded-lg border border-slate-700/40 bg-slate-900/50 p-3">
      <div className="flex items-start gap-3">
        <Score v={a.score} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-base font-semibold text-slate-100">{a.symbol}</span>
            <span className={`text-xs font-medium ${dirTone(a.direction)}`}>{a.direction}</span>
            <span className={`rounded border px-1.5 py-0.5 text-[10px] ${kindTone(a.kind)}`}>
              {a.kind}
            </span>
            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
              {a.book} · {a.tf} · {a.exchange}
            </span>
            <span
              className="ml-auto font-mono text-[11px] text-slate-400"
              title={`bar ${ist(a.ts)}-${ist(a.barClose)} · fired ${istPrecise(a.firedAt)}`}
            >
              {a.firedAt ? istPrecise(a.firedAt) : ist(a.barClose || a.ts)} IST
            </span>
          </div>
          {a.company && <div className="truncate text-[11px] text-slate-600">{a.company}</div>}
          {a.kind !== 'TRIGGER' && a.evidence?.ageMinutes !== undefined && (
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px]">
              <span className="rounded border border-indigo-500/30 bg-indigo-500/10 px-1.5 py-0.5 text-indigo-300">
                entered {ist(a.ts - Number(a.evidence.ageMinutes) * 60)} IST
              </span>
              <span className="text-slate-500">
                {String(a.evidence.ageMinutes)}m ago · re-checked every 5m · TTL{' '}
                {String(a.evidence.ttlMinutes ?? 35)}m
              </span>
              <span className="text-slate-600">this row is a re-check, not an entry</span>
            </div>
          )}
          <div className="text-[10px] text-slate-600">
            {a.tf} bar {ist(a.ts)}–{ist(a.barClose)}
            {a.firedAt > a.barClose && a.barClose > 0 && (
              <span> · decided {(a.firedAt - a.barClose).toFixed(1)}s after the close</span>
            )}
          </div>
          <div className="mt-1 text-[11px] text-slate-400">{a.reason}</div>
          {a.cta?.text && (
            <div className={`mt-2 rounded border px-2 py-1 text-[11px] ${tone}`}>
              <span className="font-semibold">{a.cta.action.replace(/_/g, ' ')}</span>
              <span className="opacity-90"> — {a.cta.text}</span>
            </div>
          )}
        </div>
      </div>

      {a.plan && (
        <div className="mt-2 grid gap-2 lg:grid-cols-2">
          <Ladder plan={a.plan} direction={a.direction} />
          <Option plan={a.plan} />
        </div>
      )}
      {a.card && <RtPanel c={a.card} />}
      {!a.plan && a.kind === 'TRIGGER' && (
        <div className="mt-2 rounded bg-slate-950/50 p-2 text-[11px] text-slate-600">
          No stop/target ladder could be built — the engine had no pivot zones or no ATR for this
          name. Shown as absent rather than approximated.
        </div>
      )}

      <button
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-[11px] text-slate-500 hover:text-slate-300"
      >
        {open ? '▾' : '▸'} why it fired ({ev.length})
      </button>
      {open && ev.length > 0 && (
        <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-0.5 rounded bg-slate-950/50 p-2 sm:grid-cols-3">
          {ev.map(([k, v]) => (
            <div key={k} className="flex justify-between gap-2 text-[11px]">
              <span className="truncate font-mono text-slate-500">{k}</span>
              <span className="shrink-0 font-mono tabular-nums text-slate-300">
                {Array.isArray(v) ? v.join(', ') : String(v)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function Alerts() {
  const [book, setBook] = useState<string>('ALL')
  const path = book === 'ALL' ? '/api/alerts?limit=200' : `/api/alerts?book=${book}&limit=200`
  const { data, error } = usePoll<Resp>(path, 3000)

  if (error) {
    return (
      <div className="p-6">
        <h1 className="mb-4 text-2xl font-semibold text-slate-100">Live Alerts</h1>
        <div className="text-sm text-rose-400">Failed to load: {String(error)}</div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="p-6">
        <h1 className="mb-4 text-2xl font-semibold text-slate-100">Live Alerts</h1>
        <div className="text-sm text-slate-500">Loading…</div>
      </div>
    )
  }

  const total = Object.values(data.counts).reduce((a, b) => a + b, 0)

  return (
    <div className="p-6">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h1 className="text-2xl font-semibold text-slate-100">Live Alerts</h1>
        <div className="text-right text-xs text-slate-500">
          <div>
            {total} fired today · {data.living} living signals · {data.now_ist} IST
            {data.marksAgeS !== null && data.marksAgeS !== undefined && (
              <span className={data.marksAgeS > 5 ? ' text-amber-400' : ' text-emerald-400'}>
                {' '}· marks {data.marksAgeS.toFixed(1)}s old
              </span>
            )}
          </div>
          <div className="text-[10px] text-slate-600">
            advisory only — nothing here reaches the gateway
          </div>
        </div>
      </div>

      {Object.entries(data.capReached ?? {}).some(([, hit]) => hit) && (
        <div className="mb-3 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-300">
          {Object.entries(data.capReached)
            .filter(([, hit]) => hit)
            .map(([b]) => `${LABEL[b] ?? b} has hit its deployed daily cap`)
            .join('; ')}
          {' — '}
          {Object.entries(data.suppressedByCap ?? {})
            .filter(([, n]) => n > 0)
            .map(([b, n]) => `${n} further ${LABEL[b] ?? b} firings suppressed`)
            .join('; ')}
          . All 216 underlyings close the same 30m bar at once, so the cap is consumed in arrival
          order: what you see are the earliest that qualified, not the strongest.
        </div>
      )}

      <div className="mb-4 flex flex-wrap gap-1 border-b border-slate-800 pb-2">
        <button
          onClick={() => setBook('ALL')}
          className={`rounded px-2.5 py-1 text-xs ${
            book === 'ALL' ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-white'
          }`}
        >
          All ({total})
        </button>
        {ORDER.map((k) => {
          const n = data.counts[k] ?? 0
          const seen = data.evaluated[k] ?? 0
          return (
            <button
              key={k}
              onClick={() => setBook(k)}
              title={`${seen} bars evaluated`}
              className={`rounded px-2.5 py-1 text-xs ${
                book === k
                  ? 'bg-slate-800 text-white'
                  : n > 0
                    ? 'text-emerald-400/90 hover:text-emerald-300'
                    : 'text-slate-500 hover:text-slate-300'
              }`}
            >
              {LABEL[k] ?? k} ({n})
            </button>
          )
        })}
      </div>

      {data.alerts.length === 0 ? (
        <div className="rounded border border-slate-700/40 bg-slate-900/40 p-4 text-sm text-slate-500">
          {book === 'ALL' ? (
            <>
              Nothing has fired yet. The books evaluate on each closed bar —{' '}
              {Object.values(data.evaluated).reduce((a, b) => a + b, 0)} evaluations so far, so this
              is a quiet market rather than a dead page.
            </>
          ) : (
            <>
              {LABEL[book] ?? book} has not fired. It has evaluated{' '}
              <span className="text-slate-300">{data.evaluated[book] ?? 0}</span> bars — the
              distinction the old stack could not make, where a dead strategy and a quiet one looked
              identical.
            </>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          {data.alerts.map((a, i) => (
            <Row key={`${a.book}-${a.symbol}-${a.ts}-${i}`} a={a} />
          ))}
        </div>
      )}
    </div>
  )
}
