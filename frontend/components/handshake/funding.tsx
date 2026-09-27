"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError, approveFunding, fundingUsesLink, getFunding, restartFunding } from "@/lib/api";
import { formatDate, formatTime, money, shortId } from "@/lib/format";
import { FUND_AGAIN_STATES, fundingStateLabel } from "@/lib/status";
import type { Funding } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Check, CreditCard, ExternalLink, Loader2, Lock } from "lucide-react";
import { LinkPanel } from "./link-connect";

const POLL_MS = 2500;

const validUntil = (iso: string | null | undefined) => (iso ? `${formatDate(iso, { month: "short", day: "numeric" })}, ${formatTime(iso)}` : null);

/** The card's story in one plain sentence. */
function sentence(f: Funding, active: boolean): string {
  // Suggest funding again only where the backend allows it (active contracts).
  const again = active ? " Fund again to buy under this contract again." : "";
  const card = f.card_last4 ? `Card ending ${f.card_last4}` : "The card";
  const simulated = !fundingUsesLink(f);
  switch (f.state) {
    case "not_funded": return "Not funded yet. Your agent can't buy anything until this contract has a card.";
    case "awaiting_approval": return simulated
      ? `Signing asked the simulated provider (standing in for Stripe Link) for a single-use test card for up to ${money(f.amount, f.currency)}. Approve it to fund the contract.`
      : `Signing asked your Link account for a single-use test card for up to ${money(f.amount, f.currency)}. Approve it to fund the contract.`;
    case "funded": return `${card} stored, encrypted, locked until Handshake approves a checkout.`;
    case "released": return `${card} was released once to your agent for an approved checkout, and the stored copy was wiped.`;
    case "used": return `${card} was used for a purchase.${again}`;
    case "denied": return `${simulated ? "The simulated card was declined" : "You declined the card in Link"}. Nothing was stored.${again}`;
    case "expired": return `The funded card expired unused (Link test cards are valid for 12 hours).${again}`;
    case "failed": return f.last_error ?? "Link couldn't provide a card.";
    case "canceled": return "Funding was canceled when the contract was revoked. The stored card was wiped.";
  }
}

/**
 * The contract's funding (signing = funding). Shows where the card is, lets the user approve it
 * ("Approve in Link", or "Simulated provider approval" in stub mode) and fund again after a
 * denied, expired, failed, or used card. Polls while the card waits for approval.
 */
export function FundingCard({ contractId, active, initial, onChange }: {
  contractId: string;
  /** Only an active contract can be funded again. */
  active: boolean;
  initial: Funding;
  onChange?: (f: Funding) => void;
}) {
  const [funding, setFunding] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [needsLink, setNeedsLink] = useState(false);
  const awaiting = funding.state === "awaiting_approval";

  useEffect(() => {
    if (!awaiting) return;
    const timer = setInterval(async () => {
      try {
        const f = await getFunding(contractId);
        setFunding(f);
        if (f.state !== "awaiting_approval") onChange?.(f);
      } catch { /* the next poll retries */ }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [awaiting, contractId, onChange]);

  async function approve() {
    setBusy(true); setError(null);
    try {
      // Called straight from the click so Link's tab isn't treated as a pop-up.
      const f = await approveFunding(contractId, funding);
      setFunding(f);
      if (f.state !== "awaiting_approval") onChange?.(f);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function fundAgain() {
    setBusy(true); setError(null); setNeedsLink(false);
    try { setFunding(await restartFunding(contractId)); }
    catch (e) {
      if (e instanceof ApiError && e.code === "link_not_connected") setNeedsLink(true);
      setError((e as Error).message);
    } finally { setBusy(false); }
  }

  const tone = funding.state === "funded" || funding.state === "released" ? "border-pass/40 bg-pass-soft"
    : awaiting ? "border-brand/40 bg-card ring-1 ring-brand/20"
    : funding.state === "denied" || funding.state === "failed" || funding.state === "expired" ? "border-fail/30 bg-fail-soft/60" : "bg-card";
  // This card's own provider decides: in link_optional mode a user can hold both simulated and Link cards.
  const stub = !fundingUsesLink(funding);

  return (
    <div className={cn("space-y-3 rounded-xl border p-4 text-sm sm:p-5", tone)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-semibold">{funding.state === "funded" ? <Lock className="size-4 text-pass" /> : <CreditCard className="size-4" />}Funding</h2>
        <span className="font-mono text-[11px] font-semibold tracking-wider text-muted-foreground uppercase">{fundingStateLabel(funding)}</span>
      </div>
      <p>{sentence(funding, active)}</p>
      {funding.state === "funded" && validUntil(funding.valid_until) && <p className="text-muted-foreground">Valid until {validUntil(funding.valid_until)}</p>}
      {funding.released_purchase_id && (funding.state === "released" || funding.state === "used") && (
        <p><Link className="underline underline-offset-4" href={`/purchases/${funding.released_purchase_id}`}>Purchase #{shortId(funding.released_purchase_id)}</Link></p>
      )}

      {awaiting && active && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {stub
              ? <Button onClick={approve} disabled={busy} className="bg-brand text-brand-foreground hover:bg-brand/90"><Check />{busy ? "Approving…" : "Simulated provider approval"}</Button>
              : <Button onClick={approve} disabled={busy} className="bg-brand text-brand-foreground hover:bg-brand/90"><ExternalLink />Approve in Link</Button>}
          </div>
          <p className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            {stub ? "Simulated provider approval stands in for Link's approval tap. No real money moves." : "Opens Link in a new tab. This card updates on its own once you approve."}
          </p>
        </div>
      )}
      {active && FUND_AGAIN_STATES.includes(funding.state) && (
        <Button variant="outline" onClick={fundAgain} disabled={busy}>{busy ? "Requesting…" : funding.state === "not_funded" ? "Fund contract" : "Fund again"}</Button>
      )}
      {error && <p className="text-fail">{error}</p>}
      {needsLink && <LinkPanel onConnected={() => { setNeedsLink(false); setError(null); }} />}
      {funding.provider_label && <p className="text-xs text-muted-foreground">{funding.provider_label}</p>}
    </div>
  );
}

/** One line for purchase pages: which card the contract holds and where it is. */
export function FundingSummary({ funding: f, contractId }: { funding: Funding; contractId: string }) {
  return (
    <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border bg-card px-4 py-2.5 text-sm">
      <CreditCard className="size-4 text-muted-foreground" />
      <span className="text-muted-foreground">Contract funding:</span>
      <span className="font-medium">{f.card_last4 ? `card ending ${f.card_last4}` : "no card"} · {fundingStateLabel(f)}</span>
      <Link href={`/contracts/${contractId}`} className="ml-auto text-xs underline underline-offset-4">View contract</Link>
    </p>
  );
}
