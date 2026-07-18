import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { AgentCommentaryData } from '../lib/api';

export default function AgentCommentaryCard() {
  const { data } = useApi<AgentCommentaryData>(api.agentCommentary);

  return (
    <div className="panel" style={{ animationDelay: '500ms' }}>
      <div className="flex items-center gap-2 mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          Agent Commentary
        </span>
        {data?.status === 'available' && data.cycle_number != null && (
          <span className="text-[10px] font-mono text-faint bg-elevated px-1.5 py-0.5 rounded">
            Cycle {data.cycle_number}
          </span>
        )}
      </div>

      {!data || data.status === 'unavailable' ? (
        <div className="text-[11px] text-faint italic">
          AI commentary unavailable
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-[11px] text-secondary leading-relaxed whitespace-pre-wrap">
            {data.content}
          </p>
          {data.timestamp && (
            <p className="text-[10px] text-faint font-mono">
              {new Date(data.timestamp).toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit',
              })}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
