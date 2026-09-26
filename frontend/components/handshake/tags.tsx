import { cn } from "@/lib/utils";
import type { ConstraintSeverity, ContractStatus, ValueSource } from "@/lib/types";
import type { DisplayStatus } from "@/lib/status";
import { Lock, Sparkles, SlidersHorizontal, User, Hand } from "lucide-react";

const STATUS_STYLES: Record<ContractStatus | DisplayStatus, string> = {
  active: "bg-pass text-white",
  pending: "bg-brand text-brand-foreground",
  rejected: "bg-fail-soft text-fail ring-1 ring-fail/30",
  draft: "bg-warn-soft text-warn ring-1 ring-warn/40",
  used: "bg-muted text-foreground ring-1 ring-border",
  revoked: "bg-fail-soft text-fail ring-1 ring-fail/30",
  expired: "bg-muted text-muted-foreground",
};

export function StatusBadge({ status, className }: { status: ContractStatus | DisplayStatus; className?: string }) {
  return (
    <span className={cn("inline-flex h-6 items-center rounded-md px-2 font-mono text-[11px] font-semibold tracking-wider uppercase", STATUS_STYLES[status], className)}>
      {status}
    </span>
  );
}

/** Where a value came from. Inferred/default values are what the user must review before signing. */
export function SourceTag({ source }: { source: ValueSource }) {
  if (source === "user") {
    return <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground"><User className="size-3" />You</span>;
  }
  return (
    <span className={cn(
      "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium",
      source === "inferred" ? "bg-warn-soft text-warn" : "bg-muted text-muted-foreground",
    )}>
      <Sparkles className="size-3" />
      {source === "inferred" ? "Inferred" : "Default"}
    </span>
  );
}

export function SeverityTag({ severity }: { severity: ConstraintSeverity }) {
  const map = {
    hard: { icon: Lock, text: "Must", cls: "text-foreground" },
    soft: { icon: SlidersHorizontal, text: "Prefer", cls: "text-muted-foreground" },
    escalating: { icon: Hand, text: "Ask me", cls: "text-warn" },
  }[severity];
  const Icon = map.icon;
  return <span className={cn("inline-flex items-center gap-1 text-[11px] font-medium", map.cls)}><Icon className="size-3" />{map.text}</span>;
}
