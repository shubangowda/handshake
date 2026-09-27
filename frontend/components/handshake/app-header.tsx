"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CONFIG_ERROR, getHealth, USE_MOCKS } from "@/lib/api";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { CircleCheck, Handshake, Link2 } from "lucide-react";
import { LinkPanel, useLinkStatus } from "./link-connect";
import { UserMenu, useSession } from "./user-menu";

const BADGE = "rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase";

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

/** Link test mode, signed in: the user's own Stripe Link connection, one click from any page. */
function LinkControl() {
  const session = useSession();
  // /link/status needs a user token; without a session there's nothing to show.
  return session && !USE_MOCKS && !CONFIG_ERROR ? <LinkControlInner /> : null;
}

function LinkControlInner() {
  const { health, status } = useLinkStatus();
  const [open, setOpen] = useState(false);
  if (health?.payment_mode !== "link_test" || !status) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}
        className={`${BADGE} inline-flex items-center gap-1 ${status.connected ? "bg-pass-soft text-pass" : "bg-brand text-brand-foreground"}`}>
        {status.connected ? <><CircleCheck className="size-3" />Stripe Link connected</> : <><Link2 className="size-3" />Connect Stripe Link</>}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stripe Link</DialogTitle>
            <DialogDescription>Your own Link account (test mode). Signing a contract asks it for one single-use card for the contract&apos;s all-in cap.</DialogDescription>
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
      <div className="mx-auto flex h-14 max-w-5xl items-center justify-between gap-4 px-4">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <span className="grid size-7 place-items-center rounded-md bg-brand text-brand-foreground"><Handshake className="size-4" /></span>
          Handshake
        </Link>
        <nav className="flex items-center gap-4 text-sm">
          <Link href="/contracts" className="text-muted-foreground hover:text-foreground">Contracts</Link>
          <ModeBadge />
          <LinkControl />
          <UserMenu />
        </nav>
      </div>
      {CONFIG_ERROR && <p className="border-t bg-fail-soft px-4 py-2 text-center text-xs text-fail">{CONFIG_ERROR}</p>}
    </header>
  );
}
