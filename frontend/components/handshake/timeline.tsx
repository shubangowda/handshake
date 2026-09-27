import { cn } from "@/lib/utils";
import type { EvidenceEvent, EvidenceEventType, EvidenceKind } from "@/lib/types";
import { formatTime } from "@/lib/format";

const TONE: Partial<Record<EvidenceEventType, string>> = {
  purchase_blocked: "bg-fail",
  payment_mismatch: "bg-fail",
  contract_revoked: "bg-fail",
  purchase_escalated: "bg-warn",
  payment_completed: "bg-pass",
  purchase_authorized: "bg-pass",
  credential_created: "bg-pass",
  credential_used: "bg-pass",
};

/** data.kind is the precise step for events models.py has no event_type for; it wins over the generic type. */
const KIND: Partial<Record<EvidenceKind, { label: string; tone?: string }>> = {
  // Funding (signing = funding)
  funding_requested: { label: "Funding requested" },
  simulated_provider_approval: { label: "Simulated provider approval", tone: "bg-pass" },
  card_stored: { label: "Card stored (encrypted)", tone: "bg-pass" },
  funding_denied: { label: "Funding declined in Link", tone: "bg-fail" },
  funding_expired: { label: "Funded card expired", tone: "bg-fail" },
  funding_failed: { label: "Funding failed", tone: "bg-fail" },
  funding_unavailable: { label: "Contract not funded", tone: "bg-fail" },
  card_unavailable: { label: "Stored card unavailable", tone: "bg-fail" },
  credential_retrieval_failed: { label: "Couldn't retrieve the card", tone: "bg-fail" },
  // Purchase payment
  credential_ready: { label: "Card unlocked for this checkout", tone: "bg-pass" },
  checkout_revalidated: { label: "Checkout rechecked", tone: "bg-pass" },
  checkout_changed: { label: "Checkout changed; card stayed locked", tone: "bg-fail" },
  credential_released: { label: "Card released to agent", tone: "bg-pass" },
  authorization_expired: { label: "Authorization expired", tone: "bg-fail" },
  payment_submitted: { label: "Payment submitted", tone: "bg-pass" },
  payment_outcome_unknown: { label: "Checking with the merchant", tone: "bg-warn" },
  payment_request_uncertain: { label: "Payment request unconfirmed", tone: "bg-warn" },
  payment_request_failed: { label: "Payment request failed", tone: "bg-fail" },
  payment_not_made: { label: "No payment made", tone: "bg-fail" },
  payment_refused: { label: "Payment refused", tone: "bg-fail" },
  order_not_verified: { label: "Order not verified", tone: "bg-warn" },
  order_mismatch: { label: "Order didn't match", tone: "bg-fail" },
  receipt_verified: { label: "Receipt verified", tone: "bg-pass" },
  receipt_mismatch: { label: "Receipt didn't match", tone: "bg-fail" },
  // Contract and user
  contract_tampered: { label: "Contract tampered", tone: "bg-fail" },
  contract_no_longer_valid: { label: "Contract no longer valid", tone: "bg-fail" },
  purchase_declined: { label: "Declined by you" },
  contract_amended: { label: "New version started" },
  draft_edited: { label: "Draft edited" },
  agent_action_denied: { label: "Agent tried a user-only action (refused)", tone: "bg-fail" },
  extraction_failed: { label: "Couldn't read the checkout", tone: "bg-fail" },
};

function describe(e: EvidenceEvent): { label: string | null; tone: string } {
  const kind = e.data?.kind ? KIND[e.data.kind as EvidenceKind] : undefined;
  if (kind) return { label: kind.label, tone: kind.tone ?? TONE[e.event_type] ?? "bg-muted-foreground/50" };
  if (e.data?.human_approval) return { label: "You accepted the exception", tone: "bg-warn" };
  if (e.data?.human_rejection) return { label: "You rejected it", tone: "bg-fail" };
  return { label: null, tone: TONE[e.event_type] ?? "bg-muted-foreground/50" };
}

export function Timeline({ events }: { events: EvidenceEvent[] }) {
  if (!events.length) return <p className="text-sm text-muted-foreground">No activity yet.</p>;
  return (
    <ol className="relative space-y-3 before:absolute before:top-2 before:bottom-2 before:left-[4.75rem] before:w-px before:bg-border">
      {events.map((e) => {
        const { label, tone } = describe(e);
        return (
          <li key={e.id} className="grid grid-cols-[4rem_1.5rem_minmax(0,1fr)] items-start text-sm">
            <time className="pt-px text-right font-mono text-xs text-muted-foreground tabular-nums">{formatTime(e.timestamp)}</time>
            <span className="flex justify-center pt-1.5"><span className={cn("relative size-2.5 rounded-full ring-4 ring-card", tone)} /></span>
            <span className={cn(tone === "bg-fail" && "text-fail")}>
              {label && <span className="block font-semibold">{label}</span>}
              <span className={cn(label && "text-muted-foreground", !label && tone === "bg-fail" && "font-semibold")}>{e.message}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
