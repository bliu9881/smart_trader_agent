import { useState, useEffect } from 'react'
import { useApi } from './hooks/useApi'
import { api } from './lib/api'
import ErrorBoundary from './components/ErrorBoundary'
import PortfolioCard from './components/PortfolioCard'
import PositionsTable from './components/PositionsTable'
import SignalsFeed from './components/SignalsFeed'
import RiskStatus from './components/RiskStatus'
import SystemStatus from './components/SystemStatus'
import RegimeCard from './components/RegimeCard'
import SmartMoneyCard from './components/SmartMoneyCard'
import TopStatsBar from './components/TopStatsBar'
import AgentCommentaryCard from './components/AgentCommentaryCard'

type Theme = 'light' | 'dark'

export default function App() {
  const health = useApi(() => api.health(), 5000)
  const connected = !health.error && health.data?.status === 'ok'
  const [time, setTime] = useState(new Date())

  // Theme is initialized from the attribute the index.html pre-paint script set
  // (from localStorage, default dark), then kept in sync on toggle.
  const [theme, setTheme] = useState<Theme>(
    () => (document.documentElement.dataset.theme as Theme) || 'dark',
  )

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('theme', theme)
    } catch {
      /* ignore storage errors (private mode, etc.) */
    }
  }, [theme])

  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="min-h-screen bg-void font-display">
      {/* Ambient radial glow */}
      <div className="fixed inset-0 pointer-events-none" aria-hidden="true">
        <div
          className="absolute inset-0"
          style={{
            background:
              'radial-gradient(ellipse 80% 50% at 50% -10%, rgba(59,130,246,0.06) 0%, transparent 60%)',
          }}
        />
      </div>

      <div className="relative z-10 max-w-[1800px] mx-auto px-4 md:px-6 lg:px-8 py-5">
        {/* ——— Header ——— */}
        <header className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-accent/10 border border-accent/20 flex items-center justify-center shrink-0">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2L22 8.5V15.5L12 22L2 15.5V8.5L12 2Z" stroke="#e8a308" strokeWidth="1.5" fill="rgba(232,163,8,0.1)" />
                <path d="M12 8L17 11V17L12 20L7 17V11L12 8Z" fill="#e8a308" opacity="0.6" />
              </svg>
            </div>
            <div>
              <h1 className="text-base font-semibold tracking-[0.15em] uppercase">
                Smart <span className="text-accent">Trader</span>
              </h1>
              <p className="text-[10px] text-muted tracking-[0.2em] uppercase">
                Smart-Money + Risk-Gated Trading Terminal
              </p>
            </div>
          </div>

          <div className="flex items-center gap-5">
            <button
              type="button"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              className="w-8 h-8 rounded-lg border border-dim bg-elevated/40 hover:border-glow flex items-center justify-center text-muted hover:text-accent transition-colors shrink-0"
            >
              {theme === 'dark' ? (
                // Sun icon (click to go light)
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
                </svg>
              ) : (
                // Moon icon (click to go dark)
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
              )}
            </button>
            <span className="text-xs font-mono text-faint hidden sm:block">
              {time.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}{' '}
              {time.toLocaleTimeString('en-US', { hour12: false })}
            </span>
            <div className="flex items-center gap-2">
              <span
                className={`w-2 h-2 rounded-full ${
                  connected
                    ? 'bg-positive shadow-[0_0_8px_rgba(0,220,130,0.6)]'
                    : 'bg-negative shadow-[0_0_8px_rgba(255,71,87,0.6)]'
                }`}
                style={connected ? { animation: 'glowPulse 2s ease-in-out infinite' } : undefined}
              />
              <span className="text-[11px] font-medium tracking-wider uppercase text-muted">
                {connected ? 'Live' : 'Offline'}
              </span>
            </div>
          </div>
        </header>

        {/* Disconnected warning */}
        {!connected && (
          <div className="mb-5 panel border-accent/30 bg-accent/10">
            <div className="flex items-center gap-3 text-sm text-accent">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" />
                <line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
              <span>
                API unreachable — start the bot:{' '}
                <code className="font-mono text-accent bg-accent/15 px-1.5 py-0.5 rounded text-xs">
                  python3 -m smart_trader.main dry-run
                </code>
              </span>
            </div>
          </div>
        )}

        {/* Top stats strip: Mode / Equity / Cash / Market */}
        <ErrorBoundary name="TopStatsBar">
          <TopStatsBar />
        </ErrorBoundary>

        {/* Row 1: Portfolio (2 cols) + Risk (1 col) */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4 mt-4">
          <div className="lg:col-span-2">
            <ErrorBoundary name="PortfolioCard">
              <PortfolioCard />
            </ErrorBoundary>
          </div>
          <ErrorBoundary name="RiskStatus">
            <RiskStatus />
          </ErrorBoundary>
        </div>

        {/* Row 2: Positions + Signals */}
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-4 mb-4">
          <div className="lg:col-span-3">
            <ErrorBoundary name="PositionsTable">
              <PositionsTable />
            </ErrorBoundary>
          </div>
          <div className="lg:col-span-2">
            <ErrorBoundary name="SignalsFeed">
              <SignalsFeed />
            </ErrorBoundary>
          </div>
        </div>

        {/* Row 3: Smart Money + Market Regime + System + Agent Commentary */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
          <ErrorBoundary name="SmartMoneyCard">
            <SmartMoneyCard />
          </ErrorBoundary>
          <ErrorBoundary name="RegimeCard">
            <RegimeCard />
          </ErrorBoundary>
          <ErrorBoundary name="SystemStatus">
            <SystemStatus />
          </ErrorBoundary>
          <ErrorBoundary name="AgentCommentaryCard">
            <AgentCommentaryCard />
          </ErrorBoundary>
        </div>

        {/* ——— Footer ——— */}
        <footer className="mt-6 pt-4 border-t border-dim text-center">
          <p className="text-[10px] text-faint tracking-wider uppercase">
            Paper Trading Only — Not Financial Advice
          </p>
        </footer>
      </div>
    </div>
  )
}
