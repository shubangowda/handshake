"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { connectLink, disconnectLink, getHealth, getLinkStatus, openLinkLogin } from "@/lib/api";
import type { Health, LinkLogin, LinkStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CircleCheck, ExternalLink, Link2, Loader2 } from "lucide-react";

const POLL_MS = 2000;

// The header, the dashboard banner and the sign dialog each show Link status; this keeps them in sync
// after a connect or disconnect without a shared store.
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((l) => l());

/** Link connection status for the signed-in user, plus a way to re-check it. */
export function useLinkStatus() {
  const [health, setHealth] = useState<Health | null>(null);
  const [status, setStatus] = useState<LinkStatus | null>(null);
  const refresh = useCallback(() => {
    getHealth().then(setHealth, () => {});
    getLinkStatus().then(setStatus, () => {});
  }, []);
  useEffect(() => {
    refresh();
    listeners.add(refresh);
    return () => { listeners.delete(refresh); };
  }, [refresh]);
  return { health, status, refresh };
}

/**
 * Connect the user's OWN Stripe Link account (link_test mode). POST /link/connect returns Link's
 * login link and a phrase at once; the user approves in the Link app while we poll /link/status.
 * In stub mode nothing is needed, so it just says so.
 */
export function LinkPanel({ className, onConnected }: { className?: string; onConnected?: () => void }) {
  const { health, status, refresh } = useLinkStatus();
  const [login, setLogin] = useState<LinkLogin | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A login started earlier (e.g. before a reload) comes back on /link/status.
  const pending = login ?? status?.login ?? null;
  // "starting" means Link hadn't sent its login link yet when /link/connect answered; keep polling for it.
  const waiting = !status?.connected && (pending?.state === "pending" || pending?.state === "starting");

  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(async () => {
      try {
        const s = await getLinkStatus();
        if (s.connected) { setLogin(null); notify(); onConnected?.(); }
        else if (s.login) setLogin(s.login);
      } catch { /* the next poll retries */ }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, onConnected]);

  async function connect() {
    setBusy(true); setError(null);
    try {
      const l = await connectLink();
      if (l.state === "connected") { notify(); onConnected?.(); }
      else if (l.state === "failed" || l.state === "expired") setError(l.error ?? "Link didn't start a login. Try again.");
      setLogin(l);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function disconnect() {
    setBusy(true); setError(null);
    try { await disconnectLink(); setLogin(null); notify(); refresh(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  function open() {
    try { if (pending) openLinkLogin(pending); } catch (e) { setError((e as Error).message); }
  }

  const box = cn("rounded-xl border p-4 text-sm", className);
  if (!health || !status) return <div className={cn(box, "h-20 animate-pulse bg-muted")} />;

  if (health.payment_mode === "stub" || status.simulated) {
    return <div className={cn(box, "bg-card")}><p className="flex items-center gap-2 font-medium"><CircleCheck className="size-4 text-pass" />Simulated provider (no Link account needed)</p></div>;
  }

  if (status.connected) {
    return (
      <div className={cn(box, "flex flex-wrap items-center justify-between gap-3 bg-card")}>
        <p className="flex items-center gap-2 font-medium"><CircleCheck className="size-4 text-pass" />Stripe Link connected <span className="font-normal text-muted-foreground">(test mode)</span></p>
        <Button variant="outline" size="sm" onClick={disconnect} disabled={busy}>Disconnect</Button>
        {error && <p className="w-full text-fail">{error}</p>}
      </div>
    );
  }

  return (
    <div className={cn(box, "space-y-3 border-warn/40 bg-warn-soft")}>
      <div>
        <p className="flex items-center gap-2 font-semibold"><Link2 className="size-4" />Connect Stripe Link</p>
        <p className="mt-1 text-foreground/80">Signing a contract asks your own Link account (test mode) for a single-use card. Connect it once first.</p>
      </div>
      {waiting && pending ? (
        <div className="space-y-2">
          {pending.verification_url
            ? <Button size="sm" onClick={open} className="bg-brand text-brand-foreground hover:bg-brand/90"><ExternalLink />Open Link to approve</Button>
            : <p className="text-muted-foreground">Waiting for Link to send its login link…</p>}
          {pending.phrase && <p>Check that Link shows this phrase: <b className="font-mono">{pending.phrase}</b></p>}
          <p className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3.5 animate-spin" />Waiting for you to approve in the Link app…</p>
        </div>
      ) : (
        <Button size="sm" onClick={connect} disabled={busy} className="bg-brand text-brand-foreground hover:bg-brand/90">
          {busy ? <><Loader2 className="animate-spin" />Starting Link login…</> : "Connect Stripe Link"}
        </Button>
      )}
      {error && <p className="text-fail">{error}</p>}
    </div>
  );
}
