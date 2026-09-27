// Dashboard grouping and plain-English payment and funding wording. Frontend-only: the backend keeps its own statuses.
import type { ContractRecord, Funding, FundingState, PaymentState, PurchaseDetail, SignedContract } from "./types";
import { formatDate } from "./format";

export type DisplayStatus = "active" | "pending" | "draft" | "used" | "rejected";

/** A signed contract's funding, when api.ts attached it (it always does for GET /contracts/{id}). */
export function fundingOf(c: ContractRecord): Funding | undefined {
  return c.status === "draft" ? undefined : (c as SignedContract).funding;
}

/**
 * "Pending" = signed, but its funding card is waiting for the user's approval in Link
 * ("Approve funding in Link"). Signing is funding, so this is the one step left for the user.
 */
export function awaitingFunding(c: ContractRecord): boolean {
  return c.status === "active" && fundingOf(c)?.state === "awaiting_approval";
}

export function displayStatus(c: ContractRecord): DisplayStatus {
  if (c.status === "revoked" || c.status === "expired") return "rejected";
  if (awaitingFunding(c)) return "pending";
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

/** The card hasn't been released yet, so "Not this one" can still decline (the card stays locked on the contract). */
export function canDecline(d: PurchaseDetail): boolean {
  return d.status === "authorized" && d.payment?.state === "credential_ready" && !d.payment.credential_released;
}

export const PAYMENT_STATE_LABELS: Record<PaymentState, string> = {
  credential_ready: "Card unlocked for this checkout",
  paying: "Card released to agent",
  paid: "Paid",
  completed: "Completed",
  denied: "Declined",
  expired: "Authorization expired",
  checkout_changed: "The checkout changed; the card stayed locked",
  failed: "Payment failed",
  unknown: "Checking with the merchant",
};

/** Short label for lists (e.g. purchase attempts on a contract). */
export function paymentLabel(d: PurchaseDetail): string | null {
  const s = d.payment_state;
  if (!s) return null;
  if (s === "denied" && d.resolution?.action === "decline") return "Declined by you";
  return PAYMENT_STATE_LABELS[s];
}

export const FUNDING_STATE_LABELS: Record<FundingState, string> = {
  not_funded: "Not funded",
  awaiting_approval: "Waiting for your approval in Link",
  funded: "Funded · card stored, locked",
  released: "Card released for a purchase",
  used: "Card used",
  denied: "You declined the card in Link",
  expired: "Funded card expired",
  failed: "Funding failed",
  canceled: "Funding canceled",
};

/** States from which the user can ask for a fresh card (POST /contracts/{id}/funding). */
export const FUND_AGAIN_STATES: FundingState[] = ["not_funded", "denied", "expired", "failed", "used"];
