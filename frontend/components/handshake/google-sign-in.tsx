"use client";

import Script from "next/script";
import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

// Google Identity Services: Google's own script renders the button and hands back a signed ID token.
// Handshake never sees a Google password; the backend verifies the token (POST /auth/google).
const GIS_SRC = "https://accounts.google.com/gsi/client";

interface GoogleCredentialResponse { credential?: string }
interface GoogleAccountsId {
  initialize(options: { client_id: string; callback: (r: GoogleCredentialResponse) => void; ux_mode?: "popup" | "redirect"; auto_select?: boolean; context?: "signin" | "signup" | "use" }): void;
  renderButton(parent: HTMLElement, options: Record<string, unknown>): void;
}
declare global {
  interface Window { google?: { accounts?: { id?: GoogleAccountsId } } }
}

/**
 * The "Sign in with Google" button. `onCredential` gets the ID token; errors (script blocked, no
 * credential) go to `onError` so the page can show them next to the other sign-in options.
 */
export function GoogleSignIn({ clientId, onCredential, onError, disabled }: {
  clientId: string;
  onCredential: (credential: string) => void;
  onError: (message: string) => void;
  disabled?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  // GIS keeps the callback it was initialized with; route it through a ref so it always sees the latest props.
  const handlers = useRef({ onCredential, onError });
  useEffect(() => { handlers.current = { onCredential, onError }; }, [onCredential, onError]);

  const render = useCallback(() => {
    const gis = window.google?.accounts?.id;
    if (!gis || !box.current) return;
    gis.initialize({
      client_id: clientId,
      ux_mode: "popup",
      callback: (r) => (r.credential ? handlers.current.onCredential(r.credential) : handlers.current.onError("Google didn't return a sign-in. Try again.")),
    });
    // GIS wants a pixel width (200 to 400); match the form's width.
    const width = Math.round(Math.min(400, Math.max(200, box.current.offsetWidth)));
    box.current.replaceChildren();
    gis.renderButton(box.current, { type: "standard", theme: "outline", size: "large", text: "signin_with", shape: "rectangular", logo_alignment: "center", width });
    setReady(true);
  }, [clientId]);

  return (
    <div className="grid gap-2">
      {/* onReady also runs when this component remounts after a client-side navigation (script already loaded). */}
      <Script src={GIS_SRC} strategy="afterInteractive" onReady={render}
        onError={() => { setFailed(true); onError("Couldn't load Google sign-in. Check your connection or ad blocker, then reload."); }} />
      <div className={disabled ? "pointer-events-none opacity-60" : undefined} aria-busy={!ready}>
        {/* Google's script owns this div's children; React never renders into it. */}
        <div ref={box} className={cn("flex min-h-10 w-full justify-center", !ready && !failed && "animate-pulse rounded-md bg-muted")} />
      </div>
    </div>
  );
}
