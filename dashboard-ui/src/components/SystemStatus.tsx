import { useApi } from '../hooks/useApi';
import { api } from '../lib/api';
import type { SystemData, AgentStatusData } from '../lib/api';

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

export default function SystemStatus() {
  const { data, error } = useApi<SystemData>(api.system);
  const { data: agentStatus } = useApi<AgentStatusData>(api.agentStatus);

  const agentDotColor = (status: string | undefined) => {
    switch (status) {
      case 'succeeded':
        return 'bg-positive shadow-[0_0_6px_rgba(0,220,130,0.5)]';
      case 'failed':
        return 'bg-accent shadow-[0_0_6px_rgba(232,163,8,0.5)]';
      case 'disabled':
      default:
        return 'bg-muted';
    }
  };

  const brokerLabel = data?.broker_mode === 'mock-broker' ? 'Mock Broker' : 'IBKR Paper';
  const brokerDotClass = data?.broker_mode === 'mock-broker'
    ? 'bg-accent shadow-[0_0_6px_rgba(232,163,8,0.4)]'
    : 'bg-positive shadow-[0_0_6px_rgba(0,220,130,0.5)]';

  return (
    <div className="panel" style={{ animationDelay: '440ms' }}>
      <div className="flex items-center gap-2 mb-4">
        <span className="text-[11px] font-medium tracking-[0.15em] uppercase text-muted">
          System
        </span>
      </div>

      {error ? (
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-negative shadow-[0_0_6px_rgba(255,71,87,0.5)]" />
          <span className="text-negative text-sm font-medium">API unreachable</span>
        </div>
      ) : !data ? (
        <div className="skeleton h-16 w-full" />
      ) : (
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-[11px]">
          <div className="flex items-center gap-2">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                data.ibkr_connected
                  ? 'bg-positive shadow-[0_0_6px_rgba(0,220,130,0.5)]'
                  : 'bg-negative shadow-[0_0_6px_rgba(255,71,87,0.5)]'
              }`}
            />
            <span className="text-muted">IBKR</span>
            <span className="font-mono ml-auto">
              {data.ibkr_connected ? data.ibkr_account : 'Disconnected'}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-accent shadow-[0_0_6px_rgba(232,163,8,0.4)]" />
            <span className="text-muted">Uptime</span>
            <span className="font-mono ml-auto">{formatUptime(data.uptime_seconds)}</span>
          </div>

          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-info shadow-[0_0_6px_rgba(6,182,212,0.4)]" />
            <span className="text-muted">Cycle</span>
            <span className="font-mono ml-auto">
              {data.last_cycle_seconds ? `${data.last_cycle_seconds.toFixed(1)}s` : '—'}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-muted" />
            <span className="text-muted">Next</span>
            <span className="font-mono ml-auto">
              {data.next_cycle_time
                ? new Date(data.next_cycle_time).toLocaleTimeString([], {
                    hour: '2-digit',
                    minute: '2-digit',
                  })
                : '--:--'}
            </span>
          </div>

          {/* Broker mode indicator */}
          <div className="flex items-center gap-2">
            <span className={`w-1.5 h-1.5 rounded-full ${brokerDotClass}`} />
            <span className="text-muted">Broker</span>
            <span className="font-mono ml-auto">{brokerLabel}</span>
          </div>

          {/* Qwen agent status indicators */}
          {agentStatus && (
            <>
              <div className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 rounded-full ${agentDotColor(agentStatus.catalyst)}`} />
                <span className="text-muted">Catalyst AI</span>
                <span className="font-mono ml-auto text-[10px]">
                  {agentStatus.catalyst === 'failed' && (
                    <span className="text-accent">⚠ </span>
                  )}
                  {agentStatus.catalyst}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 rounded-full ${agentDotColor(agentStatus.arbitration)}`} />
                <span className="text-muted">Arbitration AI</span>
                <span className="font-mono ml-auto text-[10px]">
                  {agentStatus.arbitration === 'failed' && (
                    <span className="text-accent">⚠ </span>
                  )}
                  {agentStatus.arbitration}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 rounded-full ${agentDotColor(agentStatus.commentary)}`} />
                <span className="text-muted">Commentary AI</span>
                <span className="font-mono ml-auto text-[10px]">
                  {agentStatus.commentary === 'failed' && (
                    <span className="text-accent">⚠ </span>
                  )}
                  {agentStatus.commentary}
                </span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
