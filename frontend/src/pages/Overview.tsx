import { useState } from 'react'
import { Card, ErrorLine, Notes, Stat, Table } from '../components/Ui'
import { ist, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { EventRow, Health, Overview as OverviewData } from '../types'

/** What the engine is connected to, and the three controls that matter: mode, halt, kill. */
export function Overview() {
  const { data, error } = usePoll<OverviewData>('/api/overview', 3000)
  const health = usePoll<Health>('/api/health', 3000)
  const events = usePoll<EventRow[]>('/api/events?limit=20', 10000)
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)

  const act = async (label: string, path: string, body: unknown) => {
    setBusy(label)
    setActionError(null)
    try {
      await postJson(path, body)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy('')
    }
  }

  if (!data) return <div className="p-4 text-sm text-slate-500">{error ?? 'loading…'}</div>
  const h = health.data
  const sessionCheck = h?.checks.find((c) => c.name === 'broker_session')

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error ?? actionError} />
      <Notes notes={data.boot_notes} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Mode" value={data.mode} sub={data.armed_until ? `armed until ${ist(data.armed_until, true)}` : undefined} />
        <Stat
          label="Halt"
          value={data.halted ? 'HALTED' : 'clear'}
          tone={data.halted ? 'text-rose-400' : 'text-emerald-400'}
          sub={data.halt_reason || undefined}
        />
        <Stat label="Market" value={data.market_open ? 'open' : 'closed'} tone={data.market_open ? 'text-emerald-400' : 'text-slate-400'} />
        <Stat
          label="Feed"
          value={h ? (h.feed.connected ? 'connected' : 'down') : 'DM'}
          tone={h?.feed.connected ? 'text-emerald-400' : 'text-rose-400'}
          sub={h ? `${h.feed.ticks.toLocaleString()} ticks · ${h.feed.oi.toLocaleString()} OI` : undefined}
        />
        <Stat
          label="Universe"
          value={data.universe}
          sub={sessionCheck?.value != null ? `session ${sessionCheck.value} min left` : 'no broker session'}
        />
      </div>

      <Card title="Controls" right="LIVE modes need an arming window; nothing here places an order on its own">
        <div className="flex flex-wrap gap-2 text-xs">
          {(['SHADOW', 'PAPER'] as const).map((m) => (
            <button
              key={m}
              disabled={!!busy || data.mode === m}
              onClick={() => act(m, '/api/control/mode', { mode: m })}
              className="rounded border border-slate-700 bg-slate-800 px-3 py-1 hover:bg-slate-700 disabled:opacity-40"
            >
              {m}
            </button>
          ))}
          <button
            disabled={!!busy}
            onClick={() => act('halt', '/api/control/halt', { halted: !data.halted, reason: data.halted ? '' : 'manual' })}
            className="rounded border border-amber-800 bg-amber-950 px-3 py-1 text-amber-200 hover:bg-amber-900 disabled:opacity-40"
          >
            {data.halted ? 'Resume' : 'Halt'}
          </button>
          <button
            disabled={!!busy}
            onClick={() => act('kill', '/api/control/kill', {})}
            className="rounded border border-rose-800 bg-rose-950 px-3 py-1 text-rose-200 hover:bg-rose-900 disabled:opacity-40"
          >
            Kill (halt + broker square-off when live)
          </button>
          {busy && <span className="self-center text-slate-500">{busy}…</span>}
        </div>
      </Card>

      <Card title="Recent events">
        <Table head={['Time', 'Kind', 'Detail']} empty="no events yet">
          {(events.data ?? []).map((e, i) => (
            <tr key={i} className="border-b border-slate-900">
              <td className="px-2 py-1.5 text-slate-500">{typeof e.ts === 'number' ? ist(e.ts, true) : '—'}</td>
              <td className="px-2 py-1.5">{String(e.kind ?? '')}</td>
              <td className="px-2 py-1.5 font-mono text-[11px] text-slate-400">{JSON.stringify(e)}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
