// Dashboard grouping and plain-English payment wording. Frontend-only: the backend keeps its own statuses.
import type { ContractRecord, PaymentState, PurchaseDetail } from "./types";
import { formatDate } from "./format";

export type DisplayStatus = "active" | "pending" | "draft" | "used" | "rejected";

/**
 * The purchase that passed Handshake and is waiting for the user's payment approval in Link, if any.
 * Based on payment_state, not contract status: the backend marks a single-use contract "used"
 * while an authorized purchase holds it (and frees it again if that purchase is declined).
 */
export function pendingPurchase(c: ContractRecord, purchases: PurchaseDetail[]): PurchaseDetail | undefined {
  if (c.status === "draft" || c.status === "revoked" || c.status === "expired") return undefined;
  return purchases.find((p) => p.purchase.contract_id === c.id && p.payment_state === "awaiting_approval");
}

export function displayStatus(c: ContractRecord, purchases: PurchaseDetail[]): DisplayStatus {
  if (c.status === "revoked" || c.status === "expired") return "rejected";
  if (pendingPurchase(c, purchases)) return "pending";
  return c.status;
}

/** One-line explanation shown on rejected contracts. */
export function rejectionReason(c: ContractRecord): string | null {
  if (c.status === "revoked") return "Revoked";
  if (c.status === "expired") return `Expired ${formatDate(c.expires_at, { month: "short", day: "numeric" })}. No match found in time`;
  return null;
}

/** Payment states after which nothing more will happen. */
export const TERMINAL_PAYMENT_STATES: PaymentState[] = ["completed", "denied", "expired", "checkout_changed", "failed"];

/** The purchase page stops polling once this is true. */
export function isTerminal(d: PurchaseDetail): boolean {
  if (d.status === "completed" || d.status === "blocked" || d.status === "failed") return true;
  return d.payment_state != null && TERMINAL_PAYMENT_STATES.includes(d.payment_state);
}

/** Payment hasn't started (no card released, nothing submitted), so "Not this one" can still decline it. */
export function canDecline(d: PurchaseDetail): boolean {
  if (d.status !== "authorized" || !d.payment) return false;
  const s = d.payment.state;
  return s === "awaiting_approval" || s === "approved" || (s === "credential_ready" && !d.payment.credential_released);
}

export const PAYMENT_STATE_LABELS: Record<PaymentState, string> = {
  awaiting_approval: "Awaiting your Link approval",
  approved: "Approved in Link",
  revalidating: "Rechecking checkout",
  credential_ready: "Card ready",
  paying: "Paying",
  paid: "Paid",
  completed: "Completed",
  denied: "You declined in Link",
  expired: "Approval expired",
  checkout_changed: "The checkout changed after approval; nothing was paid",
  failed: "Payment failed",
  unknown: "Checking with the merchant",
};

/** Short label for lists (e.g. purchase attempts on a contract). */
export function paymentLabel(d: PurchaseDetail): string | null {
  const s = d.payment_state;
  if (!s) return null;
  if (s === "credential_ready" && d.payment?.credential_released) return "Card released to agent";
  if (s === "denied" && d.resolution?.action === "decline") return "Declined by you";
  return PAYMENT_STATE_LABELS[s];
}
