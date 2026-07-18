import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { PortfolioData } from '../lib/api';

function fmt(n: number) {
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

function pct(n: number) {
  return `${(n * 100).toFixed(2)}%`;
}

export default function PortfolioCard() {
  const { data } = useApi<PortfolioData>(api.portfolio);

  if (!data) {
    return (
      <div className="panel" style={{ animationDelay: '80ms' }}>
        <div className="flex items-center gap-2 mb-4">
          <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
            Portfolio
          </span>
        </div>
        <div className="skeleton h-5 w-32 mb-4" />
        <div className="grid grid-cols-2 gap-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton h-10 w-full" />
          ))}
        </div>
      </div>
    );
  }

  const isPositive = data.daily_pnl >= 0;
  const pnlColor = isPositive ? '#00dc82' : '#ff4757';
  const arrow = isPositive ? '▲' : '▼';

  return (
    <div className="panel" style={{ animationDelay: '80ms' }}>
      <div className="flex items-center justify-between mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Portfolio
        </span>
        <div className="flex items-center gap-2">
          <span
            className="text-[11px] font-mono font-medium"
            style={{ color: pnlColor }}
          >
            {arrow} {fmt(data.daily_pnl)}
          </span>
          <span
            className="text-[10px] font-mono px-1.5 py-0.5 rounded"
            style={{
              backgroundColor: `${pnlColor}12`,
              color: pnlColor,
              border: `1px solid ${pnlColor}20`,
            }}
          >
            {pct(data.daily_pnl_pct)}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {[
          { label: 'Buying Power', value: fmt(data.buying_power) },
          { label: 'Allocation', value: pct(data.allocation_pct) },
          { label: 'Leverage', value: `${data.leverage.toFixed(2)}x` },
          { label: 'Cash', value: fmt(data.cash) },
        ].map(({ label, value }) => (
          <div
            key={label}
            className="px-3 py-2.5 rounded-lg bg-elevated/40 border border-dim"
          >
            <div className="text-[9px] text-muted uppercase tracking-[0.2em] mb-1">
              {label}
            </div>
            <div className="text-sm font-mono font-semibold">{value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
