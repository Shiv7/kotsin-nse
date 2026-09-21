import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * A crash on one page must not take the shell down with it.
 *
 * This is not cosmetic. React unmounts the whole tree on an uncaught render error, so a bug on the
 * Chart page blanked the entire application — including the navigation to the Risk page, which is
 * where the halt and kill buttons live. A trading UI that can lose its own kill switch to a
 * rendering bug is not acceptable; the boundary keeps the nav and the mode banner alive and
 * confines the failure to the page that caused it.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; page: string },
  { error: Error | null }
> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[kotsin-nse] page crashed', this.props.page, error, info.componentStack)
  }

  componentDidUpdate(prev: { page: string }) {
    if (prev.page !== this.props.page && this.state.error) this.setState({ error: null })
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="p-4">
        <div className="rounded border border-rose-900 bg-rose-950/40 p-4">
          <h2 className="text-sm font-semibold text-rose-200">This page failed to render</h2>
          <p className="mt-1 text-xs text-rose-300/80">
            The rest of the app is unaffected — Risk, and the kill switch, are still reachable.
          </p>
          <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-[11px] text-rose-300/70">
            {this.state.error.message}
          </pre>
        </div>
      </div>
    )
  }
}
