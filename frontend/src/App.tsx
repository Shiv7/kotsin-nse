import { BrowserRouter, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ModeBanner } from './components/ModeBanner'
import { usePoll } from './lib/usePoll'
import { Analysis } from './pages/Analysis'
import { Board } from './pages/Board'
import { Chart } from './pages/Chart'
import { Journal } from './pages/Journal'
import { ObBacktest } from './pages/ObBacktest'
import { Options } from './pages/Options'
import { Overview } from './pages/Overview'
import { System } from './pages/System'
import { Universe } from './pages/Universe'
import type { Health } from './types'

const PAGES = [
  ['/', 'Overview'],
  ['/board', 'Board'],
  ['/analysis', 'Analysis'],
  ['/backtest', 'Backtest'],
  ['/chart', 'Chart'],
  ['/journal', 'Journal'],
  ['/options', 'Options'],
  ['/universe', 'Universe'],
  ['/system', 'System'],
] as const

function Pages() {
  const { pathname } = useLocation()
  return (
    <ErrorBoundary page={pathname}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/board" element={<Board />} />
        <Route path="/analysis" element={<Analysis />} />
        <Route path="/analysis/:symbol" element={<Analysis />} />
        <Route path="/backtest" element={<ObBacktest />} />
        <Route path="/chart" element={<Chart />} />
        <Route path="/journal" element={<Journal />} />
        <Route path="/options" element={<Options />} />
        <Route path="/universe" element={<Universe />} />
        <Route path="/system" element={<System />} />
      </Routes>
    </ErrorBoundary>
  )
}

export default function App() {
  const { data: health, error } = usePoll<Health>('/api/health', 4000)
  return (
    <BrowserRouter>
      <ModeBanner health={health} error={error} />
      <div className="flex min-h-screen">
        <nav className="w-40 shrink-0 space-y-1 border-r border-slate-800 p-3">
          <div className="px-2 pb-3 text-sm font-bold tracking-wide text-slate-200">kotsin-nse</div>
          {PAGES.map(([to, label]) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `block rounded px-2 py-1 text-sm ${isActive ? 'bg-slate-800 text-white' : 'text-slate-400 hover:text-white'}`
              }
            >
              {label}
            </NavLink>
          ))}
          <div className="px-2 pt-4 text-[10px] leading-relaxed text-slate-600">
            base · no strategy
            <br />
            NSE + MCX on 5paisa
          </div>
        </nav>
        <main className="min-w-0 flex-1">
          <Pages />
        </main>
      </div>
    </BrowserRouter>
  )
}
