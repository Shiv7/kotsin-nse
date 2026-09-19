import type { ReactNode } from 'react'
import { cls } from '../lib/api'

export function Card({ title, right, children, className }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cls('rounded-lg border border-slate-800 bg-slate-900/40', className)}>
      {(title || right) && (
        <header className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          <div className="text-xs text-slate-400">{right}</div>
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  )
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={cls('text-lg font-semibold', tone ?? 'text-slate-100')}>{value}</div>
      {sub != null && <div className="text-[11px] text-slate-500">{sub}</div>}
    </div>
  )
}

export function Table({ head, children, empty }: { head: string[]; children: ReactNode; empty?: string }) {
  const rows = Array.isArray(children) ? children.flat() : children
  const isEmpty = Array.isArray(rows) ? rows.length === 0 : !rows
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-slate-800 text-slate-500">
            {head.map((h) => (
              <th key={h} className="whitespace-nowrap px-2 py-1.5 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {isEmpty ? (
            <tr>
              <td colSpan={head.length} className="px-2 py-6 text-center text-slate-600">
                {empty ?? 'nothing yet'}
              </td>
            </tr>
          ) : (
            rows
          )}
        </tbody>
      </table>
    </div>
  )
}

export function Badge({ children, tone = 'slate' }: { children: ReactNode; tone?: 'slate' | 'green' | 'red' | 'amber' | 'violet' | 'blue' }) {
  const tones = {
    slate: 'bg-slate-800 text-slate-300',
    green: 'bg-emerald-900/60 text-emerald-300',
    red: 'bg-rose-900/60 text-rose-300',
    amber: 'bg-amber-900/60 text-amber-300',
    violet: 'bg-violet-900/60 text-violet-300',
    blue: 'bg-sky-900/60 text-sky-300',
  }
  return <span className={cls('rounded px-1.5 py-0.5 text-[10px] font-medium', tones[tone])}>{children}</span>
}

export function GradeBadge({ grade }: { grade: string }) {
  const tone = grade === 'A' ? 'green' : grade === 'B' ? 'blue' : grade === 'C' ? 'amber' : grade === 'F' ? 'red' : 'slate'
  return <Badge tone={tone}>{grade || '—'}</Badge>
}

export function StrategyBadge({ k }: { k: string }) {
  return <Badge tone={k === 'FUKAA' ? 'violet' : 'blue'}>{k}</Badge>
}

export function ErrorLine({ error }: { error: string | null }) {
  if (!error) return null
  return <div className="mb-3 rounded border border-rose-900 bg-rose-950/50 px-3 py-2 text-xs text-rose-300">{error}</div>
}

export function Notes({ notes }: { notes: string[] }) {
  if (!notes?.length) return null
  return (
    <div className="mb-3 space-y-1">
      {notes.map((n) => (
        <div key={n} className="rounded border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
          {n}
        </div>
      ))}
    </div>
  )
}
