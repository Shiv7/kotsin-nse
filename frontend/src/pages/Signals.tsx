import { useMemo, useState } from 'react'
import { Gates } from '../components/Gates'
import { Badge, Card, ErrorLine, GradeBadge, StrategyBadge, Table } from '../components/Ui'
import { Link } from 'react-router-dom'
import { cls, fmt, ist, pnlColor, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { RejectionRow, SignalRow, TradeRow } from '../types'

function KV({ rows }: { rows: [string, unknown][] }) {
  return (
    <table className="w-full text-[11px]">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k} className="border-b border-slate-900/60">
            <td className="py-0.5 pr-3 font-mono text-slate-500">{k}</td>
            <td className="py-0.5 font-mono text-slate-200">{v == null ? 'DM' : typeof v === 'number' ? fmt.n(v, Number.isInteger(v) ? 0 : 3) : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * The drill-down a human reviewer needs: not "it fired", but WHY — which zone became the stop,
 * which zones became the targets, what the indicators read on that bar, which gate each value
 * cleared, and what happened next. Every number here was stored by the code that decided, at
 * the moment it decided; nothing is re-derived for display.
 */
function SignalDetail({ s, trade }: { s: SignalRow; trade?: TradeRow }) {
  const c = s.context ?? {}
  const conf = c.confluence
  const indi = c.indicators
  const zones = c.zones ?? []
  const stopZone = conf?.stop_zone ?? ''
  const targetZones = new Set(conf?.target_zones ?? [])
  const isStop = (members: string[]) => stopZone !== '' && members.join(',') === stopZone
  const isTarget = (members: string[]) => targetZones.has(members.join(','))
  const filled = s.decision === 'PAPER_FILLED' || s.decision === 'SUBMITTED'
  const [review, setReview] = useState('')
  const askCommittee = async () => {
    setReview('reviewing… (5 Claude calls)')
    try {
      const r = await postJson<{ failure_mode?: string; confidence?: number; lesson?: string; error?: string | null }>('/api/committee/review/signal', { signal_id: s.signal_id })
      setReview(r.error ? `error: ${r.error}` : `${r.failure_mode} (${((r.confidence ?? 0) * 100).toFixed(0)}%) — ${r.lesson}`)
    } catch (e) {
      setReview(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="grid gap-3 border-l-2 border-slate-700 bg-slate-950/60 p-3 md:grid-cols-3">
      <Card title="1 · Decision" right={s.signal_id}>
        <KV
          rows={[
            ['decision', s.decision ?? 'DM'],
            ['reason', s.decision_reason ?? ''],
            ['direction', s.direction],
            ['entry (underlying)', s.entry],
            ['score', s.score],
            ['confidence', s.confidence],
            ['source signal', s.source_signal_id || '—'],
          ]}
        />
        <div className="mt-2 text-[11px] text-slate-400">{s.reason}</div>
        <button className="mt-2 rounded bg-violet-900/60 px-2 py-0.5 text-[11px] text-violet-200 hover:bg-violet-800" onClick={() => void askCommittee()}>
          ask the committee why
        </button>
        {review && (
          <div className="mt-1 text-[11px] text-slate-300">
            {review} · <Link to="/committee" className="text-sky-400 underline">committee</Link>
          </div>
        )}
      </Card>

      <Card title="2 · Indicators on the bar" right={indi ? `BB(${indi.params.bb_period},${indi.params.bb_mult}) ST(${indi.params.st_atr_period},${indi.params.st_mult})` : ''}>
        {indi ? (
          <KV
            rows={[
              ['close', s.entry],
              ['bb_upper', indi.bb_upper],
              ['bb_middle', indi.bb_middle],
              ['bb_lower', indi.bb_lower],
              ['close vs band', s.direction === 'BULLISH' ? `${fmt.pct((s.entry / indi.bb_upper - 1) * 100)} above upper` : `${fmt.pct((s.entry / indi.bb_lower - 1) * 100)} vs lower`],
              ['supertrend', `${fmt.n(indi.st_value)} (${indi.st_trend > 0 ? 'UP' : 'DOWN'}, ${indi.bars_in_trend} bars)`],
              ['ATR(14)', indi.atr],
              ['ATR % of price', `${((indi.atr / s.entry) * 100).toFixed(2)}%`],
            ]}
          />
        ) : (
          <div className="text-[11px] text-slate-500">not recorded on this signal (pre-telemetry row)</div>
        )}
      </Card>

      <Card title="3 · Why this stop, why these targets" right={conf ? `grade ${conf.grade} · rr ${conf.rr}` : ''}>
        {conf ? (
          <div className="space-y-2">
            <div className="rounded border border-rose-900/50 bg-rose-950/30 p-2 text-[11px]">
              <div className="font-semibold text-rose-300">STOP {fmt.n(conf.stop)}</div>
              <div className="text-slate-400">
                {((Math.abs(s.entry - conf.stop) / s.entry) * 100).toFixed(2)}% from entry ·{' '}
                {indi ? `${(Math.abs(s.entry - conf.stop) / indi.atr).toFixed(2)} ATR` : ''}
              </div>
              <div className="text-slate-500">{stopZone ? `nearest zone behind: ${stopZone}` : 'no zone behind — fell back to 1 ATR'}</div>
            </div>
            {conf.targets.map((t, i) => (
              <div key={i} className="rounded border border-emerald-900/50 bg-emerald-950/30 p-2 text-[11px]">
                <div className="font-semibold text-emerald-300">
                  T{i + 1} {fmt.n(t)} <span className="font-normal text-slate-500">({((Math.abs(t - s.entry) / s.entry) * 100).toFixed(2)}%)</span>
                </div>
                <div className="text-slate-500">wall ahead: {conf.target_zones[i] ?? '—'}</div>
              </div>
            ))}
            <KV
              rows={[
                ['fortress (T1 wall strength)', conf.fortress],
                ['room to next wall (ATR)', conf.room_ratio],
                ['policy rr floor / A / B / C', `${conf.policy.rr_hard_floor} / ${conf.policy.rr_a} / ${conf.policy.rr_b} / ${conf.policy.rr_c}`],
                ['note', conf.note || '—'],
              ]}
            />
          </div>
        ) : (
          <div className="text-[11px] text-slate-500">not recorded on this signal</div>
        )}
      </Card>

      <Card title="4 · Every zone the engine saw" right={`${zones.length} zones · amber = wall`} className="md:col-span-2">
        <Table head={['Price', 'vs entry', 'Strength', 'Wall', 'Role', 'Members']} empty="no zones recorded">
          {zones.map((z) => {
            const role = isStop(z.members) ? 'STOP' : isTarget(z.members) ? `T${(conf?.target_zones ?? []).indexOf(z.members.join(',')) + 1}` : ''
            return (
              <tr key={z.price} className={cls('border-b border-slate-900', role === 'STOP' && 'bg-rose-950/30', role.startsWith('T') && 'bg-emerald-950/20')}>
                <td className="px-2 py-1">{fmt.n(z.price)}</td>
                <td className={cls('px-2 py-1', z.price > s.entry ? 'text-emerald-500/80' : 'text-rose-500/80')}>{fmt.pct(((z.price - s.entry) / s.entry) * 100)}</td>
                <td className="px-2 py-1">{fmt.n(z.strength, 1)}</td>
                <td className="px-2 py-1">{z.wall ? <span className="text-amber-400">wall</span> : '—'}</td>
                <td className="px-2 py-1 font-semibold">{role}</td>
                <td className="px-2 py-1 text-slate-500">{z.members.join(', ')}</td>
              </tr>
            )
          })}
        </Table>
      </Card>

      <Card title="5 · Gates and evidence">
        <Table head={['Gate', 'Value', 'Threshold', 'Pass', 'Note']}>
          {s.gates.map((g) => (
            <tr key={g.name} className="border-b border-slate-900">
              <td className="px-2 py-1 font-mono text-[11px]">{g.name}</td>
              <td className="px-2 py-1">{g.value == null ? <span className="text-amber-400">missing</span> : fmt.n(g.value, 3)}</td>
              <td className="px-2 py-1 text-slate-500">{g.threshold == null ? '—' : fmt.n(g.threshold, 3)}</td>
              <td className={cls('px-2 py-1', g.passed ? 'text-emerald-400' : 'text-rose-400')}>{g.passed ? 'ok' : 'FAIL'}</td>
              <td className="px-2 py-1 text-[10px] text-slate-500">{g.note}</td>
            </tr>
          ))}
        </Table>
        {c.conviction && (
          <div className="mt-3">
            <div className="mb-1 text-[11px] font-semibold text-violet-300">FUKAA conviction</div>
            <KV rows={Object.entries(c.conviction)} />
          </div>
        )}
        {c.volume && (
          <div className="mt-3">
            <div className="mb-1 text-[11px] font-semibold text-violet-300">FUKAA volume</div>
            <KV rows={Object.entries(c.volume)} />
          </div>
        )}
        <div className="mt-3">
          <div className="mb-1 text-[11px] font-semibold text-slate-400">evidence</div>
          <KV rows={Object.entries(s.evidence ?? {}).filter(([, v]) => v !== -1)} />
        </div>
      </Card>

      <Card title="6 · Outcome" right={filled ? 'filled' : 'not filled'} className="md:col-span-3">
        {trade ? (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
            {(
              [
                ['instrument', trade.symbol],
                ['entry → exit', `${fmt.n(trade.entry)} → ${fmt.n(trade.exit)}`],
                ['net', fmt.signedInr(trade.net)],
                ['R', fmt.r(trade.r_multiple)],
                ['MFE / MAE', `${fmt.r(trade.mfe_r)} / ${fmt.r(trade.mae_r)}`],
                ['exit', `${trade.exit_reason} · ${fmt.dur(trade.duration_s)}`],
              ] as [string, string][]
            ).map(([k, v]) => (
              <div key={k}>
                <div className="text-[10px] uppercase text-slate-500">{k}</div>
                <div className={cls('text-sm font-semibold', k === 'net' || k === 'R' ? pnlColor(trade.net) : 'text-slate-200')}>{v}</div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-[11px] text-slate-400">
            {filled ? 'position still open, or not yet closed' : `no position — ${s.decision ?? 'not filled'}${s.decision_reason ? `: ${s.decision_reason}` : ''}`}
          </div>
        )}
      </Card>
    </div>
  )
}

export function Signals() {
  const [tab, setTab] = useState<'signals' | 'rejections'>('signals')
  const [open, setOpen] = useState<string | null>(null)
  const signals = usePoll<SignalRow[]>('/api/signals?limit=150', 5000)
  const rejections = usePoll<RejectionRow[]>('/api/rejections?limit=150', 5000)
  const trades = usePoll<TradeRow[]>('/api/trades?limit=500', 10000)
  const tradeBySignal = useMemo(() => {
    const m = new Map<string, TradeRow>()
    for (const t of trades.data ?? []) if ((t as TradeRow & { signal_id?: string }).signal_id) m.set((t as TradeRow & { signal_id: string }).signal_id, t)
    return m
  }, [trades.data])

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={signals.error ?? rejections.error} />
      <div className="flex gap-2">
        {(['signals', 'rejections'] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={'rounded px-3 py-1 text-xs ' + (tab === t ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400')}>
            {t === 'signals' ? `Signals (${signals.data?.length ?? 0})` : `Rejections (${rejections.data?.length ?? 0})`}
          </button>
        ))}
        <span className="self-center text-[11px] text-slate-500">click a signal for the full why</span>
      </div>

      {tab === 'signals' ? (
        <Card title="Signals" right="every candidate that cleared its gates">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-800 text-slate-500">
                  {['Time (IST)', 'Book', 'Symbol', 'Dir', 'Entry', 'Stop', 'T1', 'RR', 'Grade', 'Decision', 'Outcome', 'Gates'].map((h) => (
                    <th key={h} className="whitespace-nowrap px-2 py-1.5 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(signals.data ?? []).length === 0 && (
                  <tr>
                    <td colSpan={12} className="px-2 py-6 text-center text-slate-600">nothing yet</td>
                  </tr>
                )}
                {(signals.data ?? []).map((s) => {
                  const t = tradeBySignal.get(s.signal_id)
                  const isOpen = open === s.signal_id
                  return [
                    <tr key={s.signal_id} onClick={() => setOpen(isOpen ? null : s.signal_id)} className={cls('cursor-pointer border-b border-slate-900 align-top hover:bg-slate-900/60', isOpen && 'bg-slate-900')}>
                      <td className="px-2 py-1.5 text-slate-500">{ist(s.ts)}</td>
                      <td className="px-2 py-1.5"><StrategyBadge k={s.strategy} /></td>
                      <td className="px-2 py-1.5 font-medium">{s.symbol}</td>
                      <td className={`px-2 py-1.5 ${s.direction === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400'}`}>{s.direction === 'BULLISH' ? 'LONG' : 'SHORT'}</td>
                      <td className="px-2 py-1.5">{fmt.n(s.entry)}</td>
                      <td className="px-2 py-1.5 text-rose-400/80">{fmt.n(s.stop)}</td>
                      <td className="px-2 py-1.5 text-emerald-400/80">{fmt.n(s.targets?.[0])}</td>
                      <td className="px-2 py-1.5">{fmt.n(s.rr)}</td>
                      <td className="px-2 py-1.5"><GradeBadge grade={s.grade} /></td>
                      <td className="px-2 py-1.5"><Badge tone={s.decision === 'PAPER_FILLED' || s.decision === 'SUBMITTED' ? 'green' : 'amber'}>{s.decision ?? 'DM'}</Badge></td>
                      <td className={cls('px-2 py-1.5 font-medium', pnlColor(t?.net))}>{t ? `${fmt.signedInr(t.net)} (${fmt.r(t.r_multiple)})` : '—'}</td>
                      <td className="px-2 py-1.5"><Gates gates={s.gates} /></td>
                    </tr>,
                    isOpen ? (
                      <tr key={`${s.signal_id}-detail`}>
                        <td colSpan={12} className="p-0"><SignalDetail s={s} trade={t} /></td>
                      </tr>
                    ) : null,
                  ]
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : (
        <Card title="Rejections" right="the gate that killed each candidate">
          <Table head={['Time (IST)', 'Book', 'Symbol', 'Binding gate', 'Gates', 'Evidence', 'Note']}>
            {(rejections.data ?? []).map((r, i) => (
              <tr key={`${r.symbol}-${r.ts}-${i}`} className="border-b border-slate-900 align-top">
                <td className="px-2 py-1.5 text-slate-500">{ist(r.ts)}</td>
                <td className="px-2 py-1.5"><StrategyBadge k={r.strategy} /></td>
                <td className="px-2 py-1.5 font-medium">{r.symbol}</td>
                <td className="px-2 py-1.5"><Badge tone="red">{r.binding_gate}</Badge></td>
                <td className="px-2 py-1.5"><Gates gates={r.gates} /></td>
                <td className="px-2 py-1.5 text-[10px] text-slate-500">
                  {Object.entries(r.evidence ?? {}).filter(([, v]) => v !== -1).slice(0, 5).map(([k, v]) => `${k}=${typeof v === 'number' ? v.toFixed(2) : v}`).join('  ')}
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
