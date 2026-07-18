import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  name?: string;
}
interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="panel border-negative/20 bg-negative/5">
          <div className="flex items-center gap-2 text-negative text-xs">
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="15" y1="9" x2="9" y2="15" />
              <line x1="9" y1="9" x2="15" y2="15" />
            </svg>
            <span className="font-medium">
              {this.props.name ?? 'Component'} error
            </span>
          </div>
          <code className="text-[10px] text-negative/60 mt-2 block font-mono">
            {this.state.error.message}
          </code>
        </div>
      );
    }
    return this.props.children;
  }
}
