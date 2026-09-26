import { cn } from "@/lib/utils";
import type { EvidenceEvent, EvidenceEventType } from "@/lib/types";
import { formatTime } from "@/lib/format";

const TONE: Partial<Record<EvidenceEventType, string>> = {
  purchase_blocked: "bg-fail",
  escalation_rejected: "bg-fail",
  payment_mismatch: "bg-fail",
  contract_revoked: "bg-fail",
  purchase_escalated: "bg-warn",
  escalation_approved: "bg-warn",
  payment_completed: "bg-pass",
  purchase_authorized: "bg-pass",
  credential_created: "bg-pass",
  credential_used: "bg-pass",
};

export function Timeline({ events }: { events: EvidenceEvent[] }) {
  if (!events.length) return <p className="text-sm text-muted-foreground">No activity yet.</p>;
  return (
    <ol className="relative space-y-3 before:absolute before:top-2 before:bottom-2 before:left-[4.75rem] before:w-px before:bg-border">
      {events.map((e) => (
        <li key={e.id} className="grid grid-cols-[4rem_1.5rem_minmax(0,1fr)] items-start text-sm">
          <time className="pt-px text-right font-mono text-xs text-muted-foreground tabular-nums">{formatTime(e.timestamp)}</time>
          <span className="flex justify-center pt-1.5"><span className={cn("relative size-2.5 rounded-full ring-4 ring-card", TONE[e.event_type] ?? "bg-muted-foreground/50")} /></span>
          <span className={cn(TONE[e.event_type] === "bg-fail" && "font-semibold text-fail")}>{e.message}</span>
        </li>
      ))}
    </ol>
  );
}
