import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { PortfolioData, SystemData } from '../lib/api';

function fmt(n: number | undefined) {
  if (n === undefined) return '—';
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
  });
}

function pct(n: number) {
  const sign = n >= 0 ? '+' : '';
  return `${sign}${(n * 100).toFixed(2)}%`;
}

/** US market hours: 9:30–16:00 America/New_York, Mon–Fri. */
function isMarketOpen(now = new Date()): boolean {
  const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const day = et.getDay();
  if (day === 0 || day === 6) return false;
  const mins = et.getHours() * 60 + et.getMinutes();
  return mins >= 9 * 60 + 30 && mins < 16 * 60;
}

interface TileProps {
  label: string;
  children: React.ReactNode;
  isLast?: boolean;
}

function Tile({ label, children, isLast }: TileProps) {
  return (
    <div
      className={`relative px-5 py-4 md:px-6 md:py-5 ${
        isLast ? '' : 'md:border-r border-dim'
      } border-b md:border-b-0 border-dim`}
    >
      <div className="text-[10px] text-muted uppercase tracking-[0.2em] mb-2">
        {label}
      </div>
      <div className="flex items-baseline gap-2">{children}</div>
    </div>
  );
}

function Badge({
  text,
  color,
}: {
  text: string;
  color: string;
}) {
  return (
    <span
      className="inline-block px-2.5 py-1 rounded text-sm font-bold tracking-[0.15em]"
      style={{
        color,
        backgroundColor: `${color}14`,
        border: `1px solid ${color}30`,
      }}
    >
      {text}
    </span>
  );
}

export default function TopStatsBar() {
  const portfolio = useApi<PortfolioData>(api.portfolio);
  const system = useApi<SystemData>(api.system);

  const acct = system.data?.ibkr_account ?? '';
  const isLive = acct.length > 0 && !acct.startsWith('DU');
  const marketOpen = isMarketOpen();
  const pnl = portfolio.data?.daily_pnl ?? 0;
  const pnlPct = portfolio.data?.daily_pnl_pct ?? 0;
  const pnlColor = pnl >= 0 ? '#00dc82' : '#ff4757';
  const arrow = pnl >= 0 ? '▲' : '▼';

  return (
    <div className="mb-5 rounded-xl border border-dim bg-panel overflow-hidden">
      <div className="grid grid-cols-2 md:grid-cols-4">
        <Tile label="Mode">
          <Badge
            text={isLive ? 'LIVE' : 'PAPER'}
            color={isLive ? '#ff4757' : '#06b6d4'}
          />
        </Tile>

        <Tile label="Equity">
          <span className="text-xl md:text-2xl font-mono font-semibold tracking-tight">
            {fmt(portfolio.data?.equity)}
          </span>
          {portfolio.data && (
            <span
              className="text-[11px] font-mono px-1.5 py-0.5 rounded whitespace-nowrap"
              style={{
                color: pnlColor,
                backgroundColor: `${pnlColor}12`,
                border: `1px solid ${pnlColor}20`,
              }}
            >
              {arrow} {pct(pnlPct)}
            </span>
          )}
        </Tile>

        <Tile label="Cash">
          <span className="text-xl md:text-2xl font-mono font-semibold tracking-tight">
            {fmt(portfolio.data?.cash)}
          </span>
        </Tile>

        <Tile label="Market" isLast>
          <Badge
            text={marketOpen ? 'OPEN' : 'CLOSED'}
            color={marketOpen ? '#00dc82' : '#7c7f94'}
          />
        </Tile>
      </div>
    </div>
  );
}
