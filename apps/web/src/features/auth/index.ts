/**
 * Entry point of `@/features/auth` (W-13, spec 13 §3.6, §3.9).
 *
 *   <AuthGate>{app}</AuthGate>       startup status check; sign-in screen over the app when needed
 *   <SignInScreen inline? />         the gate's screen: Claude link sign-in / paste credentials (lazy chunk)
 *   checkAuth(), useAuth(selector)   status store shared by both
 */
export { AuthGate, type AuthGateProps } from './AuthGate';
export { SignInScreen } from './lazy';
export type { AuthPanelProps } from './AuthPanel';
export { authStore, authSummary, backendHost, checkAuth, dismissGate, GATE_DISMISSED_KEY, resetAuth, useAuth, type AuthPhase, type AuthState } from './authStore';
export type { CredentialsCheck } from './authActions';
