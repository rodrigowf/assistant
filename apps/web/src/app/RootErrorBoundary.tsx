/**
 * Last-resort error boundary around the whole app. Without it a render error unmounts the tree
 * and the screen goes blank with no trace. Here the error and React's component stack go to the
 * remote console (always sent, like window errors: devices without devtools depend on it) and
 * the user gets a Reload button.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { remoteLog } from '@/platform';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class RootErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    const stack = error.stack ? `\n${error.stack}` : '';
    remoteLog('react', `[render] ${error.name}: ${error.message} (${window.location.hash})${stack}\ncomponent stack:${info.componentStack ?? ''}`);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        role="alert"
        style={{
          padding: 24,
          minHeight: '100vh',
          boxSizing: 'border-box',
          background: 'var(--md-sys-color-surface, #111)',
          color: 'var(--md-sys-color-on-surface, #eee)',
          fontFamily: 'inherit',
        }}
      >
        <h1 style={{ fontSize: 22, margin: '0 0 12px' }}>Something broke</h1>
        <p style={{ margin: '0 0 8px', opacity: 0.8 }}>The error was sent to the server log.</p>
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 13, opacity: 0.7, margin: '0 0 20px' }}>
          {error.name}: {error.message}
        </pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            font: 'inherit',
            padding: '10px 20px',
            borderRadius: 20,
            border: 0,
            background: 'var(--md-sys-color-primary, #9ab)',
            color: 'var(--md-sys-color-on-primary, #000)',
          }}
        >
          Reload
        </button>
      </div>
    );
  }
}
