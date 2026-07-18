import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { SmartMoneyCandidate } from '../lib/api';

// Confidence-boost tiers — mirror TraderConfig.sm_bonus_{low,high}_conviction.
// Smart money is a SUPPLEMENT: conviction adds a confidence bonus to a
// technically-triggered swing setup; it does not open trades on its own.
const HIGH_CONVICTION = 6.0; // +0.25 confidence
const LOW_CONVICTION = 3.0; // +0.15 confidence

// Short display labels for the scanner sources.
const SOURCE_LABEL: Record<string, string> = {
  capitol_trades: 'CONGRESS',
  sec_edgar: 'INSIDER',
  insider_cluster: 'CLUSTER',
  berkshire_13f: 'BRK',
  ark_invest: 'ARK',
};

function tier(score: number): { label: string; text: string; bg: string } {
  if (score >= HIGH_CONVICTION) return { label: '+0.25', text: '#00dc82', bg: '#00dc8215' };
  if (score >= LOW_CONVICTION) return { label: '+0.15', text: '#e8a308', bg: '#e8a30815' };
  return { label: 'no boost', text: '#868e96', bg: '#868e9615' };
}

function daysAgo(iso: string): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const d = Math.max(0, Math.round((Date.now() - then) / 86_400_000));
  return d === 0 ? 'today' : `${d}d`;
}

export default function SmartMoneyCard() {
  const { data, error, loading } = useApi<SmartMoneyCandidate[]>(api.smartMoney);

  return (
    <div className="panel h-full" style={{ animationDelay: '500ms' }}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Smart Money
        </span>
        {data && data.length > 0 && (
          <span className="text-[10px] font-mono text-faint bg-elevated px-1.5 py-0.5 rounded">
            {data.length}
          </span>
        )}
      </div>
      <p className="text-[10px] text-faint mb-3 leading-relaxed">
        Confidence supplement — boosts swing setups, never trades standalone.
      </p>

      {error ? (
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-negative shadow-[0_0_6px_rgba(255,71,87,0.5)]" />
          <span className="text-negative text-sm font-medium">API unreachable</span>
        </div>
      ) : loading ? (
        <div className="skeleton h-24 w-full" />
      ) : !data || data.length === 0 ? (
        <div className="flex items-center justify-center py-10 text-muted text-sm">
          No candidates
        </div>
      ) : (
        <div className="space-y-0 max-h-[360px] overflow-y-auto pr-1">
          {data.slice(0, 12).map((c, i) => {
            const t = tier(c.conviction_score);
            const firstActor = c.actors?.[0] ?? '';
            const extraActors = (c.actors?.length ?? 0) - 1;
            return (
              <div
                key={`${c.symbol}-${i}`}
                className="py-2 border-b border-dim/30 hover:bg-elevated/30 transition-colors"
              >
                {/* Row 1: rank, symbol, conviction, boost tier */}
                <div className="flex items-center gap-2 text-[11px]">
                  <span className="text-faint font-mono w-4 shrink-0 text-[10px] text-right">
                    {i + 1}
                  </span>
                  <span className="font-mono font-bold w-12 shrink-0">{c.symbol}</span>
                  <span
                    className="font-mono font-semibold shrink-0 tabular-nums"
                    style={{ color: t.text }}
                  >
                    {c.conviction_score.toFixed(2)}
                  </span>
                  <span
                    className="px-1.5 py-0.5 rounded text-[9px] font-bold tracking-wider uppercase shrink-0"
                    style={{ color: t.text, backgroundColor: t.bg }}
                  >
                    {t.label}
                  </span>
                  <span className="font-mono text-[10px] text-faint ml-auto shrink-0">
                    {daysAgo(c.most_recent_filing)}
                  </span>
                </div>

                {/* Row 2: sources + actors */}
                <div className="flex items-center gap-1.5 text-[10px] mt-1 ml-6 flex-wrap">
                  {(c.sources ?? []).map((s) => (
                    <span
                      key={s}
                      className="px-1 py-0.5 rounded bg-[#7c3aed20] text-[#a78bfa] text-[9px] font-medium tracking-wide"
                    >
                      {SOURCE_LABEL[s] ?? s.toUpperCase()}
                    </span>
                  ))}
                  {firstActor && (
                    <span className="text-faint truncate">
                      {firstActor}
                      {extraActors > 0 ? ` +${extraActors}` : ''}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
