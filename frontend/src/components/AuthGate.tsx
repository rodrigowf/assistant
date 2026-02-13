import { useState, useEffect, type ReactNode } from "react";
import { authStatus, authLogin } from "../api/rest";

interface Props {
  children: ReactNode;
}

export function AuthGate({ children }: Props) {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    authStatus()
      .then((r) => setAuthenticated(r.authenticated))
      .catch(() => setAuthenticated(false));
  }, []);

  if (authenticated === null) {
    return (
      <div className="auth-gate">
        <div className="auth-card">
          <p className="auth-checking">Checking authentication...</p>
        </div>
      </div>
    );
  }

  if (!authenticated) {
    return (
      <div className="auth-gate">
        <div className="auth-card">
          <h2 className="auth-title">Connect to Claude</h2>
          <p className="auth-description">
            Authenticate via your Claude subscription to start chatting.
          </p>
          <button
            className="auth-btn"
            disabled={loading}
            onClick={async () => {
              setLoading(true);
              try {
                const r = await authLogin();
                setAuthenticated(r.authenticated);
              } catch {
                setAuthenticated(false);
              }
              setLoading(false);
            }}
          >
            {loading ? "Authenticating..." : "Sign in with Claude"}
          </button>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
