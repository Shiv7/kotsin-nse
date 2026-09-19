export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`)
  return (await res.json()) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* the body was not JSON; the status line is all we have */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

const IST = 'Asia/Kolkata'

/** Everything the engine stores is UTC epoch seconds; everything a person reads is IST. */
export function ist(ts: number | null | undefined, withSeconds = false): string {
  if (ts == null) return 'DM'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: IST,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
    hour12: false,
  })
    .format(new Date(ts * 1000))
    .replace(',', '')
}

export function istTime(ts: number | null | undefined): string {
  if (ts == null) return 'DM'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: IST,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(ts * 1000))
}

/** 'DM' = data missing. The old dashboard's convention, kept: a blank cell and a zero are
 * indistinguishable, and that ambiguity has cost real debugging time. */
export const fmt = {
  n: (v: number | null | undefined, d = 2) =>
    v == null || Number.isNaN(v) ? 'DM' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }),
  int: (v: number | null | undefined) => (v == null ? 'DM' : Math.round(v).toLocaleString('en-IN')),
  inr: (v: number | null | undefined, d = 0) =>
    v == null ? 'DM' : `${v < 0 ? '-' : ''}₹${Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })}`,
  signedInr: (v: number | null | undefined) =>
    v == null ? 'DM' : `${v >= 0 ? '+' : '−'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`,
  pct: (v: number | null | undefined, d = 2) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`),
  r: (v: number | null | undefined) => (v == null ? 'DM' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`),
  dur: (s: number | null | undefined) => {
    if (s == null) return 'DM'
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    return h ? `${h}h${String(m).padStart(2, '0')}m` : `${m}m`
  },
}

export const cls = (...xs: (string | false | null | undefined)[]) => xs.filter(Boolean).join(' ')

export const pnlColor = (v: number | null | undefined) =>
  v == null ? 'text-slate-400' : v > 0 ? 'text-emerald-400' : v < 0 ? 'text-rose-400' : 'text-slate-300'
