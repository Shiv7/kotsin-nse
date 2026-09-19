import { useState } from 'react'
import { Badge, Card, ErrorLine, GradeBadge, StrategyBadge, Table } from '../components/Ui'
import { fmt, ist } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { GateResult, RejectionRow, SignalRow } from '../types'

function Gates({ gates }: { gates: GateResult[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {gates.map((g) => (
        <span
          key={g.name}
          title={`${g.name}: value ${g.value ?? 'missing'} vs ${g.threshold ?? '—'} ${g.note}`}
          className={
            'rounded px-1 py-0.5 text-[10px] ' +
            (g.missing
              ? 'bg-amber-900/50 text-amber-300'
              : g.passed
                ? 'bg-emerald-900/40 text-emerald-400'
                : 'bg-rose-900/50 text-rose-300')
          }
        >
          {g.name}
          {g.missing && '?'}
        </span>
      ))}
    </div>
  )
}

/**
 * Both halves of the funnel. Recording only the winners makes "what did the filter reject, and
 * would it have won?" unanswerable — which it was for most of the previous stack.
 */
export function Signals() {
  const [tab, setTab] = useState<'signals' | 'rejections'>('signals')
  const signals = usePoll<SignalRow[]>('/api/signals?limit=150', 5000)
  const rejections = usePoll<RejectionRow[]>('/api/rejections?limit=150', 5000)

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={signals.error ?? rejections.error} />
      <div className="flex gap-2">
        {(['signals', 'rejections'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={
              'rounded px-3 py-1 text-xs ' + (tab === t ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400')
            }
          >
            {t === 'signals' ? `Signals (${signals.data?.length ?? 0})` : `Rejections (${rejections.data?.length ?? 0})`}
          </button>
        ))}
      </div>

      {tab === 'signals' ? (
        <Card title="Signals" right="every candidate that cleared its gates">
          <Table head={['Time (IST)', 'Book', 'Symbol', 'Dir', 'Entry', 'Stop', 'T1', 'RR', 'Grade', 'Score', 'Gates', 'Reason']}>
            {(signals.data ?? []).map((s) => (
              <tr key={s.signal_id} className="border-b border-slate-900 align-top">
                <td className="px-2 py-1.5 text-slate-500">{ist(s.ts)}</td>
                <td className="px-2 py-1.5">
                  <StrategyBadge k={s.strategy} />
                </td>
                <td className="px-2 py-1.5 font-medium">{s.symbol}</td>
                <td className={`px-2 py-1.5 ${s.direction === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {s.direction === 'BULLISH' ? 'LONG' : 'SHORT'}
                </td>
                <td className="px-2 py-1.5">{fmt.n(s.entry)}</td>
                <td className="px-2 py-1.5 text-rose-400/80">{fmt.n(s.stop)}</td>
                <td className="px-2 py-1.5 text-emerald-400/80">{fmt.n(s.targets?.[0])}</td>
                <td className="px-2 py-1.5">{fmt.n(s.rr)}</td>
                <td className="px-2 py-1.5">
                  <GradeBadge grade={s.grade} />
                </td>
                <td className="px-2 py-1.5">{fmt.n(s.score, 0)}</td>
                <td className="px-2 py-1.5">
                  <Gates gates={s.gates} />
                </td>
                <td className="max-w-[22rem] px-2 py-1.5 text-slate-400">{s.reason}</td>
              </tr>
            ))}
          </Table>
        </Card>
      ) : (
        <Card title="Rejections" right="the gate that killed each candidate">
          <Table head={['Time (IST)', 'Book', 'Symbol', 'Binding gate', 'Gates', 'Evidence', 'Note']}>
            {(rejections.data ?? []).map((r, i) => (
              <tr key={`${r.symbol}-${r.ts}-${i}`} className="border-b border-slate-900 align-top">
                <td className="px-2 py-1.5 text-slate-500">{ist(r.ts)}</td>
                <td className="px-2 py-1.5">
                  <StrategyBadge k={r.strategy} />
                </td>
                <td className="px-2 py-1.5 font-medium">{r.symbol}</td>
                <td className="px-2 py-1.5">
                  <Badge tone="red">{r.binding_gate}</Badge>
                </td>
                <td className="px-2 py-1.5">
                  <Gates gates={r.gates} />
                </td>
                <td className="px-2 py-1.5 text-[10px] text-slate-500">
                  {Object.entries(r.evidence ?? {})
                    .filter(([, v]) => v !== -1)
                    .slice(0, 5)
                    .map(([k, v]) => `${k}=${typeof v === 'number' ? v.toFixed(2) : v}`)
                    .join('  ')}
                </td>
                <td className="max-w-[20rem] px-2 py-1.5 text-slate-400">{r.note}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  )
}
