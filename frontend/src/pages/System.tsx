import { Card, ErrorLine, Notes, Stat, Table } from '../components/Ui'
import { fmt } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Health } from '../types'

function KV({ obj }: { obj: Record<string, unknown> }) {
  return (
    <Table head={['Key', 'Value']}>
      {Object.entries(obj ?? {}).map(([k, v]) => (
        <tr key={k} className="border-b border-slate-900">
          <td className="px-2 py-1 font-mono text-[11px] text-slate-400">{k}</td>
          <td className="px-2 py-1 font-mono text-[11px] text-slate-200">
            {typeof v === 'object' ? JSON.stringify(v) : String(v)}
          </td>
        </tr>
      ))}
    </Table>
  )
}

export function System() {
  const { data, error } = usePoll<Health>('/api/health', 3000)
  if (!data) return <div className="p-4 text-sm text-slate-500">{error ?? 'loading…'}</div>

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      <Notes notes={data.boot_notes} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="Status" value={data.status} tone={data.status === 'ok' ? 'text-emerald-400' : 'text-amber-400'} />
        <Stat label="Uptime" value={fmt.dur(data.uptime_s)} />
        <Stat label="Feed" value={data.feed.connected ? 'connected' : 'down'} tone={data.feed.connected ? 'text-emerald-400' : 'text-rose-400'} sub={`${data.feed.reconnects} reconnects`} />
        <Stat label="Silence" value={data.feed.silence_s != null ? `${data.feed.silence_s.toFixed(0)}s` : 'DM'} sub="since the last message" />
        <Stat label="Open positions" value={data.positions_open} />
      </div>

      <Card title="Checks" right="a degraded check needs 3 consecutive failures — slow is not dead">
        <Table head={['Check', 'State', 'Consecutive failures', 'Detail']}>
          {data.checks.map((c) => (
            <tr key={c.name} className="border-b border-slate-900">
              <td className="px-2 py-1.5 font-mono text-[11px]">{c.name}</td>
              <td className={'px-2 py-1.5 ' + (c.ok ? 'text-emerald-400' : 'text-rose-400')}>{c.ok ? 'ok' : 'failing'}</td>
              <td className="px-2 py-1.5">{c.consecutive_failures}</td>
              <td className="px-2 py-1.5 text-slate-400">{c.detail}</td>
            </tr>
          ))}
        </Table>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Feed">
          <KV obj={data.feed as unknown as Record<string, unknown>} />
        </Card>
        <Card title="Bars">
          <KV obj={data.bars as unknown as Record<string, unknown>} />
        </Card>
        <Card title="Order gateway">
          <KV obj={data.gateway} />
        </Card>
        <Card title="Broker REST">
          <KV obj={data.rest} />
        </Card>
        <Card title="Scrip master">
          <KV obj={data.catalogue} />
        </Card>
        <Card title="Telegram">
          <KV obj={data.telegram} />
        </Card>
      </div>
    </div>
  )
}
