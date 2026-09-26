"use client";

import { Button } from "@/components/ui/button";
import { money } from "@/lib/format";
import { canDecline } from "@/lib/status";
import type { Health, PaymentState, PurchaseDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Check, CircleCheck, CircleHelp, ExternalLink, Loader2, OctagonX, Wallet } from "lucide-react";

type Tone = "pass" | "muted" | "fail" | "warn";

/** Where each payment state sits on the happy path. Side exits (denied, expired, ...) are shown as a banner instead. */
const STEP_OF: Partial<Record<PaymentState, number>> = {
  awaiting_approval: 0, approved: 1, revalidating: 1, credential_ready: 2, paying: 2, unknown: 2, paid: 3, completed: 4,
};

function steps(agentVisible: boolean) {
  return ["Awaiting your Link approval", "Rechecking checkout", agentVisible ? "Card released to agent" : "Paying", "Paid", "Completed"];
}

/** Title, one plain sentence, and tone for the current payment state. */
function describe(d: PurchaseDetail, agentVisible: boolean): { title: string; body: string; tone: Tone } {
  const pay = d.payment!;
  const merchant = d.proposal?.merchant.name ?? d.purchase.merchant_name ?? "the merchant";
  const amount = money(pay.amount, pay.currency);
  switch (pay.state) {
    case "awaiting_approval":
      // After an escalation, not every check passed: the user accepted the unverifiable ones.
      if (d.resolution?.action === "approve") return { tone: "pass", title: "APPROVE THE PAYMENT", body: `You accepted the exception. Now approve the ${amount} payment to ${merchant} in Link. Nothing is paid until you do.` };
      return { tone: "pass", title: "MATCH FOUND · APPROVE IT?", body: `Every check passed. Approve the ${amount} payment to ${merchant} in Link. Nothing is paid until you do.` };
    case "approved":
    case "revalidating":
      return { tone: "pass", title: "APPROVED · RECHECKING CHECKOUT", body: "Handshake is reading the checkout again to make sure nothing changed since you approved." };
    case "credential_ready":
      if (agentVisible && pay.credential_released) return { tone: "pass", title: "CARD RELEASED TO AGENT", body: `A single-use card capped at ${amount} was released to your agent to pay ${merchant}.` };
      if (agentVisible) return { tone: "pass", title: "CARD READY", body: "Waiting for your agent to collect the single-use card for this checkout." };
      return { tone: "pass", title: "PAYING", body: `Paying ${amount} to ${merchant}.` };
    case "paying":
      return { tone: "pass", title: "PAYING", body: `Paying ${amount} to ${merchant}.` };
    case "paid":
      return { tone: "pass", title: "PAID · VERIFYING ORDER", body: "Handshake is checking the merchant's order against the receipt." };
    case "completed": {
      const extras = [pay.order_id && `Order ${pay.order_id}`, pay.last4 && `card ending ${pay.last4}`].filter(Boolean).join(" · ");
      return { tone: "pass", title: "PURCHASED", body: `Paid ${money(d.purchase.charged_amount ?? pay.amount, pay.currency)} to ${merchant}.${extras ? ` ${extras}.` : ""}` };
    }
    case "denied":
      return d.resolution?.action === "decline"
        ? { tone: "muted", title: "DECLINED BY YOU", body: "Nothing was charged. The agent can keep looking under the same contract." }
        : { tone: "muted", title: "YOU DECLINED IN LINK", body: "Nothing was charged." };
    case "expired":
      return { tone: "muted", title: "APPROVAL EXPIRED", body: "The Link approval wasn't completed in time. Nothing was paid. The agent can request the purchase again." };
    case "checkout_changed":
      return { tone: "fail", title: "CHECKOUT CHANGED", body: "The checkout changed after approval; nothing was paid." };
    case "failed":
      return { tone: "fail", title: "PAYMENT FAILED", body: pay.last_error ?? "Nothing was paid." };
    case "unknown":
      return { tone: "warn", title: "CHECKING WITH THE MERCHANT", body: "The payment's outcome isn't known yet. Handshake doesn't retry blindly; it's checking the merchant's order." };
  }
}

const TONES: Record<Tone, { box: string; text: string; Icon: typeof Check }> = {
  pass: { box: "border-pass bg-pass-soft", text: "text-pass", Icon: CircleCheck },
  muted: { box: "border-border bg-muted/50", text: "text-muted-foreground", Icon: CircleCheck },
  fail: { box: "border-fail bg-fail-soft", text: "text-fail", Icon: OctagonX },
  warn: { box: "border-warn bg-warn-soft", text: "text-warn", Icon: CircleHelp },
};

/** Payment status for an authorized purchase: plain-words progression plus the user's approval buttons. */
export function PaymentPanel({ detail: d, health, cap, busy, onApprove, onDecline }: {
  detail: PurchaseDetail;
  health: Health | null;
  cap: number;
  busy: boolean;
  onApprove: () => void;
  onDecline: () => void;
}) {
  if (!d.payment) {
    return (
      <div className="flex items-center gap-3 rounded-2xl border-2 border-pass bg-pass-soft p-5 text-sm">
        <Loader2 className="size-5 animate-spin text-pass" />Every check passed. Requesting your payment approval…
      </div>
    );
  }
  const pay = d.payment;
  const agentVisible = health?.credential_mode === "agent_visible";
  const { title, body, tone } = describe(d, agentVisible);
  const { box, text, Icon } = TONES[tone];
  const at = STEP_OF[pay.state];
  const awaiting = d.status === "authorized" && pay.state === "awaiting_approval";
  const stub = health?.payment_mode === "stub";
  const link = health?.payment_mode === "link_test";

  return (
    <div className={cn("space-y-4 rounded-2xl border-2 p-5 animate-in fade-in zoom-in-95 duration-300", box)}>
      <div className="flex flex-wrap items-center gap-4">
        <Icon className={cn("size-10", text)} strokeWidth={2.5} />
        <div className="min-w-0 flex-1">
          <p className={cn("text-2xl font-black tracking-tight", text)}>{title}</p>
          <p className="text-sm">{body}</p>
        </div>
        <div className="text-right">
          <p className="text-sm text-muted-foreground">{money(pay.amount, pay.currency)} of {money(cap, pay.currency)}</p>
          <div className="mt-1 h-2 w-40 overflow-hidden rounded-full bg-white"><div className="h-full rounded-full bg-pass" style={{ width: `${Math.min(100, (pay.amount / cap) * 100)}%` }} /></div>
        </div>
      </div>

      {/* The happy path in plain words. Side exits (declined, expired, changed, failed) grey it all out; the title says why. */}
      <ol className="flex flex-wrap items-center gap-x-2 gap-y-2 text-xs" aria-label="Payment progress">
        {steps(agentVisible).map((label, i) => {
          const done = at != null && (i < at || pay.state === "completed");
          const current = at === i && pay.state !== "completed";
          return (
            <li key={label} className="flex items-center gap-2">
              <span className={cn("inline-flex h-6 items-center gap-1 rounded-full px-2.5",
                done ? "bg-pass text-white" : current ? "bg-foreground text-background" : "bg-white/70 text-muted-foreground")}>
                {done ? <Check className="size-3" strokeWidth={3} /> : current ? <Loader2 className="size-3 animate-spin" /> : null}
                {current && pay.state === "unknown" ? "Checking with the merchant" : label}
              </span>
              {i < 4 && <span className={cn("h-px w-3", done ? "bg-pass" : "bg-border")} />}
            </li>
          );
        })}
      </ol>

      {awaiting && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {link && (
              <Button size="lg" onClick={onApprove} disabled={busy} className="flex-1 bg-brand text-brand-foreground hover:bg-brand/90 sm:flex-none">
                <ExternalLink />Approve in Link
              </Button>
            )}
            {stub && (
              <Button size="lg" onClick={onApprove} disabled={busy} className="flex-1 bg-brand text-brand-foreground hover:bg-brand/90 sm:flex-none">
                <Check />{busy ? "Approving…" : "Simulated provider approval"}
              </Button>
            )}
            {canDecline(d) && <Button size="lg" variant="outline" onClick={onDecline} disabled={busy} className="flex-1 sm:flex-none">Not this one</Button>}
          </div>
          {stub && <p className="text-xs text-muted-foreground">Simulated provider approval stands in for Link&apos;s approval tap. No real money moves.</p>}
          {link && <p className="text-xs text-muted-foreground">Opens Link in a new tab. Come back here; this page updates on its own.</p>}
        </div>
      )}
      {!awaiting && canDecline(d) && (
        <Button size="sm" variant="outline" onClick={onDecline} disabled={busy}>Not this one</Button>
      )}

      <p className="flex items-center gap-1.5 text-xs text-muted-foreground"><Wallet className="size-3.5" />{pay.provider_label}</p>
    </div>
  );
}
