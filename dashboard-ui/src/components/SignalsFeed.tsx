import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { SignalData } from '../lib/api';

const STATUS_STYLES: Record<string, { text: string; bg: string }> = {
  approved: { text: '#00dc82', bg: '#00dc8215' },
  rejected: { text: '#ff4757', bg: '#ff475715' },
  skipped: { text: '#868e96', bg: '#868e9615' },
  pending: { text: '#fcc419', bg: '#fcc41915' },
};

function fmt(n: number | undefined | null) {
  if (n == null) return '—';
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

export default function SignalsFeed() {
  const { data } = useApi<SignalData[]>(api.signals);

  return (
    <div className="panel h-full" style={{ animationDelay: '320ms' }}>
      <div className="flex items-center gap-2 mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Signal Feed
        </span>
        {data && data.length > 0 && (
          <span className="text-[10px] font-mono text-faint bg-elevated px-1.5 py-0.5 rounded">
            {data.length}
          </span>
        )}
      </div>

      {!data || data.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-muted text-sm">
          No signals yet
        </div>
      ) : (
        <div className="space-y-0 max-h-[360px] overflow-y-auto pr-1">
          {[...data]
            .reverse()
            .slice(0, 30)
            .map((s, i) => {
              const isLong = s.direction === 'LONG';
              const sc = STATUS_STYLES[s.status] ?? STATUS_STYLES.pending;

              return (
                <div
                  key={i}
                  className="py-2 border-b border-dim/30 hover:bg-elevated/30 transition-colors"
                >
                  {/* Row 1: time, direction dot, symbol, shares, status */}
                  <div className="flex items-center gap-2 text-[11px]">
                    <span className="text-faint font-mono w-12 shrink-0 text-[10px]">
                      {s.timestamp.split('T')[1]?.slice(0, 5) ?? ''}
                    </span>
                    <span
                      className="w-1 h-4 rounded-full shrink-0"
                      style={{ backgroundColor: isLong ? '#00dc82' : '#ff4757' }}
                    />
                    <span className="font-mono font-bold w-10 shrink-0">{s.symbol}</span>
                    {s.is_ladder_in && (
                      <span className="text-[9px] font-bold tracking-wider uppercase px-1 py-0.5 rounded bg-[#fcc41920] text-[#fcc419] shrink-0">
                        LADDER
                      </span>
                    )}
                    {s.is_smart_money && (
                      <span
                        className="text-[9px] font-bold tracking-wider uppercase px-1 py-0.5 rounded bg-[#7c3aed20] text-[#7c3aed] shrink-0"
                        title={`Conviction ${s.smart_money_conviction?.toFixed(1)} · ${s.smart_money_sources?.join(', ')}`}
                      >
                        SMART MONEY
                      </span>
                    )}
                    <span className="font-mono text-[10px] text-muted shrink-0">
                      {s.shares != null && s.shares > 0 ? `${s.shares} shr` : s.size_pct ?? ''}
                    </span>
                    <span className="font-mono text-[10px] text-muted shrink-0">
                      {s.entry_price != null ? `@ ${fmt(s.entry_price)}` : ''}
                    </span>
                    <span className="text-faint truncate flex-1 text-[10px]">
                      {s.strategy.replace('Strategy', '')}
                    </span>
                    <span
                      className="px-1.5 py-0.5 rounded text-[9px] font-bold tracking-wider uppercase shrink-0"
                      style={{ color: sc.text, backgroundColor: sc.bg }}
                    >
                      {s.status}
                    </span>
                  </div>

                  {/* Row 2: stop loss, modifications or rejection reason */}
                  <div className="flex items-center gap-2 text-[10px] mt-0.5 ml-[60px]">
                    {s.stop_loss != null && s.stop_loss > 0 && (
                      <span className="font-mono text-faint">
                        SL {fmt(s.stop_loss)}
                      </span>
                    )}
                    {s.status === 'rejected' && s.rejection_reason && (
                      <span className="text-[#ff4757] truncate">
                        {s.rejection_reason}
                      </span>
                    )}
                    {s.modifications && s.modifications.length > 0 && (
                      <span className="text-[#fcc419] truncate">
                        {s.modifications[0]}
                      </span>
                    )}
                  </div>

                  {/* Row 3: arbitration reasoning (when present) */}
                  {s.arbitration_reasoning && (
                    <div className="text-[10px] mt-0.5 ml-[60px] text-muted italic truncate">
                      {s.arbitration_reasoning}
                    </div>
                  )}
                </div>
              );
            })}
        </div>
      )}
    </div>
  );
}
