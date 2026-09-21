import { useState } from 'react'
import { Card, ErrorLine, Table } from '../components/Ui'
import { fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { UniverseRow } from '../types'

export function Universe() {
  const { data, error } = usePoll<UniverseRow[]>('/api/universe', 15000)
  const [q, setQ] = useState('')
  const rows = (data ?? []).filter((r) => r.symbol.includes(q.toUpperCase()))
  const warm = rows.filter((r) => r.bars_30m >= 21).length

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <div className="flex items-center gap-3">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="filter symbol"
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs"
        />
        <span className="text-xs text-slate-500">
          {rows.length} underlyings · {warm} warm enough to decide (21+ bars of 30m) ·{' '}
          {rows.reduce((a, r) => a + r.options, 0)} strikes shortlisted ·{' '}
          {rows.filter((r) => r.note.startsWith('index')).length} indices
        </span>
      </div>
      <Card
        title="Universe"
        right="every root with a derivative, joined to its cash equity (indices ride their front future) — the scripFinder rule"
      >
        <Table head={['Symbol', 'Kind', 'Scrip', 'Segment', 'LTP', 'Prev close', 'Futures', 'Strikes', 'Expiry', '30m', '1d', 'Zones', '']}>
          {rows.map((r) => (
            <tr key={r.scrip_code} className="border-b border-slate-900">
              <td className="px-2 py-1.5 font-medium">{r.symbol}</td>
              <td className="px-2 py-1.5 text-[10px] text-slate-500">{r.note.startsWith('index') ? 'index-fut' : r.kind.toLowerCase()}</td>
              <td className="px-2 py-1.5 font-mono text-[11px] text-slate-500">{r.scrip_code}</td>
              <td className="px-2 py-1.5 text-slate-400">{r.segment}</td>
              <td className="px-2 py-1.5">{fmt.n(r.ltp)}</td>
              <td className="px-2 py-1.5 text-slate-400">{fmt.n(r.prev_close)}</td>
              <td className="px-2 py-1.5 font-mono text-[10px] text-slate-500">{r.futures.join(' ')}</td>
              <td className={'px-2 py-1.5 ' + (r.options ? 'text-slate-300' : 'text-amber-500')}>{r.options}</td>
              <td className="px-2 py-1.5 text-[11px] text-slate-500">{r.option_expiry ?? '—'}</td>
              <td className={'px-2 py-1.5 ' + (r.bars_30m >= 21 ? 'text-slate-300' : 'text-amber-500')}>{r.bars_30m}</td>
              <td className="px-2 py-1.5 text-slate-400">{r.bars_1d}</td>
              <td className={'px-2 py-1.5 ' + (r.zones ? 'text-slate-300' : 'text-amber-500')}>{r.zones}</td>
              <td className="px-2 py-1.5 text-[10px] text-amber-500/80">{r.note.replace('index', '').replace(/^;\s*/, '')}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
