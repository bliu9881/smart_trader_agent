const BASE = import.meta.env.VITE_API_URL ?? '';

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`API ${path}: ${res.status}`);
  return res.json();
}

export const api = {
  portfolio:     () => get<PortfolioData>('/api/portfolio'),
  positions:     () => get<PositionData[]>('/api/positions'),
  signals:       () => get<SignalData[]>('/api/signals'),
  risk:          () => get<RiskData>('/api/risk'),
  system:        () => get<SystemData>('/api/system'),
  smartMoney:    () => get<SmartMoneyCandidate[]>('/api/smart-money'),
  regime:        () => get<RegimeData | null>('/api/regime'),
  alerts:        () => get<AlertData[]>('/api/alerts'),
  health:        () => get<{ status: string }>('/api/health'),
  agentCommentary: () => get<AgentCommentaryData>('/api/agent-commentary'),
  agentStatus:   () => get<AgentStatusData>('/api/agent-status'),
};

export interface RegimeData {
  zone: 'bull' | 'ambiguous' | 'bear';
  posture: 'defensive' | 'aggressive';
  entries_allowed: boolean;
  close: number;
  sma_50: number;
  sma_200: number;
  above_50: boolean;
  above_200: boolean;
  sma200_rising: boolean;
  reason: string;
}

export interface PortfolioData {
  equity: number;
  cash: number;
  buying_power: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  allocation_pct: number;
  leverage: number;
}

export interface PositionData {
  symbol: string;
  quantity: number;
  avg_cost: number;
  market_price: number;
  market_value: number;
  unrealized_pnl: number;
  stop_price: number | null;
}

export interface SignalData {
  timestamp: string;
  symbol: string;
  direction: string;
  strategy: string;
  size_pct: string;
  shares: number;
  entry_price: number;
  stop_loss: number;
  status: string;
  reasoning: string;
  rejection_reason: string;
  modifications: string[];
  is_ladder_in: boolean;
  is_smart_money: boolean;
  smart_money_conviction: number;
  smart_money_sources: string[];
  arbitration_reasoning?: string;
}

export interface RiskData {
  daily_pnl: number;
  weekly_pnl: number;
  peak_drawdown: number;
  status: string;
  is_half_size: boolean;
  is_closed: boolean;
  is_halted: boolean;
  peak_equity: number;
  current_equity: number;
  circuit_breakers: Record<string, string>;
}

export interface SystemData {
  ibkr_connected: boolean;
  ibkr_account: string;
  uptime_seconds: number;
  last_cycle_seconds: number;
  next_cycle_time: string;
  started_at: string;
  broker_mode?: string;
}

export interface AlertData {
  timestamp: string;
  trigger: string;
  severity: string;
  subject: string;
  message: string;
}

export interface SmartMoneyCandidate {
  symbol: string;
  conviction_score: number;
  sources: string[];
  actors: string[];
  total_dollar_volume: number;
  filing_count: number;
  most_recent_filing: string;
}

export interface AgentCommentaryData {
  content: string | null;
  timestamp: string | null;
  cycle_number: number | null;
  status: 'available' | 'unavailable';
}

export interface AgentStatusData {
  catalyst: 'succeeded' | 'failed' | 'disabled';
  arbitration: 'succeeded' | 'failed' | 'disabled';
  commentary: 'succeeded' | 'failed' | 'disabled';
  last_cycle_timestamp: string | null;
}
