import { useState } from 'react'
import { Card, ErrorLine, Stat, Table } from '../components/Ui'
import { fmt, postJson, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Health, Overview } from '../types'

function Confirm({ label, danger, onRun }: { label: string; danger?: boolean; onRun: () => Promise<unknown> }) {
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  return (
    <div className="flex items-center gap-2">
      <button
        disabled={busy}
        onClick={async () => {
          if (!armed) {
            setArmed(true)
            setTimeout(() => setArmed(false), 5000)
            return
          }
          setBusy(true)
          try {
            await onRun()
            setMsg('done')
          } catch (e) {
            setMsg(e instanceof Error ? e.message : String(e))
          } finally {
            setBusy(false)
            setArmed(false)
          }
        }}
        className={
          'rounded px-3 py-1 text-xs font-medium ' +
          (armed
            ? 'bg-amber-600 text-white'
            : danger
              ? 'bg-rose-800 text-rose-100 hover:bg-rose-700'
              : 'bg-slate-700 text-slate-100 hover:bg-slate-600')
        }
      >
        {armed ? 'click again to confirm' : label}
      </button>
      {msg && <span className="text-[11px] text-slate-400">{msg}</span>}
    </div>
  )
}

/** Controls that can lose money. Every one of them is two clicks. */
export function Risk() {
  const { data, error, refresh } = usePoll<Overview>('/api/overview', 3000)
  const health = usePoll<Health>('/api/health', 3000)
  const [armMinutes, setArmMinutes] = useState(60)

  const caps = (health.data?.gateway as { caps?: Record<string, unknown> } | undefined)?.caps ?? {}

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error ?? health.error} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Mode" value={data?.mode ?? 'DM'} />
        <Stat label="Day P&L" value={fmt.signedInr(data?.day_pnl)} tone={pnlColor(data?.day_pnl)} />
        <Stat label="Gross exposure" value={fmt.inr(data?.exposure.gross)} sub={`${data?.exposure.gross_pct.toFixed(1) ?? '—'}%`} />
        <Stat
          label="Halt"
          value={data?.halted ? 'HALTED' : 'clear'}
          tone={data?.halted ? 'text-rose-400' : 'text-emerald-400'}
          sub={data?.halt_reason || undefined}
        />
      </div>

      <Card title="Mode" right="LIVE is state, and it expires — a restart after the window boots into PAPER">
        <div className="flex flex-wrap items-center gap-3">
          {(['SHADOW', 'PAPER'] as const).map((m) => (
            <Confirm
              key={m}
              label={`switch to ${m}`}
              onRun={async () => {
                await postJson('/api/control/mode', { mode: m })
                await refresh()
              }}
            />
          ))}
          <div className="flex items-center gap-2">
            <label className="text-xs text-slate-400">arm for</label>
            <input
              type="number"
              min={1}
              max={480}
              value={armMinutes}
              onChange={(e) => setArmMinutes(Number(e.target.value))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs"
            />
            <span className="text-xs text-slate-400">min</span>
            <Confirm
              label="arm LIVE_CAPPED"
              danger
              onRun={async () => {
                await postJson('/api/control/mode', { mode: 'LIVE_CAPPED', armed_minutes: armMinutes })
                await refresh()
              }}
            />
          </div>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Kill switch" right="halt, then square off through the broker">
          <div className="space-y-2">
            <Confirm
              label={data?.halted ? 'resume' : 'HALT (stop new entries)'}
              danger={!data?.halted}
              onRun={async () => {
                await postJson('/api/control/halt', { halted: !data?.halted, reason: 'manual' })
                await refresh()
              }}
            />
            <Confirm label="KILL — halt and flatten everything" danger onRun={() => postJson('/api/control/kill', {})} />
            <p className="text-[11px] leading-relaxed text-slate-500">
              Kill uses the broker's own bulk square-off rather than our position list: the moment this button is
              pressed is exactly when our view of the book is least trustworthy.
            </p>
          </div>
        </Card>

        <Card title="Reconciliation" right="the broker is the source of truth">
          <div className="space-y-2">
            <Confirm label="reconcile now" onRun={() => postJson('/api/control/reconcile', {})} />
            <Confirm label="acknowledge mismatch (resume entries)" onRun={() => postJson('/api/control/acknowledge', {})} />
            <Confirm label="reset order-gateway breaker" onRun={() => postJson('/api/control/reset-breaker', {})} />
          </div>
        </Card>
      </div>

      <Card title="LIVE_CAPPED caps" right="these bound an armed engine; they are not the mode itself">
        <Table head={['Cap', 'Value']}>
          {Object.entries(caps).map(([k, v]) => (
            <tr key={k} className="border-b border-slate-900">
              <td className="px-2 py-1.5 font-mono text-[11px] text-slate-400">{k}</td>
              <td className="px-2 py-1.5 font-mono text-[11px]">{JSON.stringify(v)}</td>
            </tr>
          ))}
        </Table>
      </Card>
    </div>
  )
}
