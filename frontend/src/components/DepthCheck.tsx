import type { ReactNode } from 'react'
import { cls, fmt, ist } from '../lib/api'
import type { DepthCheck, DepthCheckView, DepthOrderCheck, DepthVerdict } from '../types'
import { Badge } from './Ui'

// The depth check (operator, 2026-10-09: "go ahead build it n tell me where to view it"): every paper fill against the
// five-level book recorded beside it. The paper matcher fills a resting order WHOLE at its price; when fewer lots were
// on offer there, the rest would have traded further down the book. A record only — it moves no order, stop or wallet.

const TONE: Record<DepthVerdict, 'green' | 'amber' | 'red' | 'slate'> = { full: 'green', partial: 'amber', thin: 'red', 'no book': 'slate' }
const MEANS: Record<DepthVerdict, string> = {
  full: 'the book held every lot at the price',
  partial: 'some lots were on offer at the price, not all',
  thin: 'not one whole lot was on offer at the price',
  'no book': 'no depth book was recorded for this fill',
}

export function VerdictChip({ v }: { v: DepthVerdict }) {
  return (
    <span title={MEANS[v]} className="whitespace-nowrap">
      <Badge tone={TONE[v]}>{v}</Badge>
    </span>
  )
}

/** Rupees the paper fill was kinder than the book: amber when it was, quiet at zero. */
export function Kinder({ rs, className }: { rs: number | null | undefined; className?: string }) {
  if (rs == null) return <span className="text-slate-600">DM</span>
  return <span className={cls(rs > 0.5 ? 'text-amber-300' : rs < -0.5 ? 'text-sky-300' : 'text-slate-500', className)}>{fmt.inr(rs)}</span>
}

/** The Trades page's Depth cell: the trade's total and the worst verdict of its fills; a click opens the fills. */
export function DepthCell({ d, open, onToggle }: { d: DepthCheck | null | undefined; open: boolean; onToggle: () => void }) {
  if (!d) return <td className="px-2 py-1.5 text-slate-600">—</td>
  return (
    <td className="px-2 py-1.5">
      <button
        onClick={onToggle}
        className="flex items-center gap-1.5 whitespace-nowrap text-left hover:opacity-80"
        title={d.checked ? `the paper fills were ${fmt.inr(d.optimismRs)} kinder than the recorded book — ${d.checked} of ${d.fills} fills had a book; click for each fill` : 'no fill of this trade recorded a depth book'}
      >
        {d.checked ? <Kinder rs={d.optimismRs} /> : null}
        <VerdictChip v={d.verdict} />
        <span className="text-[10px] text-slate-600">{open ? '▾' : '▸'}</span>
      </button>
    </td>
  )
}

function lots(n: number | null | undefined, lot: number) {
  if (n == null) return 'DM'
  const unit = lot > 1 ? (n === 1 ? 'lot' : 'lots') : 'units'
  return `${fmt.int(n)} ${unit}`
}

function bookLabel(o: DepthOrderCheck) {
  if (!o.source) return 'none'
  const at = o.bookField === 'depthAtCross' ? 'at the cross' : o.source === 'fill' ? 'at the fill' : 'at placement'
  return `${at}${o.ageMs != null ? ` · ${(o.ageMs / 1000).toFixed(1)} s old` : ''}`
}

function legName(o: DepthOrderCheck) {
  if (o.purpose === 'ENTRY') return 'entry'
  if (o.kind === 'target') return 'target sell'
  return o.walked ? 'exit, walked' : 'exit'
}

/** A trade's fills, one line each: what the book held at the price, what the fill would have cost against it. */
export function DepthDetail({ d, colSpan }: { d: DepthCheck; colSpan: number }) {
  const fills = [...d.entry.orders, ...d.exit.orders]
  return (
    <tr className="border-b border-slate-900 bg-slate-950/50">
      <td colSpan={colSpan} className="px-3 py-2">
        <div className="mb-1.5 text-[11px] text-slate-400">
          Depth check — each fill against the book recorded beside it. Entry <Kinder rs={d.entry.optimismRs} /> · exits <Kinder rs={d.exit.optimismRs} /> ·
          total <Kinder rs={d.optimismRs} className="font-semibold" /> the paper fills were kinder than the book
          {d.checked < d.fills && <span className="text-slate-500"> · {d.fills - d.checked} of {d.fills} fills had no book</span>}
        </div>
        <table className="text-[11px]">
          <thead>
            <tr className="text-left text-slate-500">
              {['Fill', 'Time', 'Side', 'Lots', 'Paper price', 'On offer at the price', 'Short by', 'Realistic price', 'Kinder by', 'Book', 'Verdict', ''].map((h) => (
                <th key={h} className="whitespace-nowrap px-2 py-1 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {fills.map((o) => (
              <tr key={o.orderId} className="border-t border-slate-900 align-top">
                <td className="whitespace-nowrap px-2 py-1 text-slate-300" title={o.clientOrderId}>{legName(o)}</td>
                <td className="whitespace-nowrap px-2 py-1 text-slate-500">{ist(o.ts, true)}</td>
                <td className="px-2 py-1 text-slate-400">{o.side ?? 'DM'}</td>
                <td className="whitespace-nowrap px-2 py-1">{lots(o.lots, o.lotSize)}</td>
                <td className="px-2 py-1">{fmt.n(o.paperPrice)}</td>
                <td className="whitespace-nowrap px-2 py-1">{o.source ? (o.walked ? `${lots(o.visibleLots, o.lotSize)} in the book` : lots(o.visibleLots, o.lotSize)) : '—'}</td>
                <td className={cls('whitespace-nowrap px-2 py-1', o.shortfallLots ? 'text-amber-300' : 'text-slate-500')}>{o.source ? lots(o.shortfallLots, o.lotSize) : '—'}</td>
                <td className="px-2 py-1">
                  {o.source ? fmt.n(o.realisticPrice) : '—'}
                  {o.beyondBook && <span className="ml-1 text-[9px] text-rose-300" title="the five levels did not hold the order: the rest is priced at the last level">beyond the book</span>}
                </td>
                <td className="px-2 py-1">{o.source ? <Kinder rs={o.optimismRs} /> : '—'}</td>
                <td className="whitespace-nowrap px-2 py-1 text-slate-500">{bookLabel(o)}</td>
                <td className="px-2 py-1"><VerdictChip v={o.verdict} /></td>
                <td className="px-2 py-1 text-slate-500">{o.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </td>
    </tr>
  )
}

/** The Overview's card: per book, how many fills the book was checked against, how many were short, and the rupees. */
export function DepthCheckTable({ data }: { data: DepthCheckView | null }) {
  if (!data) return <div className="text-xs text-slate-500">loading…</div>
  if (!data.since || !data.total || data.books.length === 0) {
    return <div className="text-xs text-slate-500">{data.scope === 'today' ? 'no trade has closed today' : 'no order has recorded a depth book yet'}</div>
  }
  const t = data.total
  const row = (key: string, label: ReactNode, s: typeof t, total?: boolean) => (
    <tr key={key} className={cls('border-b border-slate-900', total && 'border-t-2 border-t-slate-700 font-semibold')}>
      <td className={cls('whitespace-nowrap px-2 py-1.5', total ? 'text-slate-100' : 'text-slate-300')}>{label}</td>
      <td className="px-2 py-1.5">
        {s.checked}
        {s.checked < s.fills && <span className="text-slate-600"> / {s.fills}</span>}
      </td>
      <td className="px-2 py-1.5 text-emerald-400/80">{s.byVerdict.full}</td>
      <td className={cls('px-2 py-1.5', s.byVerdict.partial ? 'text-amber-300' : 'text-slate-600')}>{s.byVerdict.partial}</td>
      <td className={cls('px-2 py-1.5', s.byVerdict.thin ? 'text-rose-300' : 'text-slate-600')}>{s.byVerdict.thin}</td>
      <td className="px-2 py-1.5"><Kinder rs={s.optimismRs} /></td>
    </tr>
  )
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-slate-800 text-left text-slate-500">
            <th className="px-2 py-1.5 font-medium">Book</th>
            <th className="px-2 py-1.5 font-medium" title="fills that recorded a depth book / every fill">Fills checked</th>
            <th className="px-2 py-1.5 font-medium" title={MEANS.full}>Full</th>
            <th className="px-2 py-1.5 font-medium" title={MEANS.partial}>Partial</th>
            <th className="px-2 py-1.5 font-medium" title={MEANS.thin}>Thin</th>
            <th className="px-2 py-1.5 font-medium" title="what the paper fills gained over the same fills priced against the recorded book">Paper kinder by</th>
          </tr>
        </thead>
        <tbody>
          {data.books.map((b) => row(b.book, b.shadow ? <span className="text-slate-500">{b.label}</span> : b.label, b))}
          {data.books.length > 1 && row('total', 'all books shown', t, true)}
        </tbody>
      </table>
      {t.worst.length > 0 && t.worst[0].optimismRs > 0 && (
        <div className="mt-2 text-[11px] text-slate-400">
          <span className="text-slate-500">Most kinder: </span>
          {t.worst.slice(0, 5).filter((w) => w.optimismRs > 0).map((w, i) => (
            <span key={w.id}>
              {i > 0 && ' · '}
              {w.underlying} <Kinder rs={w.optimismRs} /> <span className="text-slate-600">({w.verdict})</span>
            </span>
          ))}
        </div>
      )}
      <div className="mt-2 text-[10px] text-slate-500">
        {data.scope === 'today' ? 'Trades closed today' : `Every trade since the first order recorded a depth book (${ist(data.since)})`}. Paper fills a resting order whole at its
        price; when fewer lots sat at that price in the recorded five levels, the rest is priced further down the book. Record only — it changes no order,
        stop or wallet. Each trade&apos;s fills are on the Trades page (Depth column).
      </div>
    </div>
  )
}
