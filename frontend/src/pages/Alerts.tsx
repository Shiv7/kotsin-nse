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
  cta: { action: string; text: string }
  plan: Plan | null
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
  } | null
}

type Resp = {
  alerts: Alert[]
  counts: Record<string, number>
  evaluated: Record<string, number>
  living: number
  books: string[]
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
        <span className="text-[10px] uppercase tracking-wide text-slate-500">trade plan</span>
        <span className={`rounded border px-1 py-0.5 text-[9px] ${GRADE_TONE[plan.grade] ?? GRADE_TONE.F}`}>
          grade {plan.grade}
        </span>
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
            <span className="ml-auto font-mono text-[11px] text-slate-500">{ist(a.ts)} IST</span>
          </div>
          {a.company && <div className="truncate text-[11px] text-slate-600">{a.company}</div>}
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
