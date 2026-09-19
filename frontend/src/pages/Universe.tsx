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
          {rows.length} symbols · {warm} warm enough to decide (21+ bars of 30m)
        </span>
      </div>
      <Card title="Universe" right="a symbol with no zones has under 25 daily bars and cannot be graded">
        <Table head={['Symbol', 'Scrip code', 'Segment', 'LTP', '30m bars', 'Daily bars', 'Zones']}>
          {rows.map((r) => (
            <tr key={r.scrip_code} className="border-b border-slate-900">
              <td className="px-2 py-1.5 font-medium">{r.symbol}</td>
              <td className="px-2 py-1.5 font-mono text-[11px] text-slate-500">{r.scrip_code}</td>
              <td className="px-2 py-1.5 text-slate-400">{r.segment}</td>
              <td className="px-2 py-1.5">{fmt.n(r.ltp)}</td>
              <td className={'px-2 py-1.5 ' + (r.bars_30m >= 21 ? 'text-slate-300' : 'text-amber-500')}>{r.bars_30m}</td>
              <td className="px-2 py-1.5 text-slate-400">{r.bars_1d}</td>
              <td className={'px-2 py-1.5 ' + (r.zones ? 'text-slate-300' : 'text-amber-500')}>{r.zones}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
