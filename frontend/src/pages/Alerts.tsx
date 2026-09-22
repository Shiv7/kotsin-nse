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

function Row({ a }: { a: Alert }) {
  const [open, setOpen] = useState(false)
  const ev = Object.entries(a.evidence ?? {}).filter(([, v]) => v !== null && v !== undefined)
  return (
    <div className="rounded border border-slate-700/40 bg-slate-900/50">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-3 p-2 text-left hover:bg-slate-800/40"
      >
        <span className="w-14 shrink-0 font-mono text-[11px] text-slate-500">{ist(a.ts)}</span>
        <span className="w-28 shrink-0 truncate text-sm font-semibold text-slate-100">
          {a.symbol}
        </span>
        <span className={`w-16 shrink-0 text-xs font-medium ${dirTone(a.direction)}`}>
          {a.direction}
        </span>
        <span
          className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${kindTone(a.kind)}`}
        >
          {a.kind}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">{a.reason}</span>
        <span className="w-12 shrink-0 text-right text-xs tabular-nums text-slate-300">
          {a.score.toFixed(0)}
        </span>
        <span className="w-16 shrink-0 text-right text-xs tabular-nums text-slate-400">
          {a.price.toFixed(2)}
        </span>
      </button>
      {open && ev.length > 0 && (
        <div className="border-t border-slate-800 p-2">
          <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-3">
            {ev.map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2 text-[11px]">
                <span className="truncate font-mono text-slate-500">{k}</span>
                <span className="shrink-0 font-mono tabular-nums text-slate-300">
                  {Array.isArray(v) ? v.join(', ') : String(v)}
                </span>
              </div>
            ))}
          </div>
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
        <div className="space-y-1">
          <div className="flex gap-3 px-2 text-[10px] uppercase tracking-wide text-slate-600">
            <span className="w-14 shrink-0">time</span>
            <span className="w-28 shrink-0">symbol</span>
            <span className="w-16 shrink-0">dir</span>
            <span className="w-[4.5rem] shrink-0">kind</span>
            <span className="min-w-0 flex-1">why it fired</span>
            <span className="w-12 shrink-0 text-right">score</span>
            <span className="w-16 shrink-0 text-right">price</span>
          </div>
          {data.alerts.map((a, i) => (
            <Row key={`${a.book}-${a.symbol}-${a.ts}-${i}`} a={a} />
          ))}
        </div>
      )}
    </div>
  )
}
