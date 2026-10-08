import { useState } from 'react'
import { cls, fmt, ist, istTime } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { DepthCheck, DepthOrderCheck, DepthSummary, DepthVerdict } from '../types'
import { Badge, Table } from './Ui'

// The depth check (operator, 2026-10-09: "go ahead build it n tell me where to view it"). The paper matcher fills a
// resting order WHOLE at its limit, with no queue and no share of the depth. Each fill is set against the book
// recorded with it: how many lots it showed at the price, what the whole order would really have averaged, and how
// many rupees kinder the paper fill was. Record-only: nothing here changes an order, a fill, a stop or a wallet.

const VERDICT_TONE: Record<DepthVerdict, 'green' | 'amber' | 'red' | 'slate'> = { full: 'green', partial: 'amber', thin: 'red', 'no book': 'slate' }
const VERDICT_TIP: Record<DepthVerdict, string> = {
  full: 'the book showed every lot we traded at the price or better',
  partial: 'the book showed at least half the lots at the price; the rest would have walked to worse levels',
  thin: 'the book showed under half the lots at the price; most of the order would have walked to worse levels',
  'no book': 'no book was recorded with this fill (fills before 3 Oct 17:51, some exits), so it cannot be judged',
}

export const DEPTH_TIP =
  'Each paper fill set against the book recorded with it. full: the book showed every lot at the price · partial: at least half · thin: under half · no book: none recorded. ' +
  '₹: how much kinder the paper fills were than walking that book (positive: the paper flattered the trade). Record-only. Click a row\'s Depth cell for each fill.'

export function DepthChip({ verdict }: { verdict: DepthVerdict }) {
  return (
    <span title={VERDICT_TIP[verdict]} className="whitespace-nowrap">
      <Badge tone={VERDICT_TONE[verdict]}>{verdict}</Badge>
    </span>
  )
}

/** Rupees the paper was kinder: amber when it flattered the trade, quiet when it did not. */
export function Kinder({ rs, className }: { rs: number | null | undefined; className?: string }) {
  if (rs == null) return <span className={cls('text-slate-600', className)}>—</span>
  const tone = rs >= 1 ? 'text-amber-300' : rs <= -1 ? 'text-sky-300' : 'text-slate-400'
  return (
    <span className={cls('tabular-nums', tone, className)} title={rs >= 0 ? `the paper fills were ${fmt.inr(rs)} kinder than the book` : `the paper fills were ${fmt.inr(-rs)} harsher than the book`}>
      {rs < 0 ? '−' : ''}
      {fmt.inr(Math.abs(rs))}
    </span>
  )
}

/** The trade ledger's Depth cell: the verdict and the rupees; a click opens each fill beneath the row. */
export function DepthCell({ dc, open, onToggle }: { dc: DepthCheck | null | undefined; open: boolean; onToggle: () => void }) {
  if (!dc) return <td className="px-2 py-1.5 text-slate-600">—</td>
  return (
    <td className="px-2 py-1.5">
      <button onClick={onToggle} className={cls('flex items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-slate-800', open && 'bg-slate-800')} aria-expanded={open}>
        <DepthChip verdict={dc.verdict} />
        {dc.checked > 0 && <Kinder rs={dc.optimismRs} />}
        <span className="text-[10px] text-slate-500">{open ? '▴' : '▾'}</span>
      </button>
      {dc.fills > dc.checked && dc.checked > 0 && <div className="text-[10px] text-slate-600">{dc.fills - dc.checked} of {dc.fills} fills without a book</div>}
    </td>
  )
}

function lotsText(n: number | null | undefined, lotSize: number) {
  if (n == null) return 'DM'
  const unit = lotSize > 1 ? 'lot' : 'unit'
  return `${n.toLocaleString('en-IN')} ${unit}${n === 1 ? '' : 's'}`
}

function orderName(o: DepthOrderCheck) {
  if (o.purpose === 'ENTRY') return o.market ? 'entry · market' : 'entry'
  if (o.kind === 'target') return 'target'
  if (o.crossed) return 'exit · crossed'
  if (o.market) return 'exit · market'
  return 'exit'
}

function bookText(o: DepthOrderCheck) {
  if (!o.source) return 'none'
  const at = o.source === 'fill' ? 'at the fill' : o.source === 'cross' ? 'walked at the cross' : 'at placement'
  return o.ageMs == null ? at : `${at} · ${(o.ageMs / 1000).toFixed(1)} s old`
}

const DETAIL_HEAD = ['Fill', 'Time', 'Side', 'Lots', 'Paper price', 'Book', 'Shown at the price', 'Short', 'Realistic price', 'Paper kinder by', 'Verdict']

/** One trade's fills, each against its book: the lots the book showed at the paper price, the shortfall, the price
 *  the whole order would really have averaged, the book's age and which book it was. */
export function DepthDetail({ dc }: { dc: DepthCheck }) {
  const orders = [...dc.entry.orders, ...dc.exit.orders]
  return (
    <div className="space-y-2 bg-slate-950/60 px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400">
        <span>
          Entry <DepthChip verdict={dc.entry.verdict} /> <Kinder rs={dc.entry.optimismRs} />
          {dc.entryFrom && <span className="ml-1 text-slate-500">(copied from {dc.entryFromLabel ?? dc.entryFrom}'s fill)</span>}
        </span>
        <span>
          Exits ({dc.exit.fills}) <DepthChip verdict={dc.exit.verdict} /> <Kinder rs={dc.exit.optimismRs} />
        </span>
        <span>
          Trade <Kinder rs={dc.optimismRs} className="font-semibold" /> {dc.optimismRs != null && (dc.optimismRs >= 0 ? 'kinder on paper than the book' : 'harsher on paper than the book')}
        </span>
        <span className="text-slate-600">lot {dc.lotSize.toLocaleString('en-IN')} · record-only: nothing here changed an order, a fill, a stop or a wallet</span>
      </div>
      {orders.length === 0 ? (
        <div className="text-[11px] text-slate-500">no filled order found for this trade</div>
      ) : (
        <Table head={DETAIL_HEAD}>
          {orders.map((o) => (
            <tr key={o.orderId} className="border-b border-slate-900 align-top">
              <td className="px-2 py-1 text-slate-300">
                <div className="whitespace-nowrap">{orderName(o)}</div>
                <div className="max-w-[28rem] text-[10px] text-slate-500">{o.words}</div>
              </td>
              <td className="whitespace-nowrap px-2 py-1 text-slate-500">{istTime(o.filledTs)}</td>
              <td className="px-2 py-1 text-slate-400">{o.side}</td>
              <td className="whitespace-nowrap px-2 py-1">{lotsText(o.lotSize > 1 ? o.lots : o.qty, o.lotSize)}</td>
              <td className="px-2 py-1 tabular-nums">{fmt.n(o.paperPrice)}</td>
              <td className={cls('whitespace-nowrap px-2 py-1', o.stale ? 'text-amber-400' : 'text-slate-400')} title={o.stale ? 'older than the paper matcher\'s own 6 s limit: the market may have moved' : undefined}>
                {bookText(o)}
                {o.stale && ' · stale'}
              </td>
              <td className="whitespace-nowrap px-2 py-1">
                {o.verdict === 'no book' ? '—' : lotsText(o.lotSize > 1 ? o.visibleLots : o.visibleQty, o.lotSize)}
                {o.touch != null && (
                  <div className="text-[10px] text-slate-500">
                    {o.side === 'BUY' ? 'ask' : 'bid'} {fmt.n(o.touch)} × {(o.touchQty ?? 0).toLocaleString('en-IN')}
                  </div>
                )}
              </td>
              <td className={cls('px-2 py-1', (o.shortfallLots ?? 0) > 0 ? 'text-rose-300' : 'text-slate-500')}>
                {o.shortfallLots == null ? '—' : lotsText(o.shortfallLots, o.lotSize)}
                {o.beyondBook && <div className="text-[10px] text-slate-500">past the book</div>}
              </td>
              <td className="px-2 py-1 tabular-nums">{fmt.n(o.realisticPrice)}</td>
              <td className="px-2 py-1">
                <Kinder rs={o.optimismRs} />
              </td>
              <td className="px-2 py-1">
                <DepthChip verdict={o.verdict} />
                {o.onPrint && <div className="text-[10px] text-slate-500" title="the touch never reached the price: a trade printing through it filled the order">on a print</div>}
              </td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  )
}

const SCOPES: [string, string][] = [
  ['today', 'today'],
  ['start', 'since the start'],
]
const SETS: [string, string][] = [
  ['trading', 'trading books'],
  ['shadows', 'shadows'],
  ['mirrors', 'stop-rule mirrors'],
  ['all', 'all'],
]

function Toggle({ value, options, onChange }: { value: string; options: [string, string][]; onChange: (v: string) => void }) {
  return (
    <span className="flex items-center gap-1">
      {options.map(([k, label]) => (
        <button
          key={k}
          onClick={() => onChange(k)}
          className={cls('rounded px-2 py-0.5 text-[11px]', value === k ? 'bg-slate-700 text-white' : 'bg-slate-800 text-slate-400 hover:text-white')}
        >
          {label}
        </button>
      ))}
    </span>
  )
}

/** The Overview's card: per book, the closed trades' fills checked against their books, how many were partial or
 *  thin, and the rupees the paper fills were kinder than those books. */
export function DepthCheckCard() {
  const [since, setSince] = useState('today')
  const [books, setBooks] = useState('trading')
  const [showWorst, setShowWorst] = useState(false)
  const { data, error } = usePoll<DepthSummary>(`/api/depth-check?since=${since}&books=${books}`, 30000)
  const rows = data?.books ?? []
  const t = data?.total
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-3 py-2">
        <h2 className="text-sm font-semibold text-slate-200" title={DEPTH_TIP}>
          Depth check · paper fills vs the recorded book
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <Toggle value={since} options={SCOPES} onChange={setSince} />
          <Toggle value={books} options={SETS} onChange={setBooks} />
        </div>
      </header>
      <div className="space-y-2 p-3">
        {error && <div className="text-xs text-rose-300">{error}</div>}
        {!data ? (
          <div className="text-xs text-slate-500">loading…</div>
        ) : rows.length === 0 ? (
          <div className="text-xs text-slate-500">{since === 'today' ? 'no trade closed yet today' : 'no closed trade in these books'}</div>
        ) : (
          <>
            <Table head={['Book', 'Trades', 'Fills checked', 'Partial', 'Thin', 'Paper kinder by']}>
              {[
                ...rows.map((b) => (
                  <tr key={b.book} className="border-b border-slate-900">
                    <td className="whitespace-nowrap px-2 py-1.5 text-slate-300">
                      {b.label}
                      {b.shadow && <span className="ml-1 text-[10px] text-slate-500">shadow</span>}
                    </td>
                    <td className="px-2 py-1.5">{b.trades}</td>
                    <td className="px-2 py-1.5" title={`${b.byVerdict.full} full · ${b.byVerdict.partial} partial · ${b.byVerdict.thin} thin · ${b.byVerdict['no book']} without a book`}>
                      {b.checked} <span className="text-slate-500">of {b.fills}</span>
                      {b.stale > 0 && <span className="ml-1 text-[10px] text-amber-400/80">{b.stale} stale</span>}
                    </td>
                    <td className={cls('px-2 py-1.5', b.byVerdict.partial ? 'text-amber-300' : 'text-slate-600')}>{b.byVerdict.partial}</td>
                    <td className={cls('px-2 py-1.5', b.byVerdict.thin ? 'text-rose-300' : 'text-slate-600')}>{b.byVerdict.thin}</td>
                    <td className="px-2 py-1.5">
                      <Kinder rs={b.checked ? b.optimismRs : null} />
                    </td>
                  </tr>
                )),
                t && rows.length > 1 && (
                  <tr key="total" className="border-t-2 border-t-slate-700">
                    <td className="px-2 py-1.5 font-semibold text-slate-100">all shown</td>
                    <td className="px-2 py-1.5 font-semibold">{t.trades}</td>
                    <td className="px-2 py-1.5 font-semibold">
                      {t.checked} <span className="font-normal text-slate-500">of {t.fills}</span>
                    </td>
                    <td className="px-2 py-1.5 font-semibold text-amber-300">{t.byVerdict.partial}</td>
                    <td className="px-2 py-1.5 font-semibold text-rose-300">{t.byVerdict.thin}</td>
                    <td className="px-2 py-1.5 font-semibold">
                      <Kinder rs={t.checked ? t.optimismRs : null} />
                    </td>
                  </tr>
                ),
              ]}
            </Table>
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
              <span>
                closed trades{data.since ? ` since ${ist(data.since)}` : ''} · partial: at least half the lots were shown at the price, thin: under half ·
                stop-rule mirrors re-use their book's entry fill, so they are summed apart
              </span>
              {data.worst.length > 0 && (
                <button onClick={() => setShowWorst((v) => !v)} className="rounded bg-slate-800 px-2 py-0.5 text-slate-300 hover:text-white">
                  {showWorst ? 'hide' : 'show'} the {data.worst.length} trades the paper flattered most
                </button>
              )}
            </div>
            {showWorst && (
              <Table head={['Closed (IST)', 'Book', 'Underlying', 'Entry', 'Exits', 'Paper kinder by', 'Verdict', 'Net (paper)']}>
                {data.worst.map((w) => (
                  <tr key={w.tradeId} className="border-b border-slate-900">
                    <td className="whitespace-nowrap px-2 py-1 text-slate-500">{ist(w.closedTs)}</td>
                    <td className="whitespace-nowrap px-2 py-1 text-slate-300">{w.label}</td>
                    <td className="px-2 py-1 font-medium">{w.underlying}</td>
                    <td className="px-2 py-1"><Kinder rs={w.entryRs} /></td>
                    <td className="px-2 py-1"><Kinder rs={w.exitRs} /></td>
                    <td className="px-2 py-1"><Kinder rs={w.optimismRs} className="font-semibold" /></td>
                    <td className="px-2 py-1"><DepthChip verdict={w.verdict} /></td>
                    <td className="px-2 py-1 text-slate-400">{fmt.signedInr(w.net)}</td>
                  </tr>
                ))}
              </Table>
            )}
          </>
        )}
      </div>
    </section>
  )
}
