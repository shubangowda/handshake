"use client";

import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FIELD_LABELS, SELLER_LABELS, describeConstraint, formatDate, money } from "@/lib/format";
import type { DraftRecord } from "@/lib/types";
import { OctagonX, ShieldCheck, Sparkles } from "lucide-react";

const NEW_MERCHANT = { escalate: "Ask me first", deny: "Blocked", allow: "Allowed" } as const;

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8.5rem_minmax(0,1fr)] gap-3 py-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}

/**
 * Review-and-confirm before signing. It restates the terms the agent will be held to; signing
 * charges nothing (each purchase is paid separately after the user approves it in Link).
 * Blocking issues (lint errors from the backend) disable signing until the draft is edited.
 */
export function SignDialog({ draft: d, blockingIssues, open, onOpenChange, onSign }: {
  draft: DraftRecord;
  /** The draft's blocking_issues, or the ones a 409 draft_has_blocking_issues returned. */
  blockingIssues: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSign: () => Promise<void>;
}) {
  const [signing, setSigning] = useState(false);
  const cur = d.spend.currency;
  const hard = d.constraints.filter((k) => k.severity !== "soft");
  const blocked = blockingIssues.length > 0;

  async function sign() {
    setSigning(true);
    try { await onSign(); } finally { setSigning(false); }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><ShieldCheck className="size-5" />Review and sign</DialogTitle>
          <DialogDescription>
            Your agent may buy <b>{d.goal}</b> under exactly these terms{d.single_use ? ", once" : ""}. Anything outside them is blocked.
            Signing charges nothing: each purchase still needs your approval in Link.
          </DialogDescription>
        </DialogHeader>

        <dl className="divide-y rounded-lg border px-3 text-sm">
          <Row label="Hard cap (all-in)">{money(d.spend.hard_cap_all_in, cur)} <span className="font-normal text-muted-foreground">item, tax, shipping and fees</span></Row>
          <Row label="Delivery deadline">{d.delivery?.deliver_by ? `By ${formatDate(d.delivery.deliver_by)}` : "No deadline"}</Row>
          <Row label="Merchants">{d.merchants.allow.length ? `Only ${d.merchants.allow.join(", ")}` : "Any merchant"}</Row>
          {d.merchants.deny.length > 0 && <Row label="Never buy from">{d.merchants.deny.join(", ")}</Row>}
          <Row label="Seller">{SELLER_LABELS[d.merchants.seller_requirement]}</Row>
          <Row label="New merchants">{NEW_MERCHANT[d.merchants.new_merchant]}</Row>
          {hard.map((k, i) => <Row key={`${k.field}${i}`} label={FIELD_LABELS[k.field]}>{describeConstraint(k)}</Row>)}
          <Row label="Extras">
            {[d.terms.no_subscription && "No subscriptions", d.terms.no_membership && "no memberships", d.terms.no_addons && "no add-ons"].filter(Boolean).join(", ") || "Allowed"}
          </Row>
          <Row label="Valid until">{d.expires_at ? formatDate(d.expires_at) : "No expiry"}</Row>
        </dl>

        {!blocked && d.constraints.some((k) => k.source !== "user") && (
          <p className="flex items-start gap-2 rounded-lg bg-warn-soft p-3 text-sm text-warn"><Sparkles className="mt-0.5 size-4 shrink-0" />This includes values Handshake inferred. They&apos;re highlighted in the contract.</p>
        )}
        {blocked && (
          <div className="rounded-lg border border-fail/30 bg-fail-soft p-3 text-sm text-fail">
            <p className="flex items-center gap-2 font-semibold"><OctagonX className="size-4" />Fix these before signing</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5">{blockingIssues.map((b) => <li key={b}>{b}</li>)}</ul>
            <p className="mt-1 text-xs">Use Edit contract to change the terms.</p>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={sign} disabled={signing || blocked} className="bg-brand text-brand-foreground hover:bg-brand/90">
            <ShieldCheck />{signing ? "Signing…" : "Sign contract"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
