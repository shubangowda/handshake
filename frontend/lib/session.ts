// PLACEHOLDER session until real auth exists. Stored in this browser only; never holds a password.
export interface Session {
  email: string;
  interests: string[];
  budget: string | null;
}

const KEY = "handshake.session";

export function readSession(): Session | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
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
