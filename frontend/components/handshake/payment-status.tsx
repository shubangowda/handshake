"use client";

import { Button } from "@/components/ui/button";
import { money } from "@/lib/format";
import { canDecline } from "@/lib/status";
import type { Health, PaymentState, PurchaseDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Check, CircleCheck, CircleHelp, Loader2, OctagonX, Wallet } from "lucide-react";

type Tone = "pass" | "muted" | "fail" | "warn";

/**
 * Where each payment state sits on the happy path. Funding happened at signing, so an authorized
 * checkout starts with the contract's card unlocked. Side exits are shown as a banner instead.
 */
const STEP_OF: Partial<Record<PaymentState, number>> = { credential_ready: 0, paying: 1, unknown: 1, paid: 2, completed: 3 };

function steps(agentVisible: boolean) {
  return ["Card unlocked", agentVisible ? "Card released to agent" : "Paying", "Paid", "Completed"];
}

/** Title, one plain sentence, and tone for the current payment state. */
function describe(d: PurchaseDetail, agentVisible: boolean): { title: string; body: string; tone: Tone } {
  const pay = d.payment!;
  const merchant = d.proposal?.merchant.name ?? d.purchase.merchant_name ?? "the merchant";
  const amount = money(pay.amount, pay.currency);
  const card = d.funding.card_last4 ? `card ending ${d.funding.card_last4}` : "card";
  switch (pay.state) {
    case "credential_ready": {
      const why = d.resolution?.action === "approve" ? "You accepted the exception, so" : "Every check passed, so";
      return {
        tone: "pass", title: "CARD UNLOCKED FOR THIS CHECKOUT",
        body: agentVisible
          ? `${why} the contract's funded ${card} is unlocked for this checkout only. Your agent collects it once to pay ${amount} to ${merchant}.`
          : `${why} Handshake is paying ${amount} to ${merchant} with the contract's funded ${card}.`,
      };
    }
    case "paying":
      return agentVisible
        ? { tone: "pass", title: "CARD RELEASED TO AGENT", body: `The ${card} was released once to your agent to pay ${amount} to ${merchant}. Handshake's stored copy was wiped.` }
        : { tone: "pass", title: "PAYING", body: `Paying ${amount} to ${merchant}.` };
    case "paid":
      return { tone: "pass", title: "PAID · VERIFYING ORDER", body: "Handshake is checking the merchant's order against the receipt." };
    case "completed": {
      const extras = [pay.order_id && `Order ${pay.order_id}`, pay.last4 && `card ending ${pay.last4}`].filter(Boolean).join(" · ");
      return { tone: "pass", title: "PURCHASED", body: `Paid ${money(d.purchase.charged_amount ?? pay.amount, pay.currency)} to ${merchant}.${extras ? ` ${extras}.` : ""}` };
    }
    case "denied":
      return d.resolution?.action === "decline"
        ? { tone: "muted", title: "DECLINED BY YOU", body: "Nothing was charged. The card stays locked on the contract, and the agent can keep looking." }
        : { tone: "muted", title: "PAYMENT DECLINED", body: "Nothing was charged." };
    case "expired":
      return { tone: "muted", title: "AUTHORIZATION EXPIRED", body: "The agent didn't pay in time. Nothing was paid. The agent can request the purchase again." };
    case "checkout_changed":
      return { tone: "fail", title: "CHECKOUT CHANGED", body: "Handshake re-read the checkout before releasing the card and it had changed, so the card stayed locked. Nothing was paid." };
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

/** Payment status for an authorized purchase: plain-words progression, plus "Not this one" before the card is released. */
export function PaymentPanel({ detail: d, health, cap, busy, onDecline }: {
  detail: PurchaseDetail;
  health: Health | null;
  cap: number;
  busy: boolean;
  onDecline: () => void;
}) {
  if (!d.payment) {
    return (
      <div className="flex items-center gap-3 rounded-2xl border-2 border-pass bg-pass-soft p-5 text-sm">
        <Loader2 className="size-5 animate-spin text-pass" />Every check passed. Unlocking the contract&apos;s card for this checkout…
      </div>
    );
  }
  const pay = d.payment;
  const agentVisible = health?.credential_mode !== "executor";
  const { title, body, tone } = describe(d, agentVisible);
  const { box, text, Icon } = TONES[tone];
  const at = STEP_OF[pay.state];

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
        {steps(agentVisible).map((label, i, all) => {
          const done = at != null && (i < at || pay.state === "completed");
          const current = at === i && pay.state !== "completed";
          return (
            <li key={label} className="flex items-center gap-2">
              <span className={cn("inline-flex h-6 items-center gap-1 rounded-full px-2.5",
                done ? "bg-pass text-white" : current ? "bg-foreground text-background" : "bg-white/70 text-muted-foreground")}>
                {done ? <Check className="size-3" strokeWidth={3} /> : current ? <Loader2 className="size-3 animate-spin" /> : null}
                {current && pay.state === "unknown" ? "Checking with the merchant" : label}
              </span>
              {i < all.length - 1 && <span className={cn("h-px w-3", done ? "bg-pass" : "bg-border")} />}
            </li>
          );
        })}
      </ol>

      {canDecline(d) && (
        <div className="flex flex-wrap items-center gap-3">
          <Button size="lg" variant="outline" onClick={onDecline} disabled={busy}>Not this one</Button>
          <p className="text-xs text-muted-foreground">Declines this checkout before the card is released. The card stays locked on the contract.</p>
        </div>
      )}

      <p className="flex items-center gap-1.5 text-xs text-muted-foreground"><Wallet className="size-3.5" />{pay.provider_label}</p>
    </div>
  );
}
