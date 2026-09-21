import { useCallback, useEffect, useRef, useState } from 'react'
import { getJson } from './api'

/** Poll a JSON endpoint. Keeps the last good value on error so a blip does not blank the page,
 * and surfaces the error alongside it rather than instead of it. */
export function usePoll<T>(path: string | null, intervalMs = 4000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const alive = useRef(true)

  const refresh = useCallback(async () => {
    // `null` means there is nothing to poll yet. Callers used to pass an unrelated endpoint as a
    // placeholder, which then got parsed as the wrong type — /api/health's `bars` is a dict of
    // counters, and calling .map on it crashed the whole app, nav included.
    if (!path) {
      setLoading(false)
      return
    }
    try {
      const next = await getJson<T>(path)
      if (!alive.current) return
      setData(next)
      setError(null)
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [path])

  useEffect(() => {
    alive.current = true
    if (!path) {
      setLoading(false)
      return () => {
        alive.current = false
      }
    }
    void refresh()
    const id = setInterval(() => void refresh(), intervalMs)
    return () => {
      alive.current = false
      clearInterval(id)
    }
  }, [refresh, intervalMs, path])

  return { data, error, loading, refresh }
}
