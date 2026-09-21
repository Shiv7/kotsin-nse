import { useState } from 'react'
import { Badge, Card, ErrorLine, Stat, StrategyBadge, Table } from '../components/Ui'
import { cls, fmt, getJson, ist, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { Bucket, CommitteeStatus, Forensics, HypothesisRow, ReviewRow } from '../types'

type Tone = 'slate' | 'green' | 'red' | 'amber' | 'violet' | 'blue'

const MODE_TONE: Record<string, Tone> = {
  NOISE_STOP: 'red',
  WRONG_DIRECTION: 'red',
  LATE_ENTRY: 'amber',
  TARGET_UNREACHABLE: 'amber',
  GAVE_BACK: 'amber',
  COST_DOMINATED: 'violet',
  SESSION_TIMING: 'blue',
  INSTRUMENT_MISMATCH: 'violet',
  DATA_QUALITY: 'slate',
  VALID_LOSS: 'slate',
  GOOD_TRADE: 'green',
  INCONCLUSIVE: 'slate',
}
const STATUS_TONE: Record<string, Tone> = {
  pending: 'slate',
  running: 'blue',
  confirmed: 'green',
  refuted: 'red',
  inconclusive: 'amber',
  error: 'red',
}

const HEADLINE: [string, string, (v: unknown) => string][] = [
  ['n', 'trades', (v) => fmt.int(v as number)],
  ['n_days', 'days', (v) => fmt.int(v as number)],
  ['avg_r', 'avg R (day-clustered)', (v) => fmt.r(v as number)],
  ['avg_r_t', 't-stat', (v) => fmt.n(v as number, 2)],
  ['win_rate', 'win rate', (v) => `${fmt.n(v as number, 1)}%`],
  ['net', 'net', (v) => fmt.inr(v as number)],
  ['charges_share_of_gross', 'charges / |gross|', (v) => `${fmt.n(v as number, 1)}%`],
  ['stop_hit_rate', 'stop hits', (v) => `${fmt.n(v as number, 1)}%`],
  ['first_bar_stop_rate', 'stopped on bar 1', (v) => `${fmt.n(v as number, 1)}%`],
  ['give_back_rate', 'gave back (MFE≥1R, exit≤0)', (v) => `${fmt.n(v as number, 1)}%`],
  ['mfe_capture_median', 'median R / MFE', (v) => fmt.n(v as number, 2)],
  ['t1_hit_rate', 'T1 taken', (v) => `${fmt.n(v as number, 1)}%`],
  ['eod_rate', 'force-flat exits', (v) => `${fmt.n(v as number, 1)}%`],
  ['median_stop_pct', 'median stop distance', (v) => `${fmt.n(v as number, 2)}%`],
  ['avg_mfe_r', 'avg MFE', (v) => fmt.r(v as number)],
  ['avg_mae_r', 'avg MAE', (v) => fmt.r(v as number)],
]

const DIM_LABEL: Record<string, string> = {
  stop_pct: 'stop distance (% of entry)',
  rr: 'reward : risk at entry',
  grade: 'grade',
  exit_reason: 'exit reason',
  bars_held: 'bars held (30m)',
  hour_ist: 'entry hour (IST)',
  dow: 'weekday',
  direction: 'direction',
  strategy: 'book',
  month: 'month',
  symbol: 'symbol (n ≥ 3, worst first)',
}
const DIM_ORDER = ['stop_pct', 'rr', 'grade', 'exit_reason', 'bars_held', 'hour_ist', 'dow', 'direction', 'strategy', 'month', 'symbol']

function DimTable({ name, rows }: { name: string; rows: Bucket[] }) {
  return (
    <Card title={DIM_LABEL[name] ?? name} right={`by_${name}.*`}>
      <Table head={['bucket', 'n', 'avg R', 't', 'win %', 'net', 'loss %']} empty="no rows">
        {rows.map((b) => (
          <tr key={b.label} className={cls('border-b border-slate-900', b.too_small && 'text-slate-500')} title={b.too_small ? 'too small to trust (<30 trades or <10 days)' : `${b.n_days} days`}>
            <td className="px-2 py-1 font-mono text-[11px]">{b.label}</td>
            <td className="px-2 py-1">{b.n}</td>
            <td className={cls('px-2 py-1 font-medium', (b.avg_r ?? 0) < 0 ? 'text-rose-400' : 'text-emerald-400')}>{fmt.r(b.avg_r)}</td>
            <td className="px-2 py-1 text-slate-400">{b.t == null ? 'DM' : fmt.n(b.t, 1)}</td>
            <td className="px-2 py-1">{fmt.n(b.win_rate, 0)}</td>
            <td className="px-2 py-1">{fmt.inr(b.net)}</td>
            <td className="px-2 py-1 text-slate-400">{fmt.n(b.loss_share, 0)}</td>
          </tr>
        ))}
      </Table>
    </Card>
  )
}

function Changes({ changes }: { changes: { path: string; value: number }[] }) {
  return (
    <span className="font-mono text-[10px] text-slate-300">
      {changes.map((c) => `${c.path}=${c.value}`).join('  ')}
    </span>
  )
}

function Propose({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState('')
  const [changes, setChanges] = useState('fudkii.grade_policy.min_stop_atr=1.0')
  const [expected, setExpected] = useState('')
  const [msg, setMsg] = useState('')
  const submit = async () => {
    const parsed = changes
      .split(/[,\s]+/)
      .filter(Boolean)
      .map((kv) => {
        const [path, value] = kv.split('=')
        return { path, value: Number(value) }
      })
    if (!title || parsed.some((c) => !c.path || Number.isNaN(c.value))) {
      setMsg('need a title and changes as path=value')
      return
    }
    try {
      await postJson('/api/committee/hypotheses', { title, changes: parsed, expected })
      setMsg('proposed — run the experiment below')
      setTitle('')
      onDone()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <span className="text-slate-500">propose your own (no key needed):</span>
      <input className="w-56 rounded bg-slate-900 px-2 py-1 text-slate-200" placeholder="title" value={title} onChange={(e) => setTitle(e.target.value)} />
      <input className="w-80 rounded bg-slate-900 px-2 py-1 font-mono text-slate-200" placeholder="fudkii.grade_policy.min_stop_atr=1.0 limits.risk_per_trade_pct=0.5" value={changes} onChange={(e) => setChanges(e.target.value)} />
      <input className="w-64 rounded bg-slate-900 px-2 py-1 text-slate-200" placeholder="expected (a number to beat)" value={expected} onChange={(e) => setExpected(e.target.value)} />
      <button className="rounded bg-sky-900/60 px-2 py-1 text-sky-200 hover:bg-sky-800" onClick={() => void submit()}>
        propose
      </button>
      <span className="text-slate-400">{msg}</span>
    </div>
  )
}

function Hypotheses({ rows, onRun, busy, onProposed }: { rows: HypothesisRow[]; onRun: (id: string) => void; busy: boolean; onProposed: () => void }) {
  return (
    <Card title="Hypotheses" right="propose → backtest → grade → remember. A hypothesis is confirmed by the backtester, never by prose.">
      <div className="mb-2">
        <Propose onDone={onProposed} />
      </div>
      <Table head={['proposed', 'from', 'hypothesis', 'changes', 'status', 'baseline → patched', 'Δ avg R · p', 'lesson', '']} empty="no hypotheses yet — run a review">
        {rows.map((h) => {
          const r = h.result
          return (
            <tr key={h.id} className="border-b border-slate-900 align-top">
              <td className="px-2 py-1.5 text-slate-500">{ist(h.review_ts)}</td>
              <td className="px-2 py-1.5 text-[11px] text-slate-400">{h.review_kind}{h.subject && (h.subject as { ref?: string; source?: string }).ref ? ` · ${(h.subject as { ref?: string }).ref}` : ''}</td>
              <td className="max-w-[18rem] px-2 py-1.5">
                <div className="font-medium">{h.title}</div>
                <div className="text-[11px] text-slate-500">{h.expected}</div>
              </td>
              <td className="px-2 py-1.5"><Changes changes={h.changes} /></td>
              <td className="px-2 py-1.5"><Badge tone={STATUS_TONE[h.status] ?? 'slate'}>{h.status}</Badge></td>
              <td className="px-2 py-1.5 text-[11px]">
                {r?.baseline && r?.patched ? (
                  <>
                    {fmt.r(r.baseline.avg_r)} (n {r.baseline.n}) → {fmt.r(r.patched.avg_r)} (n {r.patched.n})
                  </>
                ) : (
                  'DM'
                )}
              </td>
              <td className="px-2 py-1.5 text-[11px]">{r?.delta_avg_r == null ? 'DM' : `${fmt.r(r.delta_avg_r)} · p ${fmt.n(r.p_value, 3)}`}</td>
              <td className="max-w-[20rem] px-2 py-1.5 text-[11px] text-slate-400">{h.reflection ?? h.error ?? r?.note ?? ''}</td>
              <td className="px-2 py-1.5">
                <button disabled={busy || h.status === 'running'} className="rounded bg-sky-900/60 px-2 py-0.5 text-[11px] text-sky-200 hover:bg-sky-800 disabled:opacity-40" onClick={() => onRun(h.id)}>
                  {h.status === 'running' ? 'running…' : 'run experiment'}
                </button>
              </td>
            </tr>
          )
        })}
      </Table>
    </Card>
  )
}

function ReviewDetail({ r, onClose }: { r: ReviewRow; onClose: () => void }) {
  const subject = (r.subject ?? {}) as { ref?: string; source?: string; kind?: string }
  return (
    <Card
      title={`${r.kind === 'case' ? 'Post-mortem' : 'Cohort report'} · ${r.symbol ?? subject.source ?? ''} · ${ist(r.ts)}`}
      right={
        <button className="text-[11px] text-slate-400 underline" onClick={onClose}>
          close
        </button>
      }
    >
      {r.error && <div className="text-sm text-rose-400">{r.error}</div>}
      {r.kind === 'case' && r.failure_mode && (
        <div className="grid gap-3 text-[12px] md:grid-cols-2">
          <div className="md:col-span-2 flex items-center gap-2">
            <Badge tone={MODE_TONE[r.failure_mode] ?? 'slate'}>{r.failure_mode}</Badge>
            <span className="text-slate-400">confidence {fmt.n(r.confidence, 2)}</span>
            {(r.secondary ?? []).map((m) => (
              <Badge key={m} tone="slate">{m}</Badge>
            ))}
            <span className="text-slate-500">{subject.ref}</span>
          </div>
          <div><div className="text-[10px] uppercase text-slate-500">what happened</div><p className="text-slate-200">{r.what_happened}</p></div>
          <div><div className="text-[10px] uppercase text-slate-500">why</div><p className="text-slate-200">{r.why}</p></div>
          <div className="md:col-span-2"><div className="text-[10px] uppercase text-slate-500">counterfactual</div><p className="text-slate-200">{r.counterfactual}</p></div>
          <div className="md:col-span-2"><div className="text-[10px] uppercase text-slate-500">lesson (re-read by future reviews)</div><p className="text-amber-200">{r.lesson}</p></div>
        </div>
      )}
      {r.kind === 'cohort' && r.verdict && (
        <div className="space-y-2 text-[12px]">
          <p className="text-slate-200">{r.verdict}</p>
          <Table head={['finding', 'mode', 'magnitude', 'confidence']} empty="no findings">
            {(r.findings ?? []).map((f, i) => (
              <tr key={i} className="border-b border-slate-900 align-top">
                <td className="px-2 py-1 font-medium">{f.title}</td>
                <td className="px-2 py-1"><Badge tone={MODE_TONE[f.failure_mode] ?? 'slate'}>{f.failure_mode}</Badge></td>
                <td className="px-2 py-1 text-slate-300">{f.magnitude}</td>
                <td className="px-2 py-1">{fmt.n(f.confidence, 2)}</td>
              </tr>
            ))}
          </Table>
          {(r.not_explained ?? []).length > 0 && (
            <div><span className="text-slate-500">not explained: </span>{(r.not_explained ?? []).join(' · ')}</div>
          )}
        </div>
      )}
      {(r.hypotheses ?? []).length > 0 && (
        <div className="mt-2 text-[11px] text-slate-400">
          {(r.hypotheses ?? []).length} hypothesis{(r.hypotheses ?? []).length > 1 ? 'es' : ''} proposed — see the Hypotheses table to run the experiment.
        </div>
      )}
      {r.run && (
        <details className="mt-2 text-[11px]">
          <summary className="cursor-pointer text-slate-400">
            analyst reports and debate ({r.run.calls} calls, {r.run.seconds}s)
          </summary>
          <pre className="mt-2 whitespace-pre-wrap text-slate-300">{JSON.stringify({ analysts: r.run.analysts, debate: r.run.debate }, null, 1)}</pre>
        </details>
      )}
      {r.pack && (
        <details className="mt-2 text-[11px]">
          <summary className="cursor-pointer text-slate-400">evidence pack (what the committee saw — every number it may cite)</summary>
          <pre className="mt-2 whitespace-pre-wrap text-slate-300">{JSON.stringify(r.pack, null, 1)}</pre>
        </details>
      )}
    </Card>
  )
}

/**
 * The review committee: post-mortems on the algo's own decisions, with the experiment loop that
 * grades every proposal by running the backtester. The forensic tables need no key — they are
 * the debugging tool; the committee adds the reading and the hypotheses.
 */
export function Committee() {
  const status = usePoll<CommitteeStatus>('/api/committee/status', 5000)
  const backtests = usePoll<{ id: string; trades: number; avg_r: number; created_ts: number }[]>('/api/backtests', 30000)
  const [source, setSource] = useState('ledger')
  const [autoSourced, setAutoSourced] = useState(false)
  const [strategy, setStrategy] = useState('')
  const forensics = usePoll<Forensics>(`/api/committee/forensics?source=${encodeURIComponent(source)}&strategy=${strategy}`, 15000)
  const reviews = usePoll<ReviewRow[]>('/api/committee/reviews?limit=100', 8000)
  const hyps = usePoll<HypothesisRow[]>('/api/committee/hypotheses', 8000)
  const [open, setOpen] = useState<ReviewRow | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const s = status.data
  const c = forensics.data?.cohort ?? {}
  const n = (c.n as number) ?? 0
  // An empty ledger is the normal state before the first paper trade; the newest backtest is the
  // cohort worth reading then. Chosen once, so a person's own choice is never overridden.
  if (!autoSourced && source === 'ledger' && forensics.data && n === 0 && (backtests.data ?? []).length > 0) {
    setAutoSourced(true)
    setSource(`backtest:${backtests.data![0].id}`)
  }

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true)
    setMsg(label)
    try {
      await fn()
      setMsg('')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      void reviews.refresh()
      void hyps.refresh()
    }
  }
  const openReview = (id: string) => void getJson<ReviewRow>(`/api/committee/reviews/${id}`).then(setOpen).catch((e) => setMsg(String(e)))
  const askCohort = () =>
    act('asking the committee (4 Claude calls)…', async () => {
      const r = await postJson<ReviewRow>('/api/committee/review/cohort', { source, strategy: strategy || null })
      openReview(r.id)
    })
  const runExperiment = (id: string) => act('starting the experiment (two backtests in a worker thread)…', () => postJson('/api/committee/experiments/run', { hypothesis_id: id }))

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={status.error ?? forensics.error ?? reviews.error} />
      <div className="flex flex-wrap gap-3">
        <Stat label="committee" value={s ? (s.available ? (s.auto ? 'auto' : 'on demand') : 'off') : 'DM'} sub={s ? (s.available ? `${s.model} · ${s.runs_today}/${s.max_runs_per_day} runs today` : 'set KN_ANTHROPIC_API_KEY to enable') : ''} />
        <Stat label="est. spend" value={s?.llm ? `$${s.llm.est_cost_usd.toFixed(2)}` : 'DM'} sub={s?.llm ? `${s.llm.calls} calls · ${s.llm.errors} errors` : 'forensics are free'} />
        <Stat label="reviews" value={s ? s.log.entries : 'DM'} sub={s ? Object.entries(s.log.by_failure_mode).slice(0, 3).map(([k, v]) => `${k} ${v}`).join(' · ') || 'no verdicts yet' : ''} />
        <Stat label="hypotheses" value={s ? s.log.hypotheses : 'DM'} sub={s ? Object.entries(s.log.hypotheses_by_status).map(([k, v]) => `${k} ${v}`).join(' · ') || '—' : ''} />
        {s?.last_error && <Stat label="last error" value={<span className="text-rose-400">{s.last_error.slice(0, 60)}</span>} tone="text-rose-400" />}
      </div>

      <Card
        title="Forensics"
        right={
          <div className="flex items-center gap-2 text-[11px]">
            <select className="rounded bg-slate-900 px-2 py-1 text-slate-200" value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="ledger">ledger (live / paper trades)</option>
              {(backtests.data ?? []).map((b) => (
                <option key={b.id} value={`backtest:${b.id}`}>
                  {b.id} · {b.trades} trades · avg R {b.avg_r}
                </option>
              ))}
            </select>
            <select className="rounded bg-slate-900 px-2 py-1 text-slate-200" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              <option value="">both books</option>
              <option value="FUDKII">FUDKII</option>
              <option value="FUKAA">FUKAA</option>
            </select>
            <button disabled={busy || !s?.available || n === 0} className="rounded bg-violet-900/60 px-2 py-1 text-violet-200 hover:bg-violet-800 disabled:opacity-40" onClick={askCohort}>
              ask the committee (4 calls)
            </button>
            <span className="text-slate-400">{msg}</span>
          </div>
        }
      >
        {n === 0 ? (
          <div className="text-[12px] text-slate-500">no trades in this source{strategy ? ` for ${strategy}` : ''} — pick a backtest run, or wait for the ledger to fill</div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-8">
              {HEADLINE.map(([k, label, f]) => (
                <div key={k} title={`cohort.${k}`}>
                  <div className="text-[10px] uppercase text-slate-500">{label}</div>
                  <div className={cls('text-sm font-semibold', k === 'avg_r' || k === 'net' ? ((c[k] as number) < 0 ? 'text-rose-400' : 'text-emerald-400') : 'text-slate-200')}>{c[k] == null ? 'DM' : f(c[k])}</div>
                </div>
              ))}
            </div>
            <div className="mt-1 text-[10px] text-slate-500">
              {c.first_day as string} → {c.last_day as string}
              {c.too_small ? ' · too small to conclude (<30 trades or <10 days)' : ''} · dim rows = too small · every number here is what the committee is allowed to cite
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {DIM_ORDER.filter((d) => (forensics.data?.dims[d] ?? []).length > 0).map((d) => (
                <DimTable key={d} name={d} rows={forensics.data!.dims[d]} />
              ))}
            </div>
          </>
        )}
      </Card>

      <Hypotheses rows={hyps.data ?? []} onRun={runExperiment} busy={busy} onProposed={() => void hyps.refresh()} />

      {open && <ReviewDetail r={open} onClose={() => setOpen(null)} />}

      <Card title="Reviews" right="click a row for the full post-mortem, the debate and the evidence pack">
        <Table head={['time (IST)', 'kind', 'book', 'subject', 'verdict', 'confidence', 'lesson', 'hyp.']} empty="no reviews yet — ask the committee about a cohort above, or about one signal from the Signals page">
          {(reviews.data ?? []).map((r) => {
            const subject = (r.subject ?? {}) as { ref?: string; source?: string }
            return (
              <tr key={r.id} onClick={() => openReview(r.id)} className={cls('cursor-pointer border-b border-slate-900 align-top hover:bg-slate-900/60', open?.id === r.id && 'bg-slate-900')}>
                <td className="px-2 py-1.5 text-slate-500">{ist(r.ts)}</td>
                <td className="px-2 py-1.5">{r.kind}</td>
                <td className="px-2 py-1.5">{r.strategy ? <StrategyBadge k={r.strategy} /> : 'both'}</td>
                <td className="px-2 py-1.5 font-mono text-[11px]">{r.symbol ?? ''} {subject.ref ?? subject.source ?? ''}</td>
                <td className="px-2 py-1.5">{r.error ? <Badge tone="red">error</Badge> : r.failure_mode ? <Badge tone={MODE_TONE[r.failure_mode] ?? 'slate'}>{r.failure_mode}</Badge> : r.kind === 'manual' ? <Badge tone="blue">proposal</Badge> : 'DM'}</td>
                <td className="px-2 py-1.5">{r.kind === 'manual' ? '—' : fmt.n(r.confidence, 2)}</td>
                <td className="max-w-[28rem] px-2 py-1.5 text-[11px] text-slate-400">{r.error ?? r.lesson ?? r.verdict ?? ''}</td>
                <td className="px-2 py-1.5">{(r.hypotheses ?? []).length || ''}</td>
              </tr>
            )
          })}
        </Table>
      </Card>
    </div>
  )
}
