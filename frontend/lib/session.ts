// DEMO session: the token from POST /auth/demo-login (no password), stored in this browser only.
// To be replaced by passkeys/OAuth. Never holds a password or card data.
export interface Session {
  email: string;
  /** Bearer token for the backend. In mock mode it's a placeholder that is never sent anywhere. */
  token: string;
  expires_at: string | null;
  interests: string[];
  budget: string | null;
}

const KEY = "handshake.session";

export function readSession(): Session | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Partial<Session>;
    // Sessions saved before real login existed have no token: treat them as signed out.
    if (!s.token || !s.email) return null;
    if (s.expires_at && new Date(s.expires_at).getTime() <= Date.now()) return null;
    return { interests: [], budget: null, expires_at: null, ...s } as Session;
  } catch {
    return null;
  }
}

export function writeSession(s: Session) {
  try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* storage unavailable: session lasts for this page only */ }
}

export function clearSession() {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** Only same-site paths are allowed after login, so ?next= can't bounce the user to another site. */
export function safeNext(next: string | null | undefined, fallback = "/contracts"): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : fallback;
}
