import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { RegimeData } from '../lib/api';

const ZONE_META: Record<
  RegimeData['zone'],
  { label: string; dot: string; text: string }
> = {
  bull: {
    label: 'Bull',
    dot: 'bg-positive shadow-[0_0_6px_rgba(0,220,130,0.5)]',
    text: 'text-positive',
  },
  ambiguous: {
    label: 'Ambiguous',
    dot: 'bg-accent shadow-[0_0_6px_rgba(232,163,8,0.5)]',
    text: 'text-accent',
  },
  bear: {
    label: 'Bear',
    dot: 'bg-negative shadow-[0_0_6px_rgba(255,71,87,0.5)]',
    text: 'text-negative',
  },
};

function Level({ label, value, above }: { label: string; value: number; above: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-muted">{label}</span>
      <span className="font-mono ml-auto tabular-nums">
        {value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
      </span>
      <span className={`text-[10px] font-medium ${above ? 'text-positive' : 'text-negative'}`}>
        {above ? '▲' : '▼'}
      </span>
    </div>
  );
}

export default function RegimeCard() {
  const { data, error, loading } = useApi<RegimeData | null>(api.regime);

  const zone = data ? ZONE_META[data.zone] : null;

  return (
    <div className="panel" style={{ animationDelay: '480ms' }}>
      <div className="flex items-center justify-between mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Market Regime
        </span>
        {data && (
          <span
            className={`text-[10px] font-semibold tracking-wider uppercase px-2 py-0.5 rounded ${
              data.entries_allowed
                ? 'text-positive bg-positive/10 border border-positive/20'
                : 'text-negative bg-negative/10 border border-negative/20'
            }`}
          >
            {data.entries_allowed ? 'Entries On' : 'Entries Off'}
          </span>
        )}
      </div>

      {error ? (
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-negative shadow-[0_0_6px_rgba(255,71,87,0.5)]" />
          <span className="text-negative text-sm font-medium">API unreachable</span>
        </div>
      ) : loading ? (
        <div className="skeleton h-20 w-full" />
      ) : !data || !zone ? (
        <div className="text-[11px] text-faint">
          Regime gate disabled or not yet computed.
        </div>
      ) : (
        <div className="space-y-3">
          {/* Zone + posture */}
          <div className="flex items-center gap-2.5">
            <span className={`w-2.5 h-2.5 rounded-full ${zone.dot}`} />
            <span className={`text-lg font-semibold ${zone.text}`}>{zone.label}</span>
            <span className="text-[10px] text-faint tracking-wider uppercase ml-auto">
              {data.posture} · 200-SMA {data.sma200_rising ? 'rising' : 'falling'}
            </span>
          </div>

          {/* SPY vs SMAs */}
          <div className="grid grid-cols-1 gap-y-1.5 text-[11px] pt-1">
            <div className="flex items-center gap-2">
              <span className="text-muted">SPY close</span>
              <span className="font-mono ml-auto tabular-nums">
                {data.close.toLocaleString('en-US', {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </span>
              <span className="text-[10px] w-3" />
            </div>
            <Level label="50-SMA" value={data.sma_50} above={data.above_50} />
            <Level label="200-SMA" value={data.sma_200} above={data.above_200} />
          </div>

          {/* Plain-language reason */}
          <p className="text-[10px] text-faint leading-relaxed border-t border-dim pt-2">
            {data.reason}
          </p>
        </div>
      )}
    </div>
  );
}
