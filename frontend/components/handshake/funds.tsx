import { cn } from "@/lib/utils";
import type { Funding } from "@/lib/types";
import { money } from "@/lib/format";
import { Landmark } from "lucide-react";

/** One-line summary of where the user's prepaid money is. */
export function fundsSummary(f: Funding | null | undefined): string | null {
  if (!f) return null;
  if (f.status === "held") return `${money(f.amount_held)} held`;
  if (f.status === "refunded") return `${money(f.amount_refunded)} refunded`;
  return f.amount_refunded > 0
    ? `Paid ${money(f.amount_captured)} · ${money(f.amount_refunded)} refunded`
    : `Paid ${money(f.amount_captured)}`;
}

export function FundsPanel({ funding: f }: { funding: Funding | null | undefined }) {
  if (!f) return null;
  const rows: [string, number, string?][] = [
    ["You paid when signing", f.amount_held],
    ...(f.amount_captured ? [["Spent on the purchase", -f.amount_captured] as [string, number]] : []),
    ...(f.amount_refunded ? [["Refunded to your card", -f.amount_refunded, "text-pass"] as [string, number, string]] : []),
  ];
  const remaining = Math.round((f.amount_held - f.amount_captured - f.amount_refunded) * 100) / 100;
  return (
    <div className="rounded-xl border bg-card p-4 sm:p-5">
      <h2 className="flex items-center gap-2 font-semibold"><Landmark className="size-4" />Your money</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        {f.status === "held" && "Handshake is holding this until your agent finds a match you approve. Whatever isn't spent is refunded. If nothing is found, you get all of it back."}
        {f.status === "captured" && "The purchase was paid from your held funds and the rest went back to your card."}
        {f.status === "refunded" && "No purchase was made, so the full amount went back to your card."}
      </p>
      <dl className="mt-3 space-y-1.5 text-sm">
        {rows.map(([label, amount, cls]) => (
          <div key={label} className={cn("flex justify-between gap-3", cls)}>
            <dt>{label}</dt><dd className="tabular-nums">{amount < 0 ? `− ${money(-amount)}` : money(amount)}</dd>
          </div>
        ))}
        <div className="flex justify-between gap-3 border-t pt-1.5 font-semibold">
          <dt>Still held</dt><dd className="tabular-nums">{money(remaining)}</dd>
        </div>
      </dl>
    </div>
  );
}
