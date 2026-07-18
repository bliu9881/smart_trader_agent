import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { PortfolioData, RiskData } from '../lib/api';

function pctStr(n: number) {
  const sign = n >= 0 ? '+' : '';
  return `${sign}${(n * 100).toFixed(2)}%`;
}

/**
 * A tile with a label, a formatted value, and a thin progress bar
 * showing how close this metric is to its circuit-breaker limit.
 * When `limit` is undefined the tile is treated as neutral (no bar).
 */
function RiskTile({
  label,
  value,
  raw,
  limit,
  neutral,
}: {
  label: string;
  value: string;
  raw?: number;
  limit?: number;
  neutral?: boolean;
}) {
  let color = '#00dc82';
  let barPct = 0;

  if (!neutral && limit !== undefined && raw !== undefined) {
    barPct = Math.min((Math.abs(raw) / Math.abs(limit)) * 100, 100);
    const severity = barPct / 100;
    color = severity >= 0.85 ? '#ff4757' : severity >= 0.6 ? '#fcc419' : '#00dc82';
  }

  return (
    <div className="px-3 py-3 rounded-lg bg-elevated/40 border border-dim">
      <div className="text-[9px] text-muted uppercase tracking-[0.2em] mb-1.5">
        {label}
      </div>
      <div
        className="text-base font-mono font-semibold mb-2"
        style={{ color: neutral ? undefined : color }}
      >
        {value}
      </div>
      {!neutral && (
        <div className="w-full h-1 rounded-full bg-dim overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-700 ease-out"
            style={{
              width: `${barPct}%`,
              background: `linear-gradient(90deg, ${color}80, ${color})`,
              boxShadow: barPct > 50 ? `0 0 6px ${color}40` : 'none',
            }}
          />
        </div>
      )}
    </div>
  );
}

export default function RiskStatus() {
  const { data } = useApi<RiskData>(api.risk);
  const portfolio = useApi<PortfolioData>(api.portfolio);

  if (!data || !data.status) {
    return (
      <div className="panel" style={{ animationDelay: '160ms' }}>
        <div className="flex items-center gap-2 mb-4">
          <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
            Risk Status
          </span>
        </div>
        <div className="grid grid-cols-3 gap-2 mb-4">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton h-20 w-full" />
          ))}
        </div>
        <div className="skeleton h-10 w-full" />
      </div>
    );
  }

  const status = data.is_halted
    ? 'HALTED'
    : data.is_closed
      ? 'CLOSED'
      : data.is_half_size
        ? 'HALF SIZE'
        : 'NORMAL';

  const allClear = status === 'NORMAL';
  const statusColor = data.is_halted
    ? '#ff2d55'
    : data.is_closed
      ? '#ff4757'
      : data.is_half_size
        ? '#fcc419'
        : '#00dc82';

  const statusMessage = allClear
    ? 'All circuit breakers clear'
    : data.is_halted
      ? 'Trading halted — lock file present'
      : data.is_closed
        ? 'All positions closed — daily loss limit'
        : 'Half-size mode — elevated drawdown';

  return (
    <div className="panel" style={{ animationDelay: '160ms' }}>
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Risk Status
        </span>
        <span
          className="px-2 py-0.5 rounded text-[10px] font-bold tracking-[0.15em] uppercase"
          style={{
            color: statusColor,
            backgroundColor: `${statusColor}14`,
            border: `1px solid ${statusColor}30`,
          }}
        >
          {status}
        </span>
      </div>

      {/* Risk tiles grid */}
      <div className="grid grid-cols-3 gap-2 mb-4">
        <RiskTile
          label="Daily DD"
          value={pctStr(data.daily_pnl)}
          raw={data.daily_pnl}
          limit={-0.03}
        />
        <RiskTile
          label="Peak DD"
          value={pctStr(data.peak_drawdown)}
          raw={data.peak_drawdown}
          limit={-0.10}
        />
        <RiskTile
          label="Leverage"
          value={
            portfolio.data
              ? `${portfolio.data.leverage.toFixed(2)}x`
              : '—'
          }
          neutral
        />
      </div>

      {/* Status banner */}
      <div
        className="flex items-center gap-2.5 p-3 rounded-lg text-[12px] font-medium"
        style={{
          backgroundColor: `${statusColor}0d`,
          border: `1px solid ${statusColor}22`,
          color: statusColor,
        }}
      >
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{
            backgroundColor: statusColor,
            boxShadow: `0 0 8px ${statusColor}80`,
            animation: allClear ? 'glowPulse 2s ease-in-out infinite' : undefined,
          }}
        />
        <span>{statusMessage}</span>
      </div>

      {/* Weekly DD as fine-print */}
      <div className="mt-3 flex justify-between items-center text-[10px] font-mono text-faint tracking-wider uppercase">
        <span>Weekly DD</span>
        <span className="text-muted">{pctStr(data.weekly_pnl)} / -7.00%</span>
      </div>
    </div>
  );
}
