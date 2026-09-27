"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CONFIG_ERROR, getHealth, USE_MOCKS } from "@/lib/api";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { CircleCheck, Handshake, Link2 } from "lucide-react";
import { LinkPanel, useLinkStatus } from "./link-connect";
import { UserMenu, useSession } from "./user-menu";

const BADGE = "rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold whitespace-nowrap uppercase";

/** Says which payment rail is live, so nobody mistakes the simulated provider (or mocks) for real money. */
function ModeBadge() {
  const [label, setLabel] = useState<string | null>(null);
  useEffect(() => {
    if (USE_MOCKS || CONFIG_ERROR) return;
    getHealth().then((h) => setLabel(h.payment_label), () => setLabel("Backend unreachable"));
  }, []);
  if (USE_MOCKS) return <span className={`${BADGE} bg-warn-soft text-warn`} title="Using mock data. Set NEXT_PUBLIC_USE_MOCKS=false to use the backend.">Mock data</span>;
  if (CONFIG_ERROR) return <span className={`${BADGE} bg-fail-soft text-fail`} title={CONFIG_ERROR}>API not configured</span>;
  if (!label) return null;
  return <span className={`${BADGE} bg-warn-soft text-warn`} title="Payment provider the backend is using">{label}</span>;
}

/**
 * Link test (or optional) mode, signed in: the user's own Stripe Link connection, one click from any page.
 * Mock mode shows it too when the mock knobs pick a Link mode (the mock status is local).
 */
function LinkControl() {
  const session = useSession();
  // /link/status needs a user token; without a session there's nothing to show.
  return session && !CONFIG_ERROR ? <LinkControlInner /> : null;
}

function LinkControlInner() {
  const { health, status } = useLinkStatus();
  const [open, setOpen] = useState(false);
  if (!health || health.payment_mode === "stub" || !status) return null;
  // Optional Link is offered quietly; required Link (link_test) is a call to action until connected.
  const optional = health.payment_mode === "link_optional";
  const tone = status.connected ? "bg-pass-soft text-pass" : optional ? "border text-muted-foreground hover:text-foreground" : "bg-brand text-brand-foreground";
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className={`${BADGE} inline-flex items-center gap-1 ${tone}`}
        title={optional && !status.connected ? "Optional: fund contracts with your own Stripe Link test-mode cards" : undefined}>
        {status.connected ? <><CircleCheck className="size-3" />Stripe Link connected</> : <><Link2 className="size-3" />{optional ? "Link (optional)" : "Connect Stripe Link"}</>}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stripe Link</DialogTitle>
            <DialogDescription>
              Your own Link account (test mode). {optional
                ? "Once connected, signing a contract asks it for one single-use card for the contract's all-in cap; without it, funding is simulated."
                : "Signing a contract asks it for one single-use card for the contract's all-in cap."}
            </DialogDescription>
          </DialogHeader>
          <LinkPanel />
        </DialogContent>
      </Dialog>
    </>
  );
}

export function AppHeader() {
  return (
    <header className="sticky top-0 z-20 border-b bg-background/85 backdrop-blur">
      {/* On a phone the nav wraps to a second row instead of pushing the page sideways. */}
      <div className="mx-auto flex min-h-14 max-w-5xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-2">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <span className="grid size-7 place-items-center rounded-md bg-brand text-brand-foreground"><Handshake className="size-4" /></span>
          Handshake
        </Link>
        <nav className="flex flex-wrap items-center justify-end gap-x-4 gap-y-2 text-sm">
          <Link href="/contracts" className="text-muted-foreground hover:text-foreground">Contracts</Link>
          <Link href="/agents" className="text-muted-foreground hover:text-foreground">Agents</Link>
          <ModeBadge />
          <LinkControl />
          <UserMenu />
        </nav>
      </div>
      {CONFIG_ERROR && <p className="border-t bg-fail-soft px-4 py-2 text-center text-xs text-fail">{CONFIG_ERROR}</p>}
    </header>
  );
}
