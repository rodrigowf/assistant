import { useState, useEffect, type ReactNode } from "react";
import { authStatus, authLogin, authSetCredentials, type AuthStatusResponse } from "../api/rest";

interface Props {
  children: ReactNode;
}

export function AuthGate({ children }: Props) {
  const [authState, setAuthState] = useState<AuthStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [showCredentialsInput, setShowCredentialsInput] = useState(false);
  const [credentialsJson, setCredentialsJson] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    authStatus()
      .then(setAuthState)
      .catch(() => setAuthState({ authenticated: false, headless: false }));
  }, []);

  if (authState === null) {
    return (
      <div className="auth-gate">
        <div className="auth-card">
          <p className="auth-checking">Checking authentication...</p>
        </div>
      </div>
    );
  }

  if (!authState.authenticated) {
    // Headless mode: show link + credentials input
    if (authState.headless || showCredentialsInput) {
      return (
        <div className="auth-gate">
          <div className="auth-card auth-card-wide">
            <h2 className="auth-title">Connect to Claude</h2>
            <p className="auth-description">
              This server is running in headless mode. To authenticate:
            </p>
            <ol className="auth-steps">
              <li>
                On a machine where you're logged into Claude, copy the contents of{" "}
                <code>~/.claude/.credentials.json</code>
              </li>
              <li>Paste the JSON below and click "Set Credentials"</li>
            </ol>

            {authState.auth_url && (
              <p className="auth-alt">
                Or visit{" "}
                <a href={authState.auth_url} target="_blank" rel="noopener noreferrer">
                  Claude Console
                </a>{" "}
                to manage your tokens.
              </p>
            )}

            <textarea
              className="auth-textarea"
              placeholder='{"claudeAiOauth": {"accessToken": "...", ...}}'
              value={credentialsJson}
              onChange={(e) => {
                setCredentialsJson(e.target.value);
                setError(null);
              }}
              rows={6}
            />

            {error && <p className="auth-error">{error}</p>}

            <div className="auth-buttons">
              <button
                className="auth-btn"
                disabled={loading || !credentialsJson.trim()}
                onClick={async () => {
                  setLoading(true);
                  setError(null);
                  try {
                    const r = await authSetCredentials(credentialsJson);
                    if (r.authenticated) {
                      setAuthState(r);
                    } else {
                      setError("Invalid credentials. Please check the JSON format.");
                    }
                  } catch {
                    setError("Failed to set credentials. Please try again.");
                  }
                  setLoading(false);
                }}
              >
                {loading ? "Setting credentials..." : "Set Credentials"}
              </button>

              {!authState.headless && (
                <button
                  className="auth-btn auth-btn-secondary"
                  onClick={() => setShowCredentialsInput(false)}
                >
                  Back
                </button>
              )}
            </div>
          </div>
        </div>
      );
    }

    // Normal mode: show login button with option to switch to headless
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
                setAuthState(r);
              } catch {
                setAuthState({ authenticated: false, headless: false });
              }
              setLoading(false);
            }}
          >
            {loading ? "Authenticating..." : "Sign in with Claude"}
          </button>

          <button
            className="auth-link"
            onClick={() => setShowCredentialsInput(true)}
          >
            Or paste credentials manually
          </button>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
