import { cn } from "@/lib/utils";
import type { ConstraintSeverity, ContractRecord, ValueSource } from "@/lib/types";
import { FIELD_LABELS, SELLER_LABELS, describeConstraint, formatDate, money, relativeExpiry } from "@/lib/format";
import { SeverityTag, SourceTag } from "./tags";

type Row = {
  label: string;
  value: string;
  source?: ValueSource;
  severity?: ConstraintSeverity;
  note?: string | null;
  emphasis?: boolean;
};

function Section({ title, rows }: { title: string; rows: Row[] }) {
  if (!rows.length) return null;
  return (
    <div>
      <h3 className="mb-1 px-4 pt-4 font-mono text-[11px] font-semibold tracking-widest text-muted-foreground uppercase sm:px-6">{title}</h3>
      <dl>
        {rows.map((r) => {
          const flagged = r.source === "inferred" || r.source === "default";
          return (
            <div key={r.label + r.value}
              className={cn(
                "grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 border-l-[3px] px-4 py-2.5 sm:grid-cols-[10rem_minmax(0,1fr)_auto] sm:px-6",
                r.source === "inferred" ? "border-warn bg-warn-soft/70" : r.source === "default" ? "border-dashed border-muted-foreground/40" : "border-transparent",
              )}>
              <dt className="text-sm text-muted-foreground">{r.label}</dt>
              <dd className={cn("col-start-1 row-start-2 min-w-0 sm:col-start-2 sm:row-start-1", r.emphasis ? "text-lg font-semibold tabular-nums" : "text-[15px] font-medium")}>
                {r.value}
                {r.note && <p className={cn("mt-0.5 text-xs font-normal", flagged ? "text-warn" : "text-muted-foreground")}>{r.note}</p>}
              </dd>
              <div className="col-start-2 row-span-2 row-start-1 flex flex-col items-end gap-1 sm:col-start-3 sm:row-span-1">
                {r.source && <SourceTag source={r.source} />}
                {r.severity && <SeverityTag severity={r.severity} />}
              </div>
            </div>
          );
        })}
      </dl>
    </div>
  );
}

export function contractRows(c: ContractRecord) {
  const cur = c.spend.currency;
  const spend: Row[] = [
    ...(c.spend.target != null ? [{ label: "Target price", value: money(c.spend.target, cur), source: c.spend.target_source, severity: "soft" as const }] : []),
    { label: "Maximum total", value: money(c.spend.hard_cap_all_in, cur), source: c.spend.hard_cap_source, severity: "hard", emphasis: true,
      note: "All-in: item, tax, shipping and fees." },
  ];

  const item: Row[] = c.constraints.filter((k) => k.severity !== "soft").map((k) => ({
    label: FIELD_LABELS[k.field], value: describeConstraint(k), source: k.source, severity: k.severity, note: k.description,
  }));
  const prefs: Row[] = c.constraints.filter((k) => k.severity === "soft").map((k) => ({
    label: FIELD_LABELS[k.field], value: describeConstraint(k), source: k.source, severity: k.severity, note: k.description,
  }));

  const delivery: Row[] = c.delivery ? [
    ...(c.delivery.deliver_by ? [{ label: "Delivery", value: `By ${formatDate(c.delivery.deliver_by)}`, source: c.delivery.deliver_by_source, severity: "hard" as const }] : []),
    ...(c.delivery.max_shipping != null ? [{ label: "Shipping", value: `≤ ${money(c.delivery.max_shipping, cur)}`, source: c.delivery.max_shipping_source, severity: "hard" as const }] : []),
  ] : [];

  // TermsPolicy has no per-field source in the backend yet; shown as Handshake defaults.
  const terms: Row[] = [
    { label: "Subscription", value: c.terms.no_subscription ? "Not allowed" : "Allowed", source: "default", severity: "hard" },
    { label: "Membership", value: c.terms.no_membership ? "Not allowed" : "Allowed", source: "default", severity: "hard" },
    { label: "Add-ons", value: c.terms.no_addons ? "Not allowed" : "Allowed", source: "default", severity: "hard" },
    ...(c.terms.min_return_days != null ? [{ label: "Returns", value: `At least ${c.terms.min_return_days} days`, source: "default" as const, severity: "hard" as const }] : []),
    { label: "Seller", value: SELLER_LABELS[c.merchants.seller_requirement], source: "default", severity: "hard" },
    { label: "New merchants", value: c.merchants.new_merchant === "escalate" ? "Ask me first" : c.merchants.new_merchant === "deny" ? "Blocked" : "Allowed", source: "default", severity: "escalating" },
    ...(c.merchants.deny.length ? [{ label: "Never buy from", value: c.merchants.deny.join(", ") }] : []),
  ];

  return { spend, item, prefs, delivery, terms };
}

export function ContractSheet({ contract: c, className }: { contract: ContractRecord; className?: string }) {
  const rows = contractRows(c);
  return (
    <div className={cn("overflow-hidden rounded-xl border bg-card shadow-sm", className)}>
      <div className="flex flex-wrap items-end justify-between gap-3 border-b bg-brand px-4 py-5 text-brand-foreground sm:px-6">
        <div className="min-w-0">
          <p className="font-mono text-[11px] font-semibold tracking-[0.3em] opacity-70">HANDSHAKE</p>
          <h2 className="mt-1 text-2xl font-semibold tracking-tight text-balance">{c.goal}</h2>
        </div>
        <div className="text-right text-xs opacity-80">
          <p>{c.single_use ? "Single use" : "Reusable"} · {c.revocable ? "Revocable" : "Not revocable"}</p>
          <p>{relativeExpiry(c.expires_at)}</p>
        </div>
      </div>
      <div className="divide-y pb-2">
        <Section title="Spend" rows={rows.spend} />
        <Section title="What to buy" rows={rows.item} />
        <Section title="Delivery" rows={rows.delivery} />
        <Section title="Terms & sellers" rows={rows.terms} />
        <Section title="Preferences (not enforced)" rows={rows.prefs} />
      </div>
      <div className="border-t bg-muted/40 px-4 py-3 text-xs text-muted-foreground sm:px-6">
        Paid with a {c.instrument.type === "virtual_single_use" ? "single-use virtual card" : c.instrument.type.replace("_", " ")} capped at {money(c.spend.hard_cap_all_in)}.
        {" "}Merchant receives: {c.data_sharing.allowed_fields.map((f) => f.replace("_", " ")).join(", ") || "nothing"}.
      </div>
    </div>
  );
}
