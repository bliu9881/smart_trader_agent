import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { PositionData } from '../lib/api';

function fmt(n: number) {
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export default function PositionsTable() {
  const { data } = useApi<PositionData[]>(api.positions);

  return (
    <div className="panel h-full" style={{ animationDelay: '280ms' }}>
      <div className="flex items-center gap-2 mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Positions
        </span>
        {data && data.length > 0 && (
          <span className="text-[10px] font-mono text-faint bg-elevated px-1.5 py-0.5 rounded">
            {data.length}
          </span>
        )}
      </div>

      {!data || data.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-muted text-sm">
          No open positions
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-muted border-b border-dim">
                <th className="text-left py-2 pr-3 font-medium tracking-wider uppercase text-[10px]">
                  Symbol
                </th>
                <th className="text-right py-2 pr-3 font-medium tracking-wider uppercase text-[10px]">
                  Qty
                </th>
                <th className="text-right py-2 pr-3 font-medium tracking-wider uppercase text-[10px]">
                  Avg Cost
                </th>
                <th className="text-right py-2 pr-3 font-medium tracking-wider uppercase text-[10px]">
                  Price
                </th>
                <th className="text-right py-2 pr-3 font-medium tracking-wider uppercase text-[10px]">
                  Value
                </th>
                <th
                  className="text-right py-2 pr-3 font-medium tracking-wider uppercase text-[10px]"
                  title="Current trailing-stop trigger price — position will sell if market drops to this level"
                >
                  Stop
                </th>
                <th className="text-right py-2 font-medium tracking-wider uppercase text-[10px]">
                  P&amp;L
                </th>
              </tr>
            </thead>
            <tbody>
              {data.map((p) => {
                const isPositive = p.unrealized_pnl >= 0;
                const pnlColor = isPositive ? '#00dc82' : '#ff4757';

                // Distance from current price to stop trigger (negative = below current)
                let stopDistancePct: number | null = null;
                if (p.stop_price != null && p.market_price > 0) {
                  stopDistancePct = (p.stop_price - p.market_price) / p.market_price;
                }

                return (
                  <tr
                    key={p.symbol}
                    className="border-b border-dim/50 hover:bg-elevated/50 transition-colors"
                  >
                    <td className="py-2.5 pr-3 font-mono font-bold">{p.symbol}</td>
                    <td className="text-right py-2.5 pr-3 font-mono">{p.quantity}</td>
                    <td className="text-right py-2.5 pr-3 font-mono text-muted">
                      {fmt(p.avg_cost)}
                    </td>
                    <td className="text-right py-2.5 pr-3 font-mono">{fmt(p.market_price)}</td>
                    <td className="text-right py-2.5 pr-3 font-mono text-muted">
                      {fmt(p.market_value)}
                    </td>
                    <td className="text-right py-2.5 pr-3">
                      {p.stop_price != null ? (
                        <div className="flex flex-col items-end leading-tight">
                          <span className="font-mono text-negative">{fmt(p.stop_price)}</span>
                          {stopDistancePct != null && (
                            <span className="font-mono text-[9px] text-faint">
                              {(stopDistancePct * 100).toFixed(2)}%
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="font-mono text-faint">—</span>
                      )}
                    </td>
                    <td className="text-right py-2.5">
                      <span className="font-mono font-medium" style={{ color: pnlColor }}>
                        {isPositive ? '+' : ''}
                        {fmt(p.unrealized_pnl)}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
