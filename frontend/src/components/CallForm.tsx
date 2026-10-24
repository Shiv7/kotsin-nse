import { useState } from 'react'
import { cls, fmt, postJson } from '../lib/api'
import { usePoll } from '../lib/usePoll'
import type { JournalCall, JournalView } from '../types'
import { Card } from './Ui'

const input = 'w-24 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200 placeholder:text-slate-600'

/** Log a discretionary call the moment it is seen. The engine scores it from the 1m bars that follow
 * (fill, stop / target, R at 15 / 30 / 60 min and at the close) — the only honest measure of a method
 * that lives in the trader's judgement. Entry blank = the last price on the screen. */
export function CallForm({ symbol, price, onLogged }: { symbol: string; price: number | null; onLogged?: (c: JournalCall) => void }) {
  const meta = usePoll<JournalView>('/api/journal?limit=1', 0)
  const [side, setSide] = useState<'LONG' | 'SHORT'>('LONG')
  const [kind, setKind] = useState<'entry' | 'alert'>('entry')
  const [entry, setEntry] = useState('')
  const [stop, setStop] = useState('')
  const [target, setTarget] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const toggle = (t: string) => setTags((xs) => (xs.includes(t) ? xs.filter((x) => x !== t) : [...xs, t]))
  const num = (s: string) => (s.trim() === '' ? null : Number(s))

  const submit = async () => {
    if (!symbol) return
    setBusy(true)
    setMsg(null)
    try {
      const row = await postJson<JournalCall>('/api/journal', {
        symbol,
        side,
        kind,
        entry: num(entry),
        stop: num(stop),
        target: num(target),
        tags,
        note,
      })
      setMsg({ ok: true, text: `logged ${row.symbol} ${row.side} ${row.kind} @ ${fmt.n(row.entry)}${row.risk_is_default ? ' · no stop: default risk 0.5%' : ''}` })
      setStop('')
      setTarget('')
      setNote('')
      onLogged?.(row)
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title={`Log a call · ${symbol || '—'}`} right="scored by the engine from the bars that follow — see Journal">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex overflow-hidden rounded border border-slate-700">
          {(['LONG', 'SHORT'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSide(s)}
              className={cls('px-2 py-1 text-xs', side === s ? (s === 'LONG' ? 'bg-emerald-900/70 text-emerald-200' : 'bg-rose-900/70 text-rose-200') : 'bg-slate-900 text-slate-400')}
            >
              {s}
            </button>
          ))}
        </div>
        <div className="flex overflow-hidden rounded border border-slate-700">
          {(['entry', 'alert'] as const).map((k) => (
            <button key={k} onClick={() => setKind(k)} className={cls('px-2 py-1 text-xs', kind === k ? 'bg-slate-700 text-white' : 'bg-slate-900 text-slate-400')} title={k === 'entry' ? 'I would take this trade' : 'watching — the first candle is an alert'}>
              {k}
            </button>
          ))}
        </div>
        <input className={input} placeholder={price != null ? `ltp ${fmt.n(price)}` : 'entry'} value={entry} onChange={(e) => setEntry(e.target.value)} inputMode="decimal" />
        <input className={input} placeholder="stop" value={stop} onChange={(e) => setStop(e.target.value)} inputMode="decimal" />
        <input className={input} placeholder="target" value={target} onChange={(e) => setTarget(e.target.value)} inputMode="decimal" />
        <input className={cls(input, 'w-56')} placeholder="note (why now)" value={note} onChange={(e) => setNote(e.target.value)} />
        <button onClick={() => void submit()} disabled={busy || !symbol} className="rounded bg-sky-800 px-3 py-1 text-xs font-semibold text-sky-100 disabled:opacity-50">
          {busy ? 'logging…' : 'log'}
        </button>
        {msg && <span className={cls('text-xs', msg.ok ? 'text-emerald-400' : 'text-rose-400')}>{msg.text}</span>}
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {(meta.data?.tags ?? []).map((t) => (
          <button key={t} onClick={() => toggle(t)} className={cls('rounded px-1.5 py-0.5 text-[10px]', tags.includes(t) ? 'bg-violet-900/70 text-violet-200' : 'bg-slate-800 text-slate-400')}>
            {t}
          </button>
        ))}
      </div>
    </Card>
  )
}
