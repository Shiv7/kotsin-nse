import { useState } from 'react'
import { Badge, Card, ErrorLine, Stat, Table } from '../components/Ui'
import { fmt, getJson, ist, pnlColor } from '../lib/api'
import { usePoll } from '../lib/usePoll'

interface Summary {
  id: string
  created_ts: number
  symbols: number
  bars: number
  signals: number
  rejections: number
  trades: number
  gross: number
  charges: number
  net: number
  charges_share_of_gross: number | null
  win_rate: number | null
  avg_r: number
  avg_r_stderr: number | null
  avg_r_t: number | null
  n_days: number
  sample_too_small: boolean
  profit_factor: number | null
  max_drawdown: number
  by_strategy: Record<string, { trades: number; net: number; avg_r: number; win_rate: number }>
  by_exit_reason: Record<string, { n: number; net: number }>
  binding_gates: Record<string, number>
  modelled_option_net: number | null
  params: Record<string, unknown>
}

interface Detail {
  summary: Summary
  trades: {
    strategy: string
    symbol: string
    direction: string
    day: string
    entry_ts: number
    exit_ts: number
    entry: number
    exit: number
    qty: number
    gross: number
    charges: number
    net: number
    r_multiple: number
    mfe_r: number
    mae_r: number
    exit_reason: string
    grade: string
    opt_net_modelled: number | null
  }[]
}

/**
 * Backtests are run from the CLI (`kotsin-nse backtest`) and read here. A long sweep must not
 * compete with the trading loop for the same event loop.
 */
export function Backtest() {
  const { data, error } = usePoll<Summary[]>('/api/backtests', 15000)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [busy, setBusy] = useState('')

  const open = async (id: string) => {
    setBusy(id)
    try {
      setDetail(await getJson<Detail>(`/api/backtests/${id}`))
    } finally {
      setBusy('')
    }
  }

  const s = detail?.summary

  return (
    <div className="space-y-4 p-4">
      <ErrorLine error={error} />
      {!data?.length && (
        <Card title="No backtests yet">
          <pre className="whitespace-pre-wrap text-[11px] leading-relaxed text-slate-400">
{`cd backend
uv run kotsin-nse fetch-history --symbols RELIANCE,TCS,INFY --start 2025-09-01
uv run kotsin-nse backtest --symbols RELIANCE,TCS,INFY`}
          </pre>
          <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
            The backtester replays cached history through the same strategy, exit and cost code the
            engine runs — a hand-rolled replay is how the previous stack produced numbers that were
            wrong by −80% to +185%. It measures the <em>underlying</em>; the option leg is modelled
            and reported separately, because expired contracts leave the scrip master and their
            premiums cannot be recovered.
          </p>
        </Card>
      )}

      {!!data?.length && (
        <Card title="Runs" right="newest first">
          <Table head={['Run', 'When (IST)', 'Symbols', 'Trades', 'Net', 'Avg R', 't', 'Days', 'PF', 'Max DD', '']}>
            {data.map((r) => (
              <tr key={r.id} className="border-b border-slate-900">
                <td className="px-2 py-1.5 font-mono text-[11px]">{r.id}</td>
                <td className="px-2 py-1.5 text-slate-500">{ist(r.created_ts)}</td>
                <td className="px-2 py-1.5">{r.symbols}</td>
                <td className="px-2 py-1.5">{r.trades}</td>
                <td className={`px-2 py-1.5 ${pnlColor(r.net)}`}>{fmt.signedInr(r.net)}</td>
                <td className={`px-2 py-1.5 ${pnlColor(r.avg_r)}`}>{fmt.r(r.avg_r)}</td>
                <td className="px-2 py-1.5 text-slate-400">{r.avg_r_t?.toFixed(2) ?? 'DM'}</td>
                <td className="px-2 py-1.5 text-slate-400">{r.n_days}</td>
                <td className="px-2 py-1.5">{r.profit_factor?.toFixed(2) ?? 'DM'}</td>
                <td className="px-2 py-1.5 text-rose-400/80">{fmt.inr(r.max_drawdown)}</td>
                <td className="px-2 py-1.5">
                  <button
                    onClick={() => void open(r.id)}
                    className="rounded bg-slate-800 px-2 py-0.5 text-[10px] hover:bg-slate-700"
                  >
                    {busy === r.id ? '…' : 'open'}
                  </button>
                </td>
              </tr>
            ))}
          </Table>
        </Card>
      )}

      {s && (
        <>
          {s.sample_too_small && (
            <div className="rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
              {s.trades} trades over {s.n_days} days is too small to conclude anything. The bar is
              ≥300 out-of-sample trades and a within-day permutation test — see docs/LEARNINGS.md R13.
            </div>
          )}

          <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
            <Stat label="Trades" value={s.trades} sub={`${s.signals} signals · ${s.rejections} rejected`} />
            <Stat label="Gross" value={fmt.signedInr(s.gross)} tone={pnlColor(s.gross)} />
            <Stat label="Charges" value={fmt.inr(s.charges)} tone="text-amber-400" sub={s.charges_share_of_gross != null ? `${s.charges_share_of_gross}% of gross` : undefined} />
            <Stat label="Net" value={fmt.signedInr(s.net)} tone={pnlColor(s.net)} />
            <Stat label="Avg R" value={fmt.r(s.avg_r)} sub={s.avg_r_stderr != null ? `± ${s.avg_r_stderr} (day-clustered)` : undefined} tone={pnlColor(s.avg_r)} />
            <Stat
              label="Option leg"
              value={s.modelled_option_net != null ? fmt.signedInr(s.modelled_option_net) : 'DM'}
              sub="MODELLED — not measured"
              tone="text-slate-400"
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card title="By book">
              <Table head={['Book', 'Trades', 'Net', 'Avg R', 'Win %']}>
                {Object.entries(s.by_strategy).map(([k, v]) => (
                  <tr key={k} className="border-b border-slate-900">
                    <td className="px-2 py-1.5">
                      <Badge tone={k === 'FUKAA' ? 'violet' : 'blue'}>{k}</Badge>
                    </td>
                    <td className="px-2 py-1.5">{v.trades}</td>
                    <td className={`px-2 py-1.5 ${pnlColor(v.net)}`}>{fmt.signedInr(v.net)}</td>
                    <td className={`px-2 py-1.5 ${pnlColor(v.avg_r)}`}>{fmt.r(v.avg_r)}</td>
                    <td className="px-2 py-1.5">{v.win_rate}%</td>
                  </tr>
                ))}
              </Table>
            </Card>
            <Card title="By exit reason">
              <Table head={['Reason', 'n', 'Net']}>
                {Object.entries(s.by_exit_reason).map(([k, v]) => (
                  <tr key={k} className="border-b border-slate-900">
                    <td className="px-2 py-1.5">{k}</td>
                    <td className="px-2 py-1.5">{v.n}</td>
                    <td className={`px-2 py-1.5 ${pnlColor(v.net)}`}>{fmt.signedInr(v.net)}</td>
                  </tr>
                ))}
              </Table>
            </Card>
            <Card title="Binding gates" right="why candidates did not become trades">
              <Table head={['Gate', 'Count']}>
                {Object.entries(s.binding_gates)
                  .slice(0, 12)
                  .map(([k, v]) => (
                    <tr key={k} className="border-b border-slate-900">
                      <td className="px-2 py-1.5 font-mono text-[11px]">{k}</td>
                      <td className="px-2 py-1.5">{v}</td>
                    </tr>
                  ))}
              </Table>
            </Card>
          </div>

          <Card title="Trades" right={`${detail.trades.length} rows`}>
            <Table head={['Day', 'Book', 'Symbol', 'Dir', 'Entry', 'Exit', 'Qty', 'Gross', 'Charges', 'Net', 'R', 'MFE', 'MAE', 'Exit', 'Grade']}>
              {detail.trades.slice(0, 400).map((t, i) => (
                <tr key={`${t.symbol}-${t.entry_ts}-${i}`} className="border-b border-slate-900">
                  <td className="px-2 py-1.5 text-slate-500">{t.day}</td>
                  <td className="px-2 py-1.5">
                    <Badge tone={t.strategy === 'FUKAA' ? 'violet' : 'blue'}>{t.strategy}</Badge>
                  </td>
                  <td className="px-2 py-1.5 font-medium">{t.symbol}</td>
                  <td className={`px-2 py-1.5 ${t.direction === 'BULLISH' ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {t.direction === 'BULLISH' ? 'L' : 'S'}
                  </td>
                  <td className="px-2 py-1.5">{fmt.n(t.entry)}</td>
                  <td className="px-2 py-1.5">{fmt.n(t.exit)}</td>
                  <td className="px-2 py-1.5">{t.qty}</td>
                  <td className={`px-2 py-1.5 ${pnlColor(t.gross)}`}>{fmt.signedInr(t.gross)}</td>
                  <td className="px-2 py-1.5 text-amber-400/80">{fmt.inr(t.charges)}</td>
                  <td className={`px-2 py-1.5 font-medium ${pnlColor(t.net)}`}>{fmt.signedInr(t.net)}</td>
                  <td className={`px-2 py-1.5 ${pnlColor(t.r_multiple)}`}>{fmt.r(t.r_multiple)}</td>
                  <td className="px-2 py-1.5 text-emerald-500/70">{fmt.r(t.mfe_r)}</td>
                  <td className="px-2 py-1.5 text-rose-500/70">{fmt.r(t.mae_r)}</td>
                  <td className="px-2 py-1.5 text-slate-400">{t.exit_reason}</td>
                  <td className="px-2 py-1.5 text-slate-500">{t.grade}</td>
                </tr>
              ))}
            </Table>
          </Card>
        </>
      )}
    </div>
  )
}
