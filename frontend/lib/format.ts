import type { Constraint, ConstraintField, ContractRecord, DraftRecord, SellerRequirement } from "./types";

export const money = (n: number | null | undefined, currency = "USD") =>
  n == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n);

export const FIELD_LABELS: Record<ConstraintField, string> = {
  category: "Product", brand: "Brand", model: "Model", product_name: "Product name",
  condition: "Condition", color: "Color", size: "Size", quantity: "Quantity",
  screen_size_inches: "Screen size", display_type: "Display", refresh_rate_hz: "Refresh rate",
  storage_gb: "Storage", memory_gb: "Memory", material: "Material", gender: "Fit for", fit: "Fit",
  food_size: "Size", toppings: "Toppings", dietary: "Dietary",
  event_name: "Event", ticket_quantity: "Tickets", seats_together: "Seats together", section: "Section",
  merchant_name: "Merchant", seller_of_record: "Seller",
  return_days: "Returns", subscription: "Subscription", membership: "Membership", addons: "Add-ons",
  delivery_date: "Delivery", item_price: "Item price", shipping: "Shipping", fees: "Fees",
  tax: "Tax", total_price: "Total",
};

const UNITS: Partial<Record<ConstraintField, string>> = {
  screen_size_inches: "″", refresh_rate_hz: " Hz", storage_gb: " GB", memory_gb: " GB", return_days: " days",
};
const MONEY_FIELDS: ConstraintField[] = ["item_price", "shipping", "fees", "tax", "total_price"];

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function show(field: ConstraintField, v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => show(field, x)).join(", ");
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number" && MONEY_FIELDS.includes(field)) return money(v);
  if (v == null) return "—";
  if (field === "condition") return cap(String(v).replace("_", " "));
  return cap(String(v)) + (UNITS[field] ?? "");
}

/** Plain-English value for a constraint row: "≤ $135.00", "One of Nike, Brooks", "Not white". */
export function describeConstraint(c: Constraint): string {
  const v = show(c.field, c.value);
  switch (c.operator) {
    case "eq": return v;
    case "neq": return `Not ${v.toLowerCase()}`;
    case "lt": return `Under ${v}`;
    case "lte": return `≤ ${v}`;
    case "gt": return `Over ${v}`;
    case "gte": return `≥ ${v}`;
    case "in": return Array.isArray(c.value) && c.value.length > 1 ? `One of ${v}` : v;
    case "not_in": return `Not ${v}`;
    case "contains": return `Includes ${v.toLowerCase()}`;
    case "not_contains": return `Excludes ${v.toLowerCase()}`;
    case "before": return `By ${v}`;
    case "after": return `After ${v}`;
  }
}

export const SELLER_LABELS: Record<SellerRequirement, string> = {
  any: "Any seller",
  first_party: "Brand's own store only",
  verified: "Verified retailers only",
  first_party_or_verified: "Brand store or verified retailer",
};

export function formatDate(iso: string | null | undefined, opts: Intl.DateTimeFormatOptions = { weekday: "long", month: "short", day: "numeric" }) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", opts);
}

export function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

/** "Expires tomorrow", "Expires in 3 hours", "Expired 2 days ago". */
export function relativeExpiry(iso: string | null): string {
  if (!iso) return "No expiry";
  const diff = new Date(iso).getTime() - Date.now();
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const [value, unit]: [number, Intl.RelativeTimeFormatUnit] =
    abs < 3_600_000 ? [Math.round(diff / 60_000), "minute"]
    : abs < 86_400_000 * 0.9 ? [Math.round(diff / 3_600_000), "hour"]
    : [Math.round(diff / 86_400_000), "day"];
  return `${diff >= 0 ? "Expires" : "Expired"} ${rtf.format(value, unit)}`;
}

export function shortId(id: string) {
  return id.replace(/^[a-z]+_/, "").slice(0, 8);
}

export const isDraft = (c: ContractRecord): c is DraftRecord => c.status === "draft" && "assumptions" in c;
