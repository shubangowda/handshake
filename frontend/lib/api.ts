// Every backend call goes through this file.
// NEXT_PUBLIC_USE_MOCKS=false + NEXT_PUBLIC_API_URL=http://localhost:8000 switches to the FastAPI backend.
import * as fixtures from "./mock-data";
import type {
  Contract, ContractRecord, DraftPatch, DraftRecord, EvidenceEvent, PurchaseDetail,
} from "./types";

export const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS !== "false";
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status} ${body}`);
  }
  return res.json() as Promise<T>;
}

// ---------- In-memory mock store (lives for the browser session) ----------

const clone = <T,>(v: T): T => structuredClone(v);
const store = {
  drafts: clone(fixtures.drafts),
  contracts: clone(fixtures.contracts),
  purchases: clone(fixtures.purchases),
  evidence: clone(fixtures.evidence),
};
const delay = (ms = 250) => new Promise((r) => setTimeout(r, ms));
const iso = () => new Date().toISOString();
const newId = (prefix: string) => `${prefix}_${Math.random().toString(16).slice(2, 14)}`;

function findRecord(id: string): ContractRecord | undefined {
  return store.drafts.find((d) => d.id === id) ?? store.contracts.find((c) => c.id === id);
}

function pushEvent(e: Omit<EvidenceEvent, "id" | "timestamp">) {
  const key = e.purchase_id ?? `contract:${e.contract_id}`;
  (store.evidence[key] ??= []).push({ ...e, id: newId("event"), timestamp: iso() });
}

// ---------- Public API ----------

export async function listContracts(): Promise<ContractRecord[]> {
  if (!USE_MOCKS) {
    // ASSUMPTION: drafts come from GET /drafts until the backend decides otherwise.
    const [contracts, drafts] = await Promise.all([
      http<Contract[]>("/contracts"),
      http<DraftRecord[]>("/drafts").catch(() => []),
    ]);
    return [...drafts, ...contracts];
  }
  await delay();
  return clone([...store.drafts, ...store.contracts]);
}

export async function getContract(id: string): Promise<ContractRecord> {
  if (!USE_MOCKS) {
    return id.startsWith("draft_") ? http<DraftRecord>(`/drafts/${id}`) : http<Contract>(`/contracts/${id}`);
  }
  await delay();
  const record = findRecord(id);
  if (!record) throw new Error(`Contract ${id} not found`);
  return clone(record);
}

function refundAll(c: Contract, why: string) {
  const f = c.funding;
  if (!f || f.status !== "held") return;
  Object.assign(f, { status: "refunded", amount_refunded: f.amount_held, settled_at: iso() });
  pushEvent({ purchase_id: null, contract_id: c.id, event_type: "funds_refunded", message: `$${f.amount_held.toFixed(2)} refunded to your card · ${why}`, data: {} });
}

/**
 * Signs the draft AND takes payment for its hard cap, which Handshake holds until a purchase.
 * PROPOSED: backend creates the hold (e.g. a Stripe PaymentIntent with manual capture) at signing.
 * Card details are never passed here. Stripe's own form will collect them.
 */
export async function signContract(draftId: string): Promise<Contract> {
  if (!USE_MOCKS) {
    return http<Contract>(`/contracts/${draftId}/sign`, {
      method: "POST", body: JSON.stringify({ draft_id: draftId }),
    });
  }
  await delay(500);
  const i = store.drafts.findIndex((d) => d.id === draftId);
  if (i < 0) throw new Error("Draft not found");
  const draft = store.drafts[i];
  const { status: _s, assumptions: _a, clarifications_needed: _c, compiler_notes: _n, ...body } = draft;
  const contract: Contract = {
    ...body, id: newId("contract"), status: "active", agent_key: "agent_demo",
    signed_at: iso(), contract_hash: `sha256:${newId("h").slice(2)}`, signature: "ed25519:demo",
    previous_contract_id: draft.previous_contract_id,
    funding: { status: "held", amount_held: draft.spend.hard_cap_all_in, amount_captured: 0, amount_refunded: 0, funded_at: iso(), settled_at: null },
  };
  // A new version supersedes the one it was edited from; its held funds go back to the user.
  if (draft.previous_contract_id) {
    const prev = store.contracts.find((c) => c.id === draft.previous_contract_id);
    if (prev && prev.status === "active") { prev.status = "revoked"; refundAll(prev, "replaced by a new version"); }
  }
  store.drafts.splice(i, 1);
  store.contracts.unshift(contract);
  pushEvent({ purchase_id: null, contract_id: contract.id, event_type: "contract_signed", message: `Contract signed · $${draft.spend.hard_cap_all_in.toFixed(2)} held`, data: {} });
  return clone(contract);
}

export async function revokeContract(id: string): Promise<Contract> {
  if (!USE_MOCKS) return http<Contract>(`/contracts/${id}/revoke`, { method: "POST" });
  await delay();
  const c = store.contracts.find((x) => x.id === id);
  if (!c) throw new Error("Contract not found");
  c.status = "revoked";
  pushEvent({ purchase_id: null, contract_id: id, event_type: "contract_revoked", message: "Contract revoked", data: {} });
  refundAll(c, "contract revoked");
  return clone(c);
}

/** PROPOSED endpoint: POST /contracts/:id/amend → new draft pointing at the signed contract. */
export async function amendContract(id: string): Promise<DraftRecord> {
  if (!USE_MOCKS) return http<DraftRecord>(`/contracts/${id}/amend`, { method: "POST" });
  await delay();
  const c = store.contracts.find((x) => x.id === id);
  if (!c) throw new Error("Contract not found");
  const { status: _s, agent_key: _k, signed_at: _t, contract_hash: _h, signature: _g, funding: _f, ...body } = c;
  const draft: DraftRecord = {
    ...body, id: newId("draft"), status: "draft", created_at: iso(), previous_contract_id: c.id,
    assumptions: [], clarifications_needed: [], compiler_notes: [],
  };
  store.drafts.unshift(draft);
  return clone(draft);
}

/** PROPOSED endpoint: PATCH /drafts/:id. Edited values become user-specified. */
export async function updateDraft(id: string, patch: DraftPatch): Promise<DraftRecord> {
  if (!USE_MOCKS) return http<DraftRecord>(`/drafts/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
  await delay();
  const d = store.drafts.find((x) => x.id === id);
  if (!d) throw new Error("Draft not found");
  if (patch.goal !== undefined) d.goal = patch.goal;
  if (patch.target !== undefined && patch.target !== d.spend.target) {
    d.spend.target = patch.target; d.spend.target_source = "user";
  }
  if (patch.hard_cap_all_in !== undefined && patch.hard_cap_all_in !== d.spend.hard_cap_all_in) {
    d.spend.hard_cap_all_in = patch.hard_cap_all_in; d.spend.hard_cap_source = "user";
  }
  if (d.delivery) {
    if (patch.max_shipping !== undefined && patch.max_shipping !== d.delivery.max_shipping) {
      d.delivery.max_shipping = patch.max_shipping; d.delivery.max_shipping_source = "user";
    }
    if (patch.deliver_by !== undefined && patch.deliver_by !== d.delivery.deliver_by) {
      d.delivery.deliver_by = patch.deliver_by; d.delivery.deliver_by_source = "user";
    }
  }
  if (patch.constraints) d.constraints = patch.constraints;
  return clone(d);
}

export async function getPurchase(id: string): Promise<PurchaseDetail> {
  // PROPOSED: backend includes `proposal` alongside PurchaseStatusResponse.
  if (!USE_MOCKS) return http<PurchaseDetail>(`/purchases/${id}`);
  await delay();
  const p = store.purchases.find((x) => x.purchase.id === id);
  if (!p) throw new Error(`Purchase ${id} not found`);
  return clone(p);
}

export async function listPurchasesForContract(contractId: string): Promise<PurchaseDetail[]> {
  if (!USE_MOCKS) return http<PurchaseDetail[]>(`/contracts/${contractId}/purchases`).catch(() => []);
  await delay(100);
  return clone(store.purchases.filter((p) => p.purchase.contract_id === contractId));
}

export async function listPurchases(): Promise<PurchaseDetail[]> {
  if (!USE_MOCKS) return http<PurchaseDetail[]>("/purchases").catch(() => []);
  await delay(100);
  return clone(store.purchases);
}

/** Pay the purchase total from the contract's held funds and refund the rest. */
function capture(p: PurchaseDetail) {
  const total = p.proposal?.total ?? 0;
  const contractId = p.purchase.contract_id;
  const c = store.contracts.find((x) => x.id === contractId);
  p.purchase.status = "completed";
  p.purchase.authorized_amount = total;
  p.purchase.charged_amount = total;
  p.purchase.credential_id = newId("cred");
  p.purchase.completed_at = iso();
  pushEvent({ purchase_id: p.purchase.id, contract_id: contractId, event_type: "payment_completed", message: `Paid $${total.toFixed(2)} from held funds`, data: {} });
  if (c?.funding?.status === "held") {
    const refund = Math.round((c.funding.amount_held - total) * 100) / 100;
    Object.assign(c.funding, { status: "captured", amount_captured: total, amount_refunded: refund, settled_at: iso() });
    if (refund > 0) pushEvent({ purchase_id: p.purchase.id, contract_id: contractId, event_type: "funds_refunded", message: `$${refund.toFixed(2)} refunded to your card`, data: {} });
  }
  if (c?.single_use) c.status = "used";
}

/** PROPOSED endpoint: POST /purchases/:id/approve. The user's one tap after Handshake finds a match. */
export async function approvePurchase(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return http<PurchaseDetail>(`/purchases/${id}/approve`, { method: "POST" });
  await delay(700);
  const p = store.purchases.find((x) => x.purchase.id === id);
  if (!p) throw new Error("Purchase not found");
  if (p.purchase.status !== "authorized") throw new Error("This purchase isn't waiting for approval");
  pushEvent({ purchase_id: id, contract_id: p.purchase.contract_id, event_type: "escalation_approved", message: "You approved the purchase", data: {} });
  capture(p);
  return clone(p);
}

/** PROPOSED endpoint: POST /purchases/:id/decline. Funds stay held and the agent keeps looking. */
export async function declinePurchase(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return http<PurchaseDetail>(`/purchases/${id}/decline`, { method: "POST" });
  await delay(400);
  const p = store.purchases.find((x) => x.purchase.id === id);
  if (!p) throw new Error("Purchase not found");
  p.purchase.status = "blocked";
  pushEvent({ purchase_id: id, contract_id: p.purchase.contract_id, event_type: "purchase_declined", message: "You declined this product · funds still held, agent keeps looking", data: {} });
  return clone(p);
}

export async function getEvidence(purchaseId: string): Promise<EvidenceEvent[]> {
  if (!USE_MOCKS) return http<EvidenceEvent[]>(`/evidence/${purchaseId}`);
  await delay(100);
  return clone(store.evidence[purchaseId] ?? []);
}

/**
 * PROPOSED endpoint: POST /purchases/:id/resolve { action }.
 * Approving never converts UNVERIFIABLE into PASS — the decision keeps its verdict
 * and the purchase records who accepted which unverified constraints.
 */
export async function resolveEscalation(id: string, action: "approve" | "reject"): Promise<PurchaseDetail> {
  if (!USE_MOCKS) {
    return http<PurchaseDetail>(`/purchases/${id}/resolve`, { method: "POST", body: JSON.stringify({ action }) });
  }
  await delay(600);
  const p = store.purchases.find((x) => x.purchase.id === id);
  if (!p) throw new Error("Purchase not found");
  const accepted = p.decision?.results.filter((r) => r.verdict === "unverifiable" && r.severity === "hard").map((r) => r.constraint) ?? [];
  p.purchase.resolution = { action, resolved_at: iso(), accepted_constraints: action === "approve" ? accepted : [] };
  const contractId = p.purchase.contract_id;
  if (action === "approve") {
    // Approving the exception is the user's approval of the purchase, so pay right away.
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "escalation_approved", message: "You approved an exception and the purchase", data: { accepted } });
    capture(p);
  } else {
    p.purchase.status = "blocked";
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "escalation_rejected", message: "You rejected the purchase · funds still held", data: {} });
  }
  return clone(p);
}
