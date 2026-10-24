import { useState } from 'react'
import { Badge, Card, ErrorLine, Stat, Table } from '../components/Ui'
import { cls, fmt, ist, pnlColor, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { JournalBlock, JournalCall, JournalView } from '../types'

const R = (v: number | null | undefined) => <span className={pnlColor(v)}>{fmt.r(v)}</span>

function BlockRows({ blocks }: { blocks: Record<string, JournalBlock> }) {
  return (
    <>
      {Object.entries(blocks)
        .filter(([, b]) => b.calls > 0)
        .sort((a, b) => b[1].calls - a[1].calls)
        .map(([name, b]) => (
          <tr key={name} className="border-b border-slate-900">
            <td className="px-2 py-1">{name}</td>
            <td className="px-2 py-1 text-right">{b.calls}</td>
            <td className="px-2 py-1 text-right">{b.filled}</td>
            <td className="px-2 py-1 text-right">{b.closed}</td>
            <td className="px-2 py-1 text-right text-emerald-400">{b.targets}</td>
            <td className="px-2 py-1 text-right text-rose-400">{b.stops}</td>
            <td className="px-2 py-1 text-right">{b.win_rate == null ? 'DM' : `${b.win_rate}%`}</td>
            <td className="px-2 py-1 text-right">{R(b.avg_r)}</td>
            <td className="px-2 py-1 text-right">{R(b.sum_r)}</td>
            <td className="px-2 py-1 text-right">{R(b.avg_mfe_r)}</td>
            <td className="px-2 py-1 text-right">{R(b.avg_mae_r)}</td>
          </tr>
        ))}
    </>
  )
}

const BLOCK_HEAD = ['', 'Calls', 'Filled', 'Closed', 'Targets', 'Stops', 'Win %', 'Avg R', 'Sum R', 'Avg MFE', 'Avg MAE']

/** The owner's calls, scored by the engine. Numbers here are the only measure of the method that the
 * backtests cannot give: which setups he takes, and what happened after he said so. */
export function Journal() {
  const [day, setDay] = useState('')
  const { data, error, refresh } = usePoll<JournalView>(`/api/journal?limit=500${day ? `&day=${day}` : ''}`, 5000)
  const [busy, setBusy] = useState<string | null>(null)
  const act = async (id: string, what: 'close' | 'cancel') => {
    setBusy(id)
    try {
      await postJson(`/api/journal/${id}/${what}`, {})
      await refresh()
    } finally {
      setBusy(null)
    }
  }
  const s = data?.summary
  const all = s?.all

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-sm font-semibold text-slate-200">Call journal</h1>
        <input type="date" value={day} onChange={(e) => setDay(e.target.value)} className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300" />
        {day && (
          <button onClick={() => setDay('')} className="text-xs text-slate-400 hover:text-white">
            all days
          </button>
        )}
        <span className="text-xs text-slate-500">log a call from the Chart page · scored every 20 s from 1m bars · stop before target when a bar touches both · closed at 15:25</span>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <Stat label="Calls" value={all?.calls ?? 'DM'} sub={`${data?.open ?? 0} open`} />
        <Stat label="Filled" value={all?.filled ?? 'DM'} sub="level reached" />
        <Stat label="Targets / stops" value={all ? `${all.targets} / ${all.stops}` : 'DM'} />
        <Stat label="Win rate" value={all?.win_rate == null ? 'DM' : `${all.win_rate}%`} sub="closed, R > 0" />
        <Stat label="Avg R" value={fmt.r(all?.avg_r)} tone={pnlColor(all?.avg_r)} sub="per closed call" />
        <Stat label="Sum R" value={fmt.r(all?.sum_r)} tone={pnlColor(all?.sum_r)} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="By kind and side" right="entry = would take it · alert = watching">
          <Table head={BLOCK_HEAD}>{s ? [<BlockRows key="k" blocks={s.by_kind} />, <BlockRows key="s" blocks={s.by_side} />] : []}</Table>
        </Card>
        <Card title="By tag" right="a call carries every tag it was given">
          <Table head={BLOCK_HEAD} empty="no tags yet">{s ? <BlockRows blocks={s.by_tag} /> : []}</Table>
        </Card>
      </div>

      <Card title="Calls" right={`${data?.rows.length ?? 0} shown, newest first`}>
        <Table head={['Time', 'Symbol', 'Side', 'Kind', 'Entry', 'Stop', 'Target', 'Tags', 'Status', 'R now', 'MFE', 'MAE', '15m', '30m', '60m', 'Close', 'Note', '']} empty="no calls yet — log one from the Chart page">
          {(data?.rows ?? []).map((c: JournalCall) => {
            const o = c.outcome
            return (
              <tr key={c.id} className={cls('border-b border-slate-900', c.status === 'CANCELLED' && 'opacity-40')}>
                <td className="whitespace-nowrap px-2 py-1 text-slate-400">{ist(c.ts).slice(0, 16)}</td>
                <td className="px-2 py-1 font-semibold">{c.symbol}</td>
                <td className={cls('px-2 py-1', c.side === 'LONG' ? 'text-emerald-400' : 'text-rose-400')}>{c.side}</td>
                <td className="px-2 py-1 text-slate-400">{c.kind}</td>
                <td className="px-2 py-1 text-right">
                  {fmt.n(c.entry)}
                  {c.price_at_call != null && Math.abs(c.price_at_call - c.entry) > 1e-9 && <span className="text-slate-500"> ({fmt.n(c.price_at_call)})</span>}
                </td>
                <td className="px-2 py-1 text-right">{c.stop == null ? <span className="text-amber-400" title="no stop: default risk 0.5%">0.5%</span> : fmt.n(c.stop)}</td>
                <td className="px-2 py-1 text-right">{fmt.n(c.target)}</td>
                <td className="px-2 py-1">
                  <div className="flex flex-wrap gap-1">
                    {c.tags.map((t) => (
                      <Badge key={t} tone="violet">
                        {t}
                      </Badge>
                    ))}
                  </div>
                </td>
                <td className="px-2 py-1">
                  {c.status === 'OPEN' ? (
                    <Badge tone={o?.filled_ts ? 'blue' : 'slate'}>{o?.filled_ts ? 'open' : 'waiting'}</Badge>
                  ) : c.status === 'CANCELLED' ? (
                    <Badge>cancelled</Badge>
                  ) : (
                    <Badge tone={o?.hit === 'target' ? 'green' : o?.hit === 'stop' ? 'red' : 'amber'}>{c.close_reason}</Badge>
                  )}
                </td>
                <td className="px-2 py-1 text-right">{R(o?.r_now)}</td>
                <td className="px-2 py-1 text-right">{R(o?.mfe_r)}</td>
                <td className="px-2 py-1 text-right">{R(o?.mae_r)}</td>
                <td className="px-2 py-1 text-right">{R(o?.r_15m)}</td>
                <td className="px-2 py-1 text-right">{R(o?.r_30m)}</td>
                <td className="px-2 py-1 text-right">{R(o?.r_60m)}</td>
                <td className="px-2 py-1 text-right">{R(o?.r_close)}</td>
                <td className="max-w-[16rem] truncate px-2 py-1 text-slate-400" title={c.note}>
                  {c.note}
                </td>
                <td className="whitespace-nowrap px-2 py-1">
                  {c.status === 'OPEN' && (
                    <>
                      <button disabled={busy === c.id} onClick={() => void act(c.id, 'close')} className="mr-1 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-200">
                        exit
                      </button>
                      <button disabled={busy === c.id} onClick={() => void act(c.id, 'cancel')} className="rounded bg-slate-900 px-1.5 py-0.5 text-[10px] text-slate-500" title="a mistake, not a trade">
                        cancel
                      </button>
                    </>
                  )}
                </td>
              </tr>
            )
          })}
        </Table>
      </Card>
    </div>
  )
}
