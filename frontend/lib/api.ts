// Every backend call goes through this file. docs/API.md is the contract; this file adapts to it.
// NEXT_PUBLIC_USE_MOCKS=false + NEXT_PUBLIC_API_URL (see .env.example) switches to the FastAPI backend.
import * as fixtures from "./mock-data";
import { clearSession, readSession } from "./session";
import type {
  Agent, ApiErrorBody, AuthConfig, Contract, ContractListItem, ContractRecord, ContractResponse, CreatedAgentKey,
  DeviceAuthorization, DeviceDecision, DraftPatch, DraftRecord, EvidenceBundle, EvidenceEvent, Funding, Health, LinkLogin,
  LinkStatus, OAuthAuthorizationRequest, OAuthDecision, PaymentState, PurchaseDetail, SignedContract, UserLogin,
} from "./types";

export const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS !== "false";
// No fallback host on purpose: a missing setting should fail loudly, not quietly call some default address.
const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/+$/, "");

/**
 * The public MCP server URL (e.g. the Fly deployment's /mcp), for the "Connect Muse" help on /agents.
 * Optional: when unset the help box is hidden rather than showing a guessed address.
 */
export const MCP_URL: string | null = (process.env.NEXT_PUBLIC_MCP_URL ?? "").trim() || null;

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
    // A 401 from a login call is a failed sign-in (bad Google token), not an expired session.
    if (res.status === 401 && !path.startsWith("/auth/")) { clearSession(); redirectToLogin(); }
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
  fundings: clone(fixtures.fundings),
  agents: clone(fixtures.agents),
};
// Whether the mock user's own Link account is connected. Read lazily: the knobs live in localStorage.
let mockLinkConnected: boolean | null = null;
const mockLinked = () => (mockLinkConnected ??= fixtures.mockSettings().link_connected);
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

const NOT_FUNDED: Funding = { state: "not_funded", card_stored: false };
const mockFundingOf = (contractId: string): Funding => store.fundings[contractId] ?? NOT_FUNDED;

/** A purchase as the backend returns it: with its contract's current funding. */
const withFunding = (d: PurchaseDetail): PurchaseDetail => clone({ ...fixtures.syncDetail(d), funding: mockFundingOf(d.purchase.contract_id) });
const signedWithExtras = (c: Contract): SignedContract => ({ ...c, verification: mockVerification, funding: mockFundingOf(c.id) });

function pushEvent(e: Omit<EvidenceEvent, "id" | "timestamp">) {
  const key = e.purchase_id ?? `contract:${e.contract_id}`;
  (store.evidence[key] ??= []).push({ ...e, id: newId("event"), timestamp: iso() });
}

/**
 * Mock stand-in for the agent collecting the unlocked card and paying, one step per poll, so the
 * progression is visible. It waits a few polls at credential_ready so "Not this one" can be tried.
 */
const mockTicks = new Map<string, number>();
const MOCK_NEXT: Partial<Record<PaymentState, PaymentState>> = { credential_ready: "paying", paying: "paid", paid: "completed" };
function advanceMockPayment(d: PurchaseDetail) {
  const pay = d.payment;
  const next = pay && d.purchase.status === "authorized" ? MOCK_NEXT[pay.state] : undefined;
  if (!pay || !next) return;
  const { id, contract_id } = d.purchase;
  const ticks = (mockTicks.get(id) ?? 0) + 1;
  mockTicks.set(id, ticks);
  if (pay.state === "credential_ready" && ticks < 4) return;
  const total = pay.amount;
  const funding = store.fundings[contract_id];
  pay.state = next;
  pay.updated_at = iso();
  if (next === "paying") {
    pay.credential_released = true;
    pay.last4 = funding?.card_last4 ?? "4242";
    if (funding) Object.assign(funding, { state: "released", card_stored: false, released_purchase_id: id });
    pushEvent({ purchase_id: id, contract_id, event_type: "validation_completed", message: "Checkout rechecked before release: unchanged", data: { kind: "checkout_revalidated" } });
    pushEvent({ purchase_id: id, contract_id, event_type: "credential_created", message: `Card ending ${pay.last4} released once to the agent; stored copy wiped`, data: { kind: "credential_released" } });
  }
  if (next === "paid") {
    pushEvent({ purchase_id: id, contract_id, event_type: "credential_used", message: `Paid $${total.toFixed(2)} with the single-use card`, data: { kind: "payment_submitted" } });
  }
  if (next === "completed") {
    pay.order_id = `order_${newId("demo").slice(5, 11)}`;
    if (funding) funding.state = "used";
    Object.assign(d.purchase, { status: "completed", charged_amount: total, completed_at: iso() });
    d.summary = `Completed: charged $${total.toFixed(2)} at ${d.purchase.merchant_name}.`;
    pushEvent({ purchase_id: id, contract_id, event_type: "payment_completed", message: "Merchant order verified against the receipt", data: { kind: "receipt_verified" } });
  }
}

/** Only ever open a real web page in a new tab; the URL comes from Link via the backend. */
function openExternal(url: string | null | undefined, what: string) {
  if (!url || !/^https?:\/\//i.test(url)) throw new ApiError(`Link hasn't provided ${what} yet. Try again in a moment.`, 0, "approval_url_missing");
  const tab = window.open(url, "_blank");
  if (!tab) throw new ApiError(`Your browser blocked the Link tab. Allow pop-ups for this site, or open ${url}`, 0, "popup_blocked");
  tab.opener = null;
}

// ---------- Auth ----------

/** GET /auth/config (public): whether Google sign-in and/or demo login are offered. Decides the login page. */
export async function getAuthConfig(): Promise<AuthConfig> {
  if (!USE_MOCKS) return http<AuthConfig>("/auth/config");
  await delay(100);
  const { google_client_id, demo_login } = fixtures.mockSettings();
  return { google_client_id, demo_login };
}

const mockLogin = (email: string, demo: boolean): UserLogin => ({
  token: "mock-token", token_type: "bearer", email, role: "user", expires_at: Math.floor(Date.now() / 1000) + 86_400, demo_auth: demo,
});

/** DEMO AUTH: exchanges an email for a user token. No password. Off in production (404 demo_login_disabled). */
export async function demoLogin(email: string): Promise<UserLogin> {
  if (!USE_MOCKS) return post<UserLogin>("/auth/demo-login", { email });
  await delay(300);
  if (!fixtures.mockSettings().demo_login) throw new ApiError("Demo login is turned off on this server.", 404, "demo_login_disabled");
  return mockLogin(email.trim().toLowerCase(), true);
}

/**
 * POST /auth/google: trades the ID token Google Identity Services handed the page for a Handshake user
 * token. The backend verifies the token's signature, audience and expiry; the page never inspects it.
 */
export async function googleLogin(credential: string): Promise<UserLogin> {
  if (!USE_MOCKS) return post<UserLogin>("/auth/google", { credential });
  await delay(300);
  if (!fixtures.mockSettings().google_client_id) throw new ApiError("Google sign-in isn't configured on this server.", 404, "google_login_disabled");
  if (!credential) throw new ApiError("Google didn't return a valid sign-in. Try again.", 401, "google_token_invalid");
  return mockLogin("google-user@example.com", false);
}

// ---------- Health (cached: the payment mode doesn't change while the backend runs) ----------

let healthValue: Health | null = null;
let healthPromise: Promise<Health> | null = null;

function mockHealth(): Health {
  const mode = fixtures.mockSettings().payment_mode;
  return { ...fixtures.health, payment_mode: mode, payment_label: fixtures.PAYMENT_LABELS[mode] };
}

export function getHealth(): Promise<Health> {
  if (USE_MOCKS) return Promise.resolve(mockHealth());
  healthPromise ??= http<Health>("/health").then((h) => (healthValue = h), (e) => { healthPromise = null; throw e; });
  return healthPromise;
}

/** The cached health, if already fetched. Lets approveFunding open Link's tab synchronously. */
export function cachedHealth(): Health | null {
  return USE_MOCKS ? mockHealth() : healthValue;
}

// ---------- Drafts and contracts ----------

async function getSignedContract(id: string): Promise<ContractRecord> {
  const r = await http<ContractResponse>(`/contracts/${encodeURIComponent(id)}`);
  // GET /contracts/{id} also answers for draft ids, but without review_url/blocking_issues; /drafts has those.
  if (r.kind === "draft") return http<DraftRecord>(`/drafts/${encodeURIComponent(id)}`);
  return { ...r.contract, verification: r.verification, funding: r.funding } satisfies SignedContract;
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
  return clone([...store.drafts.filter((d) => !d.signed_contract_id), ...store.contracts.map(signedWithExtras)]);
}

/** Draft ids go to GET /drafts/{id}; signed contracts come back with `.verification` attached. */
export async function getContract(id: string): Promise<ContractRecord> {
  if (!USE_MOCKS) {
    return id.startsWith("draft_") ? http<DraftRecord>(`/drafts/${encodeURIComponent(id)}`) : getSignedContract(id);
  }
  await delay();
  const record = findRecord(id);
  if (!record) throw new ApiError(`No contract or draft with id '${id}'.`, 404, "contract_not_found");
  return clone(record.status === "draft" ? record : signedWithExtras(record as Contract));
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

/**
 * Signs a draft, which also starts FUNDING: the backend asks the user's Link account for one
 * single-use test card for the all-in hard cap. In link_test mode this is refused with
 * 409 link_not_connected until the user connects their own Link account.
 */
export async function signContract(draftId: string): Promise<Contract> {
  if (!USE_MOCKS) return post<Contract>(`/contracts/${encodeURIComponent(draftId)}/sign`, { draft_id: draftId });
  await delay(500);
  const draft = store.drafts.find((d) => d.id === draftId);
  if (!draft) throw new ApiError(`No contract draft with id '${draftId}'.`, 404, "draft_not_found");
  if (draft.signed_contract_id) throw new ApiError("This draft was already signed.", 409, "already_signed");
  if (draft.blocking_issues.length) {
    throw new ApiError("This draft has problems that must be fixed before it can be signed.", 409, "draft_has_blocking_issues", { blocking_issues: draft.blocking_issues });
  }
  const provider = mockFundingProvider();
  const { status: _s, assumptions: _a, clarifications_needed: _c, compiler_notes: _n, signed_contract_id: _i, review_url: _r, blocking_issues: _b, compiler_source: _cs, proposed_by_agent: proposer, ...body } = draft;
  const contract: Contract = {
    // Like the backend: the contract is bound to the agent that drafted it, else the default agent.
    ...body, id: newId("contract"), status: "active", agent_key: proposer ?? "agent_demo",
    signed_at: iso(), contract_hash: `sha256:${newId("h").slice(2)}`, signature: "hmac:demo",
    previous_contract_id: draft.previous_contract_id,
  };
  // Signing a new version revokes the one it was edited from.
  if (draft.previous_contract_id) {
    const prev = store.contracts.find((c) => c.id === draft.previous_contract_id);
    if (prev && prev.status === "active") { prev.status = "revoked"; cancelMockFunding(prev.id); }
  }
  draft.signed_contract_id = contract.id;
  store.contracts.unshift(contract);
  store.fundings[contract.id] = mockPendingFunding(contract, provider);
  pushEvent({ purchase_id: null, contract_id: contract.id, event_type: "contract_signed", message: "Contract signed", data: {} });
  pushEvent({ purchase_id: null, contract_id: contract.id, event_type: "contract_signed", message: fundingRequestedMessage(provider), data: { kind: "funding_requested" } });
  return clone(contract);
}

export async function revokeContract(id: string): Promise<Contract> {
  if (!USE_MOCKS) return post<Contract>(`/contracts/${encodeURIComponent(id)}/revoke`);
  await delay();
  const c = store.contracts.find((x) => x.id === id);
  if (!c) throw new ApiError(`No contract with id '${id}'.`, 404, "contract_not_found");
  c.status = "revoked";
  cancelMockFunding(id);
  pushEvent({ purchase_id: null, contract_id: id, event_type: "contract_revoked", message: "Contract revoked", data: {} });
  return clone(c);
}

/**
 * Which provider a new mock funding uses, mirroring the backend: stub always simulates; link_test needs the
 * user's Link (409 link_not_connected otherwise); link_optional uses Link only if the user connected it.
 */
function mockFundingProvider(): "stub" | "link_test" {
  const mode = fixtures.mockSettings().payment_mode;
  if (mode === "stub") return "stub";
  if (mode === "link_test" && !mockLinked()) {
    throw new ApiError("Connect your Stripe Link account before signing: signing asks it for the contract's card.", 409, "link_not_connected");
  }
  return mockLinked() ? "link_test" : "stub";
}

/** A pending mock funding. A Link one "approves" in a stand-in tab of this app (there is no Link in mock mode). */
function mockPendingFunding(c: Contract, provider: "stub" | "link_test"): Funding {
  const f = fixtures.mockFunding(c.id, c.spend.hard_cap_all_in, "awaiting_approval");
  if (provider === "stub") return f;
  return { ...f, provider, provider_label: fixtures.PAYMENT_LABELS.link_test, approval_url: `${window.location.origin}/contracts/${c.id}` };
}

const fundingRequestedMessage = (provider: "stub" | "link_test") => provider === "stub"
  ? "Asked the simulated provider for a single-use card for the all-in cap"
  : "Asked your Stripe Link account (test mode) for a single-use card for the all-in cap";

/** Revoking cancels a pending funding request and wipes a stored card. */
function cancelMockFunding(contractId: string) {
  const f = store.fundings[contractId];
  if (f && (f.state === "awaiting_approval" || f.state === "funded")) Object.assign(f, { state: "canceled", card_stored: false });
}

// ---------- Contract funding (signing = funding) ----------

/** GET /contracts/{id}/funding. The backend polls Link and stores the card once it's approved. */
export async function getFunding(contractId: string): Promise<Funding> {
  if (!USE_MOCKS) return http<Funding>(`/contracts/${encodeURIComponent(contractId)}/funding`);
  await delay(150);
  return clone(mockFundingOf(contractId));
}

/** POST /contracts/{id}/funding: fund again after a denied, expired, failed, or used card. */
export async function restartFunding(contractId: string): Promise<Funding> {
  if (!USE_MOCKS) return post<Funding>(`/contracts/${encodeURIComponent(contractId)}/funding`);
  await delay(400);
  const c = store.contracts.find((x) => x.id === contractId);
  if (!c || c.status !== "active") throw new ApiError(`Only an active contract can be funded; this one is ${c?.status ?? "missing"}.`, 409, "contract_not_active");
  const provider = mockFundingProvider();
  store.fundings[contractId] = mockPendingFunding(c, provider);
  pushEvent({ purchase_id: null, contract_id: contractId, event_type: "contract_signed", message: fundingRequestedMessage(provider), data: { kind: "funding_requested" } });
  return clone(store.fundings[contractId]);
}

/**
 * The user's approval of the contract's funding card, decided by the card's own provider (not the backend's
 * payment mode: in link_optional one user's cards are simulated and another's are real Link test cards).
 * - link_test: opens Link's approval page (funding.approval_url) in a new tab; polling getFunding picks it up.
 * - stub: POST /contracts/{id}/funding/simulate-approval, "Simulated provider approval".
 * The tab opens before any await, so pop-up blockers allow it.
 */
export async function approveFunding(contractId: string, funding: Funding): Promise<Funding> {
  if (fundingUsesLink(funding)) {
    openExternal(funding.approval_url, "an approval page for this card");
    // Mock mode has no Link: the stand-in approval lands a moment after the tab opens.
    if (USE_MOCKS) setTimeout(() => { try { mockApproveFunding(contractId, null); } catch { /* revoked meanwhile */ } }, 1500);
    return funding;
  }
  if (!USE_MOCKS) return post<Funding>(`/contracts/${encodeURIComponent(contractId)}/funding/simulate-approval`);
  await delay(400);
  return mockApproveFunding(contractId, "Simulated provider approval: you approved the (simulated) funding.");
}

/**
 * Whether a funding card is approved in Stripe Link (vs. the simulated provider). Older responses without
 * `provider` fall back to the backend's mode, which is only unambiguous outside link_optional.
 */
export function fundingUsesLink(funding: Funding | null | undefined): boolean {
  if (funding?.provider) return funding.provider === "link_test";
  return cachedHealth()?.payment_mode === "link_test";
}

function mockApproveFunding(contractId: string, message: string | null): Funding {
  const f = store.fundings[contractId];
  if (f?.state !== "awaiting_approval") throw new ApiError(`The funding is ${f?.state ?? "not_funded"}, not waiting for approval.`, 409, "funding_not_awaiting_approval");
  Object.assign(f, { state: "funded", card_stored: true, card_last4: "4242", valid_until: new Date(Date.now() + 12 * 3_600_000).toISOString() });
  // Link approvals happen in Link, so (like the backend) only the stored card is recorded for them.
  if (message) pushEvent({ purchase_id: null, contract_id: contractId, event_type: "contract_signed", message, data: { kind: "simulated_provider_approval" } });
  pushEvent({ purchase_id: null, contract_id: contractId, event_type: "contract_signed", message: "Card ending 4242 stored encrypted on the contract, locked", data: { kind: "card_stored" } });
  return clone(f);
}

// ---------- The user's own Stripe Link account ----------

/** POST /link/connect: returns Link's login link and phrase right away (stub mode: simulated, connected). */
export async function connectLink(): Promise<LinkLogin> {
  if (!USE_MOCKS) return post<LinkLogin>("/link/connect");
  await delay(300);
  if (fixtures.mockSettings().payment_mode === "stub") return { state: "connected", simulated: true, provider_label: "Simulated provider" };
  // Mock mode has no Link app to approve in, so the connection lands at once.
  mockLinkConnected = true;
  return { state: "connected", simulated: false, provider_label: fixtures.PAYMENT_LABELS.link_test };
}

function mockLinkStatus(): LinkStatus {
  if (fixtures.mockSettings().payment_mode === "stub") return { connected: true, simulated: true, provider_label: "Simulated provider", login: null };
  return { connected: mockLinked(), simulated: false, provider_label: fixtures.PAYMENT_LABELS.link_test, login: null };
}

export async function getLinkStatus(): Promise<LinkStatus> {
  if (!USE_MOCKS) return http<LinkStatus>("/link/status");
  await delay(100);
  return mockLinkStatus();
}

export async function disconnectLink(): Promise<LinkStatus> {
  if (!USE_MOCKS) return post<LinkStatus>("/link/disconnect");
  await delay(200);
  if (fixtures.mockSettings().payment_mode !== "stub") mockLinkConnected = false;
  return mockLinkStatus();
}

/** Opens Link's login page for connecting the account (from POST /link/connect). Not a backend call. */
export function openLinkLogin(login: LinkLogin) {
  openExternal(login.verification_url, "a login link");
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
  return withFunding(p);
}

export async function refreshPayment(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return post<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}/payment/refresh`);
  await delay(150);
  const p = mockPurchase(id);
  advanceMockPayment(p);
  return withFunding(p);
}

export async function listPurchasesForContract(contractId: string): Promise<PurchaseDetail[]> {
  if (!USE_MOCKS) return http<PurchaseDetail[]>(`/contracts/${encodeURIComponent(contractId)}/purchases`);
  await delay(100);
  return store.purchases.filter((p) => p.purchase.contract_id === contractId).map(withFunding);
}

export async function listPurchases(): Promise<PurchaseDetail[]> {
  if (!USE_MOCKS) return http<PurchaseDetail[]>("/purchases");
  await delay(100);
  return store.purchases.map(withFunding);
}

/** "Not this one": POST /purchases/{id}/reject declines an authorized purchase whose card hasn't been released. The card stays locked on the contract. */
export async function declinePurchase(id: string): Promise<PurchaseDetail> {
  if (!USE_MOCKS) return post<PurchaseDetail>(`/purchases/${encodeURIComponent(id)}/reject`, {});
  await delay(400);
  const p = mockPurchase(id);
  const pay = p.payment;
  if (p.purchase.status !== "authorized" || (pay && (pay.state !== "credential_ready" || pay.credential_released))) {
    throw new ApiError("The card was already released for this purchase; it can no longer be declined.", 409, "payment_already_started");
  }
  if (pay) pay.state = "denied";
  Object.assign(p.purchase, { status: "blocked", error: "Declined by the user before payment.", completed_at: iso() });
  p.resolution = { action: "decline", resolved_at: iso(), accepted_constraints: [], note: null };
  // Declining frees the single-use contract so the agent can keep looking.
  const c = store.contracts.find((x) => x.id === p.purchase.contract_id);
  if (c?.status === "used") c.status = "active";
  pushEvent({ purchase_id: id, contract_id: p.purchase.contract_id, event_type: "purchase_blocked", message: "You declined this purchase before payment. The card stays locked on the contract; the agent can keep looking.", data: { kind: "purchase_declined", human_rejection: true } });
  return withFunding(p);
}

/**
 * Step 1 of an escalation: accept (approve) or reject the checks Handshake couldn't verify.
 * Approving never turns UNVERIFIABLE into PASS, and never overrides a FAIL. Because funding
 * happened at signing, accepting unlocks the contract's stored card for this checkout (step 2).
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
    if (mockFundingOf(contractId).state !== "funded") throw new ApiError("This contract has no funded card. Fund it, then try again.", 409, "contract_not_funded");
    const total = p.proposal?.total ?? 0;
    Object.assign(p.purchase, { status: "authorized", authorized_amount: total });
    const c = store.contracts.find((x) => x.id === contractId);
    if (c?.single_use) c.status = "used";
    p.payment = fixtures.mockPayment(total, "credential_ready", { updated_at: iso() });
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "purchase_authorized", message: "You accepted the exception for this checkout.", data: { human_approval: true, accepted_constraints: accepted } });
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "purchase_authorized", message: "The contract's card is unlocked for this checkout only", data: { kind: "credential_ready" } });
  } else {
    Object.assign(p.purchase, { status: "blocked", error: "Rejected by the user after escalation.", completed_at: iso() });
    pushEvent({ purchase_id: id, contract_id: contractId, event_type: "purchase_blocked", message: "User rejected the escalated purchase.", data: { human_rejection: true } });
  }
  return withFunding(p);
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
    credential: p.credential, ledger_intact: true,
    // Like the backend, the story includes the contract's signing and funding events.
    events: [...(store.evidence[`contract:${p.purchase.contract_id}`] ?? []), ...(store.evidence[purchaseId] ?? [])]
      .sort((a, b) => a.timestamp.localeCompare(b.timestamp)),
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

// ---------- OAuth 2.1 authorization code flow (Muse and other MCP clients land on /authorize) ----------

// Mock decisions per request id, so a decided request can't be decided twice (like the backend).
const mockAuthorizations = new Map<string, boolean>();

/** GET /oauth/authorize/request: the pending request the backend's /oauth/authorize redirected here with. */
export async function getAuthorizationRequest(requestId: string): Promise<OAuthAuthorizationRequest> {
  if (!USE_MOCKS) return http<OAuthAuthorizationRequest>(`/oauth/authorize/request?request_id=${encodeURIComponent(requestId)}`);
  await delay();
  // Mock: ids containing "expired" / "unknown" show the two error screens.
  if (/expired/i.test(requestId)) throw new ApiError("This sign-in request expired. Start connecting again from your agent.", 410, "authorization_request_expired");
  if (/unknown/i.test(requestId) || mockAuthorizations.has(requestId)) {
    throw new ApiError("This sign-in request wasn't found. It may have been used already.", 404, "authorization_request_not_found");
  }
  return {
    request_id: requestId, client_name: "Muse (mock)", client_id: "client_mock_muse", redirect_host: window.location.host,
    scopes: ["handshake.agent"], expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  };
}

/**
 * POST /oauth/authorize/decision (user only). Returns where to send the browser: the client's registered
 * redirect_uri with a code (approve) or error=access_denied (deny). The caller checks it's http(s).
 */
export async function decideAuthorization(requestId: string, approve: boolean): Promise<OAuthDecision> {
  if (!USE_MOCKS) return post<OAuthDecision>("/oauth/authorize/decision", { request_id: requestId, approve });
  await delay(400);
  if (mockAuthorizations.has(requestId)) throw new ApiError("This sign-in request was already decided.", 404, "authorization_request_not_found");
  mockAuthorizations.set(requestId, approve);
  if (approve) {
    store.agents.unshift({
      agent_id: newId("agt"), bound_agent_id: "hsc_muse", client_id: "hsc_muse", name: "Muse (mock)", kind: "oauth", client_name: "Muse (mock)", created_at: iso(),
      last_used_at: null, expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString(), revoked_at: null,
    });
  }
  // Mock: there is no real client, so "the client's redirect" is this app's Agents page.
  const back = new URL("/agents", window.location.origin);
  back.searchParams.set(approve ? "code" : "error", approve ? "mock-code" : "access_denied");
  return { redirect_to: back.href };
}

// ---------- Agents and agent keys (user only) ----------

export async function listAgents(): Promise<Agent[]> {
  if (!USE_MOCKS) return (await http<{ agents: Agent[] }>("/agents")).agents;
  await delay(150);
  return clone(store.agents);
}

/** POST /agents/keys. The token in the answer is shown once and never stored (not even in localStorage). */
export async function createAgentKey(name: string): Promise<CreatedAgentKey> {
  if (!USE_MOCKS) return post<CreatedAgentKey>("/agents/keys", { name });
  await delay(400);
  const agent: Agent = {
    agent_id: newId("agt"), bound_agent_id: newId("key"), client_id: null, name, kind: "key", client_name: null, created_at: iso(), last_used_at: null,
    expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString(), revoked_at: null,
  };
  store.agents.unshift(agent);
  // Obviously fake, like every fake secret in this repo.
  return { agent_id: agent.agent_id, name, token: `hs_agent_FAKE-TOK-${Math.random().toString(36).slice(2, 14)}`, expires_at: agent.expires_at };
}

export async function revokeAgent(agentId: string): Promise<Agent> {
  if (!USE_MOCKS) return post<Agent>(`/agents/${encodeURIComponent(agentId)}/revoke`);
  await delay(300);
  const a = store.agents.find((x) => x.agent_id === agentId);
  if (!a) throw new ApiError(`No agent with id '${agentId}'.`, 404, "agent_not_found");
  a.revoked_at ??= iso();
  return clone(a);
}
