import { cn } from "@/lib/utils";
import type { ConstraintResult } from "@/lib/types";
import { Check, CircleHelp, X } from "lucide-react";

const ICON = {
  pass: { Icon: Check, cls: "bg-pass text-white", word: "PASS", text: "text-pass" },
  fail: { Icon: X, cls: "bg-fail text-white", word: "FAIL", text: "text-fail" },
  unverifiable: { Icon: CircleHelp, cls: "bg-warn text-white", word: "UNVERIFIED", text: "text-warn" },
} as const;

/** Intent diff: what the contract said vs. what checkout showed, one row per constraint. */
export function VerdictRows({ results, acceptedByUser = [] }: { results: ConstraintResult[]; acceptedByUser?: string[] }) {
  // Failures first, then unverifiable, then passes — the problem is always on top.
  const order = { fail: 0, unverifiable: 1, pass: 2 };
  const weight = { hard: 0, escalating: 1, soft: 2 };
  const sorted = [...results].sort((a, b) => order[a.verdict] - order[b.verdict] || weight[a.severity] - weight[b.severity]);
  return (
    <ul className="divide-y rounded-xl border bg-card">
      {sorted.map((r) => {
        const { Icon, cls, word, text } = ICON[r.verdict];
        const accepted = acceptedByUser.includes(r.constraint);
        return (
          <li key={r.constraint}
            className={cn("grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-3 px-4 py-3",
              r.verdict === "fail" && "bg-fail-soft", r.verdict === "unverifiable" && r.severity === "hard" && "bg-warn-soft/70")}>
            <span className={cn("mt-0.5 grid size-6 place-items-center rounded-full", cls)}><Icon className="size-3.5" strokeWidth={3} /></span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <span className="font-medium">{r.label ?? r.constraint}</span>
                <span className={cn("text-sm tabular-nums", r.verdict === "fail" ? "font-semibold text-fail" : "text-muted-foreground")}>
                  {String(r.actual ?? "—")}
                  {r.expected != null && <span className="text-muted-foreground"> · contract: {String(r.expected)}</span>}
                </span>
              </div>
              {r.verdict !== "pass" && <p className={cn("mt-0.5 text-sm", text)}>{r.reason}</p>}
              {accepted && <p className="mt-1 text-xs font-medium text-warn">Still unverified. You approved an exception.</p>}
              {r.severity === "soft" && r.verdict !== "pass" && <p className="mt-0.5 text-xs text-muted-foreground">Preference only. Doesn&apos;t block the purchase.</p>}
            </div>
            <span className={cn("font-mono text-[11px] font-bold tracking-wider", text)}>{word}</span>
          </li>
        );
      })}
    </ul>
  );
}
