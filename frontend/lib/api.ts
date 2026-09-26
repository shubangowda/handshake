// Every backend call goes through this file. docs/API.md is the contract; this file adapts to it.
// NEXT_PUBLIC_USE_MOCKS=false + NEXT_PUBLIC_API_URL (see .env.example) switches to the FastAPI backend.
import * as fixtures from "./mock-data";
import { clearSession, readSession } from "./session";
import type {
  ApiErrorBody, Contract, ContractListItem, ContractRecord, ContractResponse, DemoLogin, DeviceAuthorization,
  DeviceDecision, DraftPatch, DraftRecord, EvidenceBundle, EvidenceEvent, Health, PaymentState, PurchaseDetail,
  SignedContract,
} from "./types";

export const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS !== "false";
// No fallback host on purpose: a missing setting should fail loudly, not quietly call some default address.
const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/+$/, "");

/** Shown by pages (and the header) when the app is pointed at the backend but doesn't know where it is. */
export const CONFIG_ERROR: string | null = !USE_MOCKS && !API_URL
  ? "NEXT_PUBLIC_API_URL is not set. Copy .env.example to .env.local (scripts/dev.py writes it for you), set the backend URL, and restart npm run dev."
  : null;

/** Every backend error: `message` is the human sentence; `code` and `details` come from {error, details}. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code: string, readonly details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * A 401 means the demo token is missing or expired: drop it and come back here after logging in.
 * This module has no router, so it does a full page load (which also clears any stale in-memory state).
 */
function redirectToLogin() {
  if (typeof window === "undefined" || window.location.pathname === "/login") return;
  const login = new URL("/login", window.location.origin);
  login.searchParams.set("next", window.location.pathname + window.location.search);
  window.location.assign(login.href);
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  if (CONFIG_ERROR) throw new ApiError(CONFIG_ERROR, 0, "config_missing_api_url");
  const token = readSession()?.token;
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(`Couldn't reach the Handshake backend at ${API_URL}. Is it running?`, 0, "network_error");
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as ApiErrorBody | null;
    if (res.status === 401 && path !== "/auth/demo-login") { clearSession(); redirectToLogin(); }
    throw new ApiError(
      body?.message ?? body?.error_description ?? `${init?.method ?? "GET"} ${path} failed (${res.status})`,
      res.status, body?.error ?? "http_error", body?.details ?? {},
    );
  }
  return res.json() as Promise<T>;
}

const post = <T,>(path: string, body?: unknown) =>
  http<T>(path, { method: "POST", ...(body === undefined ? {} : { body: JSON.stringify(body) }) });

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
const mockVerification = { valid: true, hash_matches: true, signature_matches: true };

function findRecord(id: string): ContractRecord | undefined {
  return store.drafts.find((d) => d.id === id) ?? store.contracts.find((c) => c.id === id);
}

function mockPurchase(id: string): PurchaseDetail {
  const p = store.purchases.find((x) => x.purchase.id === id);
  if (!p) throw new ApiError(`No purchase with id '${id}'.`, 404, "purchase_not_found");
  return p;
}

function pushEvent(e: Omit<EvidenceEvent, "id" | "timestamp">) {
  const key = e.purchase_id ?? `contract:${e.contract_id}`;
  (store.evidence[key] ??= []).push({ ...e, id: newId("event"), timestamp: iso() });
}

/** Mock stand-in for the backend's payment refresh: one step per poll, so the progression is visible. */
const MOCK_NEXT: Partial<Record<PaymentState, PaymentState>> = { approved: "revalidating", revalidating: "paying", paying: "paid", paid: "completed" };
function advanceMockPayment(d: PurchaseDetail) {
  const pay = d.payment;
  const next = pay && d.purchase.status === "authorized" ? MOCK_NEXT[pay.state] : undefined;
  if (!pay || !next) return;
  const { id, contract_id } = d.purchase;
  const total = pay.amount;
  pay.state = next;
  pay.updated_at = iso();
  if (next === "paying") {
    pushEvent({ purchase_id: id, contract_id, event_type: "validation_completed", message: "Checkout rechecked: unchanged since approval", data: { kind: "checkout_revalidated" } });
  }
  if (next === "paid") {
    pay.last4 = "4242";
    pushEvent({ purchase_id: id, contract_id, event_type: "credential_used", message: `Paid $${total.toFixed(2)} with the single-use card`, data: { kind: "payment_submitted" } });
  }
  if (next === "completed") {
    pay.order_id = `order_${newId("demo").slice(5, 11)}`;
    Object.assign(d.purchase, { status: "completed", charged_amount: total, completed_at: iso() });
    d.summary = `Completed: charged $${total.toFixed(2)} at ${d.purchase.merchant_name}.`;
    pushEvent({ purchase_id: id, contract_id, event_type: "payment_completed", message: "Merchant order verified against the receipt", data: { kind: "receipt_verified" } });
  }
  fixtures.syncDetail(d);
}

// ---------- Auth ----------

/** DEMO AUTH: exchanges an email for a user token. No password. To be replaced by passkeys/OAuth. */
export async function demoLogin(email: string): Promise<DemoLogin> {
  if (!USE_MOCKS) return post<DemoLogin>("/auth/demo-login", { email });
  await delay(300);
  return { token: "mock-token", token_type: "bearer", email: email.trim().toLowerCase(), role: "user", expires_at: Math.floor(Date.now() / 1000) + 86_400, demo_auth: true };
}

// ---------- Health (cached: the payment mode doesn't change while the backend runs) ----------

let healthValue: Health | null = null;
let healthPromise: Promise<Health> | null = null;

export function getHealth(): Promise<Health> {
  if (USE_MOCKS) return Promise.resolve(clone(fixtures.health));
  healthPromise ??= http<Health>("/health").then((h) => (healthValue = h), (e) => { healthPromise = null; throw e; });
  return healthPromise;
}

/** The cached health, if already fetched. Lets approvePurchase open Link's tab synchronously (see below). */
export function cachedHealth(): Health | null {
  return USE_MOCKS ? fixtures.health : healthValue;
}

// ---------- Drafts and contracts ----------

async function getSignedContract(id: string): Promise<ContractRecord> {
  const r = await http<ContractResponse>(`/contracts/${encodeURIComponent(id)}`);
  // GET /contracts/{id} also answers for draft ids, but without review_url/blocking_issues; /drafts has those.
  if (r.kind === "draft") return http<DraftRecord>(`/drafts/${encodeURIComponent(id)}`);
  return { ...r.contract, verification: r.verification } satisfies SignedContract;
}

export async function listContracts(): Promise<ContractRecord[]> {
  if (!USE_MOCKS) {
    // GET /contracts is a thin list; cards need the full terms, so each signed contract is fetched in full.
    const [drafts, items] = await Promise.all([http<DraftRecord[]>("/drafts"), http<ContractListItem[]>("/contracts")]);
    const signed = await Promise.all(items.filter((i) => i.kind === "contract").map((i) => getSignedContract(i.id)));
    // A signed draft is kept by the backend (signed_contract_id set); its contract represents it.
    return [...drafts.filter((d) => !d.signed_contract_id), ...signed];
  }
  await delay();
  return clone([...store.drafts.filter((d) => !d.signed_contract_id), ...store.contracts.map((c) => ({ ...c, verification: mockVerification }))]);
}

/** Draft ids go to GET /drafts/{id}; signed contracts come back with `.verification` attached. */
export async function getContract(id: string): Promise<ContractRecord> {
  if (!USE_MOCKS) {
    return id.startsWith("draft_") ? http<DraftRecord>(`/drafts/${encodeURIComponent(id)}`) : getSignedContract(id);
  }
  await delay();
  const record = findRecord(id);
  if (!record) throw new ApiError(`No contract or draft with id '${id}'.`, 404, "contract_not_found");
  return clone(record.status === "draft" ? record : { ...record, verification: mockVerification });
}

/** Turns the user's words into a DRAFT. It has no authority until the user signs it. */
export async function compileDraft(intent: string): Promise<DraftRecord> {
  if (!USE_MOCKS) return post<DraftRecord>("/drafts/compile", { intent });
  await delay(700);
  // Same rule as the backend's offline compiler: only the Pegasus 41 demo request compiles.
  if (!/pegasus\s*41/i.test(intent)) {
    throw new ApiError("The live compiler is not configured, and the offline demo compiler only understands the Pegasus 41 demo request.",
      422, "live_compiler_not_configured", { draft_created: false });
  }
  const id = newId("draft");
  const draft: DraftRecord = {
    ...clone(fixtures.drafts[0]), id, goal: "Nike Pegasus 41 running shoes", created_at: iso(),
    review_url: `/contracts/${id}`, compiler_source: "fixture", compiler_notes: ["fixture: produced by the mock compiler."],
  };
  store.drafts.unshift(draft);
  return clone(draft);
}

/** Signs a draft. Nothing is charged: payment happens per purchase, approved by the user in Link. */
export async function signContract(draftId: string): Promise<Contract> {
  if (!USE_MOCKS) return post<Contract>(`/contracts/${encodeURIComponent(draftId)}/sign`, { draft_id: draftId });
  await delay(500);
  const draft = store.drafts.find((d) => d.id === draftId);
  if (!draft) throw new ApiError(`No contract draft with id '${draftId}'.`, 404, "draft_not_found");
  if (draft.signed_contract_id) throw new ApiError("This draft was already signed.", 409, "already_signed");
  if (draft.blocking_issues.length) {
    throw new ApiError("This draft has problems that must be fixed before it can be signed.", 409, "draft_has_blocking_issues", { blocking_issues: draft.blocking_issues });
  }
  const { status: _s, assumptions: _a, clarifications_needed: _c, compiler_notes: _n, signed_contract_id: _i, review_url: _r, blocking_issues: _b, compiler_source: _cs, ...body } = draft;
  const contract: Contract = {
    ...body, id: newId("contract"), status: "active", agent_key: "agent_demo",
    signed_at: iso(), contract_hash: `sha256:${newId("h").slice(2)}`, signature: "hmac:demo",
    previous_contract_id: draft.previous_contract_id,
  };
  // Signing a new version revokes the one it was edited from.
  if (draft.previous_contract_id) {
    const prev = store.contracts.find((c) => c.id === draft.previous_contract_id);
    if (prev && prev.status === "active") prev.status = "revoked";
  }
  draft.signed_contract_id = contract.id;
  store.contracts.unshift(contract);
  pushEvent({ purchase_id: null, contract_id: contract.id, event_type: "contract_signed", message: "Contract signed", data: {} });
  return clone(contract);
}

export async function revokeContract(id: string): Promise<Contract> {
  if (!USE_MOCKS) return post<Contract>(`/contracts/${encodeURIComponent(id)}/revoke`);
  await delay();
  const c = store.contracts.find((x) => x.id === id);
  if (!c) throw new ApiError(`No contract with id '${id}'.`, 404, "contract_not_found");
  c.status = "revoked";
  pushEvent({ purchase_id: null, contract_id: id, event_type: "contract_revoked", message: "Contract revoked", data: {} });
  return clone(c);
}

/** POST /contracts/{id}/amend → a new draft pointing at the signed contract, which stays in force until the draft is signed. */
export async function amendContract(id: string): Promise<DraftRecord> {
  if (!USE_MOCKS) return post<DraftRecord>(`/contracts/${encodeURIComponent(id)}/amend`);
  await delay();
  const c = store.contracts.find((x) => x.id === id);
  if (!c) throw new ApiError(`No contract with id '${id}'.`, 404, "contract_not_found");
  const { status: _s, agent_key: _k, signed_at: _t, contract_hash: _h, signature: _g, ...body } = c;
  const draftId = newId("draft");
  const draft: DraftRecord = {
    ...clone(body), id: draftId, status: "draft", created_at: iso(), previous_contract_id: c.id,
    signed_contract_id: null, review_url: `/contracts/${draftId}`, blocking_issues: [],
    assumptions: [], clarifications_needed: [], compiler_notes: [`Amendment of contract ${c.id}.`],
  };
  store.drafts.unshift(draft);
  return clone(draft);
}

/** PATCH /drafts/{id}. Callers send only the keys that changed; edited values become source "user". */
export async function updateDraft(id: string, patch: DraftPatch): Promise<DraftRecord> {
  if (!USE_MOCKS) return http<DraftRecord>(`/drafts/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(patch) });
  await delay();
  const d = store.drafts.find((x) => x.id === id);
  if (!d) throw new ApiError(`No contract draft with id '${id}'.`, 404, "draft_not_found");
  if (d.signed_contract_id) throw new ApiError("This draft was already signed.", 409, "already_signed");
  if (patch.goal !== undefined) d.goal = patch.goal;
  if (patch.target !== undefined) { d.spend.target = patch.target; d.spend.target_source = "user"; }
  if (patch.hard_cap_all_in !== undefined) { d.spend.hard_cap_all_in = patch.hard_cap_all_in; d.spend.hard_cap_source = "user"; }
  if (d.delivery) {
    if (patch.max_shipping !== undefined) { d.delivery.max_shipping = patch.max_shipping; d.delivery.max_shipping_source = "user"; }
    if (patch.deliver_by !== undefined) { d.delivery.deliver_by = patch.deliver_by; d.delivery.deliver_by_source = "user"; }
  }
  if (patch.constraints) d.constraints = patch.constraints;
  return clone(d);
}

// ---------- Purchases ----------

/** GET /purchases/{id}. The backend runs the payment refresh first, so polling this moves a purchase along. */
export async function getPurchase(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return http<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}`);
  await delay();
  const p = mockPurchase(id);
  advanceMockPayment(p);
  return clone(p);
}

export async function refreshPayment(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return post<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}/payment/refresh`);
  await delay(150);
  const p = mockPurchase(id);
  advanceMockPayment(p);
  return clone(p);
}

export async function listPurchasesForContract(contractId: string): Promise<PurchaseDetail[]> {
  if (!USE_MOCKS) return http<PurchaseDetail[]>(`/contracts/${encodeURIComponent(contractId)}/purchases`);
  await delay(100);
  return clone(store.purchases.filter((p) => p.purchase.contract_id === contractId));
}

export async function listPurchases(): Promise<PurchaseDetail[]> {
  if (!USE_MOCKS) return http<PurchaseDetail[]>("/purchases");
  await delay(100);
  return clone(store.purchases);
}

/**
 * The user's payment approval for a purchase that passed every check.
 * - link_test: opens Link's approval page in a new tab; polling picks up the result.
 * - stub: "Simulated provider approval", which stands in for Link's approval tap.
 * The tab is opened before any await when health is cached, so pop-up blockers allow it.
 */
export async function approvePurchase(detail: PurchaseDetail): Promise<PurchaseDetail> {
  const health = cachedHealth() ?? (await getHealth());
  const id = detail.purchase.id;
  if (health.payment_mode === "link_test") {
    const url = detail.approval_url ?? detail.payment?.approval_url;
    // Only ever open a real web page; the URL comes from the payment provider via the backend.
    if (!url || !/^https?:\/\//i.test(url)) throw new ApiError("Link hasn't provided an approval page for this payment yet. Try again in a moment.", 0, "approval_url_missing");
    const tab = window.open(url, "_blank");
    if (!tab) throw new ApiError(`Your browser blocked the Link tab. Allow pop-ups for this site, or open ${url}`, 0, "popup_blocked");
    tab.opener = null;
    return detail;
  }
  if (!USE_MOCKS) return post<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}/payment/simulate-approval`);
  await delay(400);
  const p = mockPurchase(id);
  if (p.payment?.state !== "awaiting_approval") throw new ApiError(`The payment is ${p.payment?.state ?? "missing"}, not waiting for approval.`, 409, "payment_not_awaiting_approval");
  p.payment.state = "approved";
  pushEvent({ purchase_id: id, contract_id: p.purchase.contract_id, event_type: "purchase_authorized", message: "Simulated provider approval: you approved the (simulated) payment.", data: { kind: "simulated_provider_approval" } });
  return clone(fixtures.syncDetail(p));
}

/** "Not this one": POST /purchases/{id}/reject declines an authorized purchase whose payment hasn't started. */
export async function declinePurchase(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return post<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}/reject`, {});
  await delay(400);
  const p = mockPurchase(id);
  const pay = p.payment;
  if (p.purchase.status !== "authorized" || (pay && !["awaiting_approval", "approved", "credential_ready"].includes(pay.state))) {
    throw new ApiError("The payment is already under way and can no longer be declined.", 409, "payment_already_started");
  }
  if (pay) pay.state = "denied";
  Object.assign(p.purchase, { status: "blocked", error: "Declined by the user before payment.", completed_at: iso() });
  p.resolution = { action: "decline", resolved_at: iso(), accepted_constraints: [], note: null };
  // Declining frees the single-use contract so the agent can keep looking.
  const c = store.contracts.find((x) => x.id === p.purchase.contract_id);
  if (c?.status === "used") c.status = "active";
  pushEvent({ purchase_id: id, contract_id: p.purchase.contract_id, event_type: "purchase_blocked", message: "You declined this purchase before payment. The agent can keep looking under the same contract.", data: { kind: "purchase_declined", human_rejection: true } });
  return clone(fixtures.syncDetail(p));
}

/**
 * Step 1 of an escalation: accept (approve) or reject the checks Handshake couldn't verify.
 * Approving never turns UNVERIFIABLE into PASS, and never overrides a FAIL. Afterwards the
 * payment still needs the user's own approval in Link (step 2).
 */
export async function resolveEscalation(id: string, action: "approve" | "reject", note?: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return post<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}/${action}`, note ? { note } : {});
  await delay(600);
  const p = mockPurchase(id);
  if (p.purchase.status !== "escalated") throw new ApiError(`Purchase is ${p.purchase.status}; only an escalated purchase can be ${action === "approve" ? "approved" : "rejected"}.`, 409, "purchase_not_escalated");
  const contractId = p.purchase.contract_id;
  const accepted = p.decision?.results.filter((r) => r.verdict === "unverifiable" && r.severity !== "soft").map((r) => r.constraint) ?? [];
  p.resolution = { action, resolved_at: iso(), accepted_constraints: action === "approve" ? accepted : [], note: note ?? null };
  if (action === "approve") {
    const total = p.proposal?.total ?? 0;
    Object.assign(p.purchase, { status: "authorized", authorized_amount: total });
    const c = store.contracts.find((x) => x.id === contractId);
    if (c?.single_use) c.status = "used";
    p.payment = fixtures.mockPayment(total, "awaiting_approval", { updated_at: iso() });
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "purchase_authorized", message: "You accepted the exception. The payment still needs your approval.", data: { human_approval: true, accepted_constraints: accepted } });
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "purchase_authorized", message: "Payment requested from the simulated provider", data: { kind: "payment_requested" } });
  } else {
    Object.assign(p.purchase, { status: "blocked", error: "Rejected by the user after escalation.", completed_at: iso() });
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "purchase_blocked", message: "User rejected the escalated purchase.", data: { human_rejection: true } });
  }
  return clone(fixtures.syncDetail(p));
}

// ---------- Evidence ----------

export async function getEvidenceBundle(purchaseId: string): Promise<EvidenceBundle> {
  if (!USE_MOCKS) return http<EvidenceBundle>(`/evidence/${encodeURIComponent(purchaseId)}`);
  await delay(100);
  const p = mockPurchase(purchaseId);
  const contract = store.contracts.find((c) => c.id === p.purchase.contract_id);
  return clone({
    purchase: p.purchase, status: p.purchase.status, summary: p.summary, contract: contract ?? {},
    contract_verification: mockVerification, proposal: p.proposal, proposal_raw_payload: null, decision: p.decision,
    credential: p.credential, ledger_intact: true, events: store.evidence[purchaseId] ?? [],
  });
}

export async function getEvidence(purchaseId: string): Promise<EvidenceEvent[]> {
  return (await getEvidenceBundle(purchaseId)).events;
}

// ---------- Connecting an agent (OAuth device flow; the MCP login link lands on /connect) ----------

const MOCK_PERMISSIONS = {
  permissions: [
    "compile draft contracts for you to review",
    "read your contracts, purchases, and evidence",
    "request purchases under contracts you sign for this agent",
    "collect a single-use test card for a purchase you approved",
  ],
  never: ["sign contracts", "approve or reject purchases", "edit or revoke contracts", "approve payments"],
};
const mockDevices = new Map<string, string>();

export async function getDeviceAuthorization(userCode: string): Promise<DeviceAuthorization> {
  if (!USE_MOCKS) return http<DeviceAuthorization>(`/oauth/device?user_code=${encodeURIComponent(userCode)}`);
  await delay();
  if (/expired|unknown/i.test(userCode)) throw new ApiError("That code is unknown or has expired. Ask the agent for a new link.", 404, "unknown_code");
  return {
    user_code: userCode.toUpperCase(), client_id: "handshake-mcp", client_name: "Handshake MCP (demo agent)",
    status: mockDevices.get(userCode) ?? "pending", expires_at: new Date(Date.now() + 10 * 60_000).toISOString(), ...MOCK_PERMISSIONS,
  };
}

async function decideDevice(userCode: string, decision: "approve" | "deny"): Promise<DeviceDecision> {
  if (!USE_MOCKS) return post<DeviceDecision>(`/oauth/device/${decision}`, { user_code: userCode });
  await delay(400);
  if (mockDevices.has(userCode)) throw new ApiError(`This request was already ${mockDevices.get(userCode)}.`, 409, "already_decided");
  const status = decision === "approve" ? "approved" : "denied";
  mockDevices.set(userCode, status);
  return { user_code: userCode.toUpperCase(), status, client_id: "handshake-mcp" };
}

export const approveDevice = (userCode: string) => decideDevice(userCode, "approve");
export const denyDevice = (userCode: string) => decideDevice(userCode, "deny");
