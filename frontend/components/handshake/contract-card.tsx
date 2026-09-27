import Link from "next/link";
import { cn } from "@/lib/utils";
import type { ContractRecord } from "@/lib/types";
import { isDraft, money, relativeExpiry } from "@/lib/format";
import { fundingOf, fundingStateLabel, rejectionReason, type DisplayStatus } from "@/lib/status";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "./tags";
import { Ban, Check, Clock, CreditCard, GitBranch, Sparkles } from "lucide-react";

function inferredCount(c: ContractRecord) {
  return c.constraints.filter((k) => k.source !== "user").length
    + (c.spend.hard_cap_source !== "user" ? 1 : 0)
    + (c.delivery && c.delivery.max_shipping_source !== "user" ? 1 : 0);
}

export function ContractCard({ contract: c, status, onApproveFunding, approving, approveLabel = "Approve funding in Link" }: {
  contract: ContractRecord;
  /** "pending" = signed, with its funding card waiting for the user's approval. */
  status: DisplayStatus;
  onApproveFunding?: (c: ContractRecord) => void;
  approving?: boolean;
  /** "Approve funding in Link", or "Simulated provider approval" when the backend runs the stub provider. */
  approveLabel?: string;
}) {
  const funding = fundingOf(c);
  const rejected = status === "rejected";
  const draft = isDraft(c);
  const reason = rejectionReason(c);
  const ReasonIcon = c.status === "expired" ? Clock : Ban;
  return (
    // The title link stretches over the whole card; the Approve button sits above it.
    <div className={cn(
      "relative flex flex-col gap-4 rounded-xl border bg-card p-5 shadow-xs transition hover:-translate-y-0.5 hover:shadow-md",
      draft && "border-warn/50 ring-1 ring-warn/20",
      status === "pending" && "border-brand/40 ring-1 ring-brand/20",
      rejected && "bg-muted/40",
    )}>
      {reason && (
        <p className="-mx-5 -mt-5 flex items-center gap-1.5 rounded-t-xl border-b border-fail/15 bg-fail-soft px-5 py-2 text-xs font-medium text-fail">
          <ReasonIcon className="size-3.5 shrink-0" />{reason}
        </p>
      )}
      <div className="flex items-start justify-between gap-3">
        <h3 className={cn("font-semibold leading-snug", rejected && "text-muted-foreground")}>
          <Link href={`/contracts/${c.id}`} className="after:absolute after:inset-0 after:rounded-xl">{c.goal}</Link>
        </h3>
        <StatusBadge status={status} />
      </div>
      <p className={cn("text-3xl font-semibold tracking-tight tabular-nums", rejected && "text-muted-foreground")}>
        <span className="mr-1 text-lg font-normal text-muted-foreground">≤</span>{money(c.spend.hard_cap_all_in, c.spend.currency)}
      </p>

      {status === "pending" ? (
        <div className="mt-auto space-y-3">
          <div className="rounded-lg bg-muted/60 p-3 text-sm">
            <p className="font-medium">{approveLabel === "Simulated provider approval" ? "Approve funding (simulated)" : "Approve funding in Link"}</p>
            <p className="text-muted-foreground">Signed. Approve the single-use card for up to {money(c.spend.hard_cap_all_in, c.spend.currency)} so your agent can buy.</p>
          </div>
          <Button className="relative z-10 w-full bg-brand text-brand-foreground hover:bg-brand/90" size="lg" disabled={approving} onClick={() => onApproveFunding?.(c)}>
            <Check />{approving ? "Approving…" : approveLabel}
          </Button>
        </div>
      ) : (
        <div className="mt-auto space-y-1.5 text-sm text-muted-foreground">
          {!rejected && c.status !== "used" && <p>{relativeExpiry(c.expires_at)}</p>}
          {funding && !rejected && (
            <p className="flex items-center gap-1.5 text-foreground">
              <CreditCard className="size-3.5" />{funding.card_last4 && funding.state === "funded" ? `Card ending ${funding.card_last4} · locked` : fundingStateLabel(funding)}
            </p>
          )}
          <p>{c.single_use ? "Single use" : "Reusable"} · {c.constraints.filter((k) => k.severity === "hard").length} hard rules</p>
          {draft && (
            <p className="flex items-center gap-1 font-medium text-warn"><Sparkles className="size-3.5" />Review {inferredCount(c)} inferred values, then sign</p>
          )}
          {c.previous_contract_id && (
            <p className="flex items-center gap-1 text-xs"><GitBranch className="size-3.5" />New version of an earlier contract</p>
          )}
        </div>
      )}
    </div>
  );
}
