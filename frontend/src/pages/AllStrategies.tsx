import { useState } from 'react'
import { usePoll } from '../lib/usePoll'

// One tab per book the old stack ran, mirroring trading-dashboard's /strategy page.
//
// Two of them compute here; the rest are recorded with the parameters that were actually deployed
// and what a port would still need. A tab that is not ported says so — an empty tab fed by an
// endpoint returning [] looks exactly like a quiet market, which is how a dead strategy went
// unnoticed for six weeks in the old stack.

type Live = {
  config: Record<string, unknown>
  gates: { candidates: number; passed: number; by_gate: Record<string, { evaluated: number; rejected: number; binding: number }> }
  binding: { gate: string; count: number }[]
  wallet: { balance: number; deployed: number; realized_pnl: number; trades: number; wins: number; losses: number } | null
  pnl: unknown
  last_signal: Record<string, unknown> | null
  open_positions: number
}

type Book = {
  key: string
  label: string
  tf: string
  summary: string
  status: 'live' | 'not_ported'
  params: Record<string, string>
  paramsSource: string
  have: string[]
  need: string[]
  source: string
  note: string
  live?: Live
}

type Resp = {
  books: Book[]
  ported: number
  total: number
  mode: string
  universe: number
  segments: string[]
}

const n = (v: number | null | undefined, d = 0) =>
  v === null || v === undefined ? '—' : v.toLocaleString('en-IN', { maximumFractionDigits: d })

function StatusPill({ status }: { status: Book['status'] }) {
  return status === 'live' ? (
    <span className="rounded border border-emerald-500/40 bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300">
      live here
    </span>
  ) : (
    <span className="rounded border border-slate-600/50 bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
      not ported
    </span>
  )
}

function Params({ book }: { book: Book }) {
  const entries = Object.entries(book.params)
  if (entries.length === 0) {
    return (
      <div className="text-[11px] text-slate-500">
        No deployed configuration exists for this book anywhere in the old stack.
      </div>
    )
  }
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold text-slate-400">
        Deployed parameters {book.status === 'live' && <span className="text-slate-600">(old stack)</span>}
      </div>
      <div className="overflow-x-auto rounded bg-slate-950/50">
        <table className="w-full text-[11px]">
          <tbody>
            {entries.map(([k, v]) => (
              <tr key={k} className="border-b border-slate-800/60 last:border-0">
                <td className="px-2 py-1 font-mono text-slate-400">{k}</td>
                <td className="px-2 py-1 text-right font-mono tabular-nums text-slate-200">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {book.paramsSource && (
        <div className="mt-1 font-mono text-[10px] text-slate-600">{book.paramsSource}</div>
      )}
    </div>
  )
}

function LivePanel({ live }: { live: Live }) {
  const g = live.gates ?? { candidates: 0, passed: 0, by_gate: {} }
  const gates = Object.entries(g.by_gate ?? {})
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-2">
        {[
          ['candidates', g.candidates],
          ['passed', g.passed],
          ['open', live.open_positions],
          ['trades', live.wallet?.trades ?? 0],
        ].map(([label, v]) => (
          <div key={String(label)} className="rounded border border-slate-700/40 bg-slate-900/50 p-2">
            <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
            <div className="text-lg font-semibold tabular-nums text-slate-200">{n(Number(v))}</div>
          </div>
        ))}
      </div>

      {live.wallet && (
        <div className="rounded border border-slate-700/40 bg-slate-900/50 p-2 text-[11px]">
          <span className="text-slate-500">wallet </span>
          <span className="tabular-nums text-slate-200">₹{n(live.wallet.balance)}</span>
          <span className="text-slate-500"> · deployed </span>
          <span className="tabular-nums text-slate-300">₹{n(live.wallet.deployed)}</span>
          <span className="text-slate-500"> · realised </span>
          <span
            className={`tabular-nums ${live.wallet.realized_pnl > 0 ? 'text-emerald-400' : live.wallet.realized_pnl < 0 ? 'text-rose-400' : 'text-slate-300'}`}
          >
            {live.wallet.realized_pnl >= 0 ? '+' : ''}₹{n(live.wallet.realized_pnl, 2)}
          </span>
          <span className="text-slate-500">
            {' '}· {live.wallet.wins}W / {live.wallet.losses}L
          </span>
        </div>
      )}

      {gates.length > 0 && (
        <div>
          <div className="mb-1 text-[11px] font-semibold text-slate-400">
            Gate funnel — every candidate recorded, with the gate that killed it
          </div>
          <div className="overflow-x-auto rounded bg-slate-950/50">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-slate-500">
                  <th className="px-2 py-1 text-left font-normal">gate</th>
                  <th className="px-2 py-1 text-right font-normal">evaluated</th>
                  <th className="px-2 py-1 text-right font-normal">rejected</th>
                  <th className="px-2 py-1 text-right font-normal">binding</th>
                </tr>
              </thead>
              <tbody>
                {gates
                  .sort((a, b) => b[1].binding - a[1].binding)
                  .map(([name, s]) => (
                    <tr key={name} className="border-b border-slate-800/60 last:border-0">
                      <td className="px-2 py-1 font-mono text-slate-300">{name}</td>
                      <td className="px-2 py-1 text-right tabular-nums text-slate-400">{s.evaluated}</td>
                      <td className="px-2 py-1 text-right tabular-nums text-slate-400">{s.rejected}</td>
                      <td
                        className={`px-2 py-1 text-right tabular-nums ${s.binding > 0 ? 'text-amber-400' : 'text-slate-600'}`}
                      >
                        {s.binding}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="rounded border border-slate-700/40 bg-slate-900/50 p-2 text-[11px]">
        <span className="text-slate-500">last signal: </span>
        {live.last_signal ? (
          <span className="text-slate-300">
            {String(live.last_signal.symbol)} {String(live.last_signal.direction)}
            {live.last_signal.grade ? ` · grade ${String(live.last_signal.grade)}` : ''}
            {live.last_signal.decision ? ` · ${String(live.last_signal.decision)}` : ''}
          </span>
        ) : (
          <span className="text-amber-400">
            none ever — the cheapest liveness test there is, and the old stack failed it silently
          </span>
        )}
      </div>

      {Object.keys(live.config ?? {}).length > 0 && (
        <div>
          <div className="mb-1 text-[11px] font-semibold text-slate-400">Running configuration</div>
          <div className="overflow-x-auto rounded bg-slate-950/50">
            <table className="w-full text-[11px]">
              <tbody>
                {Object.entries(live.config).map(([k, v]) => (
                  <tr key={k} className="border-b border-slate-800/60 last:border-0">
                    <td className="px-2 py-1 font-mono text-slate-400">{k}</td>
                    <td className="px-2 py-1 text-right font-mono tabular-nums text-slate-200">
                      {String(v)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

function BookPanel({ book }: { book: Book }) {
  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-baseline gap-2">
          <h2 className="text-xl font-semibold text-slate-100">{book.label}</h2>
          <StatusPill status={book.status} />
          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
            {book.tf}
          </span>
        </div>
        <p className="mt-1 text-sm text-slate-400">{book.summary}</p>
        {book.note && (
          <p className="mt-2 rounded border border-slate-700/40 bg-slate-900/40 p-2 text-[11px] leading-relaxed text-slate-400">
            {book.note}
          </p>
        )}
      </div>

      {book.status === 'live' && book.live ? (
        <LivePanel live={book.live} />
      ) : (
        <div className="rounded border border-slate-700/40 bg-slate-900/40 p-3">
          <div className="text-[11px] font-semibold text-slate-300">
            Not computed by this engine — so there is no state to show, and none is invented.
          </div>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-emerald-400/80">
                already available here
              </div>
              <ul className="space-y-0.5 text-[11px] text-slate-400">
                {book.have.map((h) => (
                  <li key={h}>· {h}</li>
                ))}
              </ul>
            </div>
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-amber-400/80">
                still needed
              </div>
              <ul className="space-y-0.5 text-[11px] text-slate-400">
                {book.need.length === 0 ? (
                  <li className="text-slate-600">—</li>
                ) : (
                  book.need.map((x) => <li key={x}>· {x}</li>)
                )}
              </ul>
            </div>
          </div>
        </div>
      )}

      <Params book={book} />

      {book.source && (
        <div className="font-mono text-[10px] text-slate-600">source: {book.source}</div>
      )}
    </div>
  )
}

export function AllStrategies() {
  const { data, error } = usePoll<Resp>('/api/all-strategies', 5000)
  const [active, setActive] = useState<string>('FUDKII')

  if (error) {
    return (
      <div className="p-6">
        <h1 className="mb-4 text-2xl font-semibold text-slate-100">All Strategies</h1>
        <div className="text-sm text-rose-400">Failed to load: {String(error)}</div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="p-6">
        <h1 className="mb-4 text-2xl font-semibold text-slate-100">All Strategies</h1>
        <div className="text-sm text-slate-500">Loading…</div>
      </div>
    )
  }

  const book = data.books.find((b) => b.key === active) ?? data.books[0]

  return (
    <div className="p-6">
      <div className="mb-4 flex items-baseline justify-between gap-4">
        <h1 className="text-2xl font-semibold text-slate-100">All Strategies</h1>
        <div className="text-right text-xs text-slate-500">
          <div>
            <span className="text-emerald-400">{data.ported}</span> of {data.total} books compute
            here
          </div>
          <div className="text-[10px] text-slate-600">
            {data.mode} · {data.segments.join(', ')} · {data.universe} underlyings
          </div>
        </div>
      </div>

      <div className="mb-4 flex flex-wrap gap-1 border-b border-slate-800 pb-2">
        {data.books.map((b) => (
          <button
            key={b.key}
            onClick={() => setActive(b.key)}
            className={`rounded px-2.5 py-1 text-xs ${
              b.key === active
                ? 'bg-slate-800 text-white'
                : b.status === 'live'
                  ? 'text-emerald-400/80 hover:text-emerald-300'
                  : 'text-slate-500 hover:text-slate-300'
            }`}
          >
            {b.label}
            {b.status === 'live' && <span className="ml-1 text-[9px] text-emerald-500">●</span>}
          </button>
        ))}
      </div>

      {book && <BookPanel book={book} />}
    </div>
  )
}
