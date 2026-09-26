"use client";

import { cn } from "@/lib/utils";
import { Check, Loader2, X, CircleHelp } from "lucide-react";

export type Outcome = "pass" | "fail" | "unverifiable";

const STEPS = ["Searching", "Product selected", "Validating checkout", "Checking contract"];
const FINAL = { pass: "Authorized", fail: "Blocked", unverifiable: "Needs you" } as const;

/** Agent activity stepper. `step` counts completed steps; STEPS.length + 1 means the verdict is shown. */
export function Progression({ step, outcome }: { step: number; outcome: Outcome }) {
  const labels = [...STEPS, FINAL[outcome]];
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-2 text-sm" aria-live="polite">
      {labels.map((label, i) => {
        const done = i < step;
        const current = i === step;
        const last = i === labels.length - 1;
        const finalTone = outcome === "pass" ? "bg-pass text-white" : outcome === "fail" ? "bg-fail text-white" : "bg-warn text-white";
        const FinalIcon = outcome === "pass" ? Check : outcome === "fail" ? X : CircleHelp;
        return (
          <li key={label} className="flex items-center gap-2">
            <span className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-full px-3 transition-all duration-300",
              last && done ? cn(finalTone, "font-semibold") :
              done ? "bg-pass-soft text-pass" :
              current ? "bg-foreground text-background" : "bg-muted text-muted-foreground/70",
            )}>
              {last && done ? <FinalIcon className="size-3.5" strokeWidth={3} /> : done ? <Check className="size-3.5" /> : current ? <Loader2 className="size-3.5 animate-spin" /> : null}
              {label}
            </span>
            {!last && <span className={cn("h-px w-3 sm:w-5", done ? "bg-pass" : "bg-border")} />}
          </li>
        );
      })}
    </ol>
  );
}

export const PROGRESSION_STEPS = STEPS.length + 1;
