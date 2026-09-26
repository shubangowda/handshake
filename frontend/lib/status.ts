// Dashboard grouping. Frontend-only: the backend keeps its own ContractStatus values.
import type { ContractRecord, PurchaseDetail } from "./types";
import { formatDate } from "./format";

export type DisplayStatus = "active" | "pending" | "draft" | "used" | "rejected";

/** The purchase that passed Handshake and is waiting for the user's approval, if any. */
export function pendingPurchase(c: ContractRecord, purchases: PurchaseDetail[]): PurchaseDetail | undefined {
  if (c.status !== "active") return undefined;
  return purchases.find((p) => p.purchase.contract_id === c.id && p.purchase.status === "authorized");
}

export function displayStatus(c: ContractRecord, purchases: PurchaseDetail[]): DisplayStatus {
  if (c.status === "revoked" || c.status === "expired") return "rejected";
  if (pendingPurchase(c, purchases)) return "pending";
  return c.status;
}

/** One-line explanation shown on rejected contracts. */
export function rejectionReason(c: ContractRecord): string | null {
  if (c.status === "revoked") return "Revoked by you";
  if (c.status === "expired") return `Expired ${formatDate(c.expires_at, { month: "short", day: "numeric" })}. No match found in time`;
  return null;
}
