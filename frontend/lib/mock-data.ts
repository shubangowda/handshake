// Demo fixtures for NEXT_PUBLIC_USE_MOCKS=true. Shapes match docs/API.md so UI work runs with no backend.
// Times are relative to page load so "expires tomorrow" stays true.
import type {
  Agent, Constraint, Contract, DraftRecord, EvidenceEvent, Funding, FundingState, Health, NextAction, PaymentInfo, PaymentMode,
  PaymentState, Purchase, PurchaseDetail, TransactionProposal, ValidationDecision,
} from "./types";

const now = Date.now();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const at = (offset: number) => new Date(now + offset).toISOString();

function nextMonday(): string {
  const d = new Date(now);
  d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
  d.setHours(20, 0, 0, 0);
  return d.toISOString();
}

/** What GET /health would say. The header shows "Mock data" instead of the payment label in mock mode. */
export const health: Health = {
  status: "ok", database: "mock", payment_mode: "stub", payment_label: "Simulated provider",
  credential_mode: "agent_visible", compiler_mode: "fixture",
};

export const PAYMENT_LABELS: Record<PaymentMode, string> = {
  stub: "Simulated provider",
  link_test: "Stripe Link (test mode)",
  link_optional: "Simulated provider, or your Stripe Link (TEST MODE)",
};

/**
 * Mock-mode knobs, so backend configurations can be tried without a backend (and set by browser tests).
 * Set them in the browser console, then reload:
 *   localStorage.setItem("handshake.mock", JSON.stringify({ payment_mode: "link_optional", google_client_id: "…" }))
 * Unset keys keep today's defaults: stub payments, demo login on, no Google sign-in, Link not connected.
 * Only ever read in mock mode; the real app gets all of this from the backend.
 */
export interface MockSettings {
  payment_mode: PaymentMode;
  /** Non-null shows "Sign in with Google" (the real Google script loads; tests stub it). */
  google_client_id: string | null;
  demo_login: boolean;
  /** Whether the user's own Stripe Link account starts out connected (link_test / link_optional). */
  link_connected: boolean;
}

const MOCK_DEFAULTS: MockSettings = { payment_mode: "stub", google_client_id: null, demo_login: true, link_connected: false };

export function mockSettings(): MockSettings {
  try {
    const raw = typeof window === "undefined" ? null : localStorage.getItem("handshake.mock");
    return { ...MOCK_DEFAULTS, ...(raw ? (JSON.parse(raw) as Partial<MockSettings>) : {}) };
  } catch {
    return MOCK_DEFAULTS;
  }
}

/** The one request the offline (fixture) compiler understands, same as the backend's. */
export const DEMO_INTENT = "Buy me Nike Pegasus 41 running shoes, size 10, new. Around $120, but no more than $135 all-in including tax and shipping. Delivered within 3 days. From Amazon.com. No subscriptions, memberships, or add-ons.";

const shoeConstraints: Constraint[] = [
  { field: "category", operator: "eq", value: "running shoes", severity: "hard", source: "user" },
  { field: "size", operator: "eq", value: "10", severity: "hard", source: "user" },
  { field: "condition", operator: "eq", value: "new", severity: "hard", source: "inferred",
    description: "You didn't say — Handshake assumed new, not used or refurbished." },
  { field: "brand", operator: "in", value: ["Nike", "Brooks", "Hoka"], severity: "soft", source: "user" },
  { field: "color", operator: "neq", value: "white", severity: "soft", source: "user" },
];

const shoeBase = {
  hil_version: "0.2",
  goal: "Running shoes",
  category: "footwear",
  spend: {
    currency: "USD", target: 120, hard_cap_all_in: 135,
    includes: ["item", "tax", "shipping", "fees"] as ("item" | "tax" | "shipping" | "fees")[],
    target_source: "user" as const, hard_cap_source: "inferred" as const,
  },
  delivery: {
    deliver_by: nextMonday(), max_shipping: 8, require_verified_estimate: false,
    deliver_by_source: "user" as const, max_shipping_source: "default" as const,
  },
  terms: { no_subscription: true, no_membership: true, no_addons: true, min_return_days: 30, no_forced_account_creation: false },
  merchants: { allow: [], deny: [], seller_requirement: "first_party_or_verified" as const, new_merchant: "escalate" as const },
  substitution: { allowed: false, same_model_other_color: false, same_brand: false, max_price_delta: null },
  data_sharing: { allowed_fields: ["name", "shipping_address"] as ("name" | "shipping_address")[], merchant_may_contact: false },
  instrument: { type: "virtual_single_use" as const, fixed_by_contract: true },
  constraints: shoeConstraints,
  escalate_if: ["any_unverifiable_hard"],
  selection_disclosure_required: true,
  single_use: true,
  revocable: true,
};

export const drafts: DraftRecord[] = [
  {
    ...shoeBase,
    id: "draft_shoes01",
    status: "draft",
    expires_at: at(DAY),
    created_at: at(-2 * MIN),
    previous_contract_id: null,
    signed_contract_id: null,
    review_url: "/contracts/draft_shoes01",
    blocking_issues: [],
    // Drafted by the mock Muse agent (see `agents` below), so signing shows who may use it.
    proposed_by_agent: "hsc_muse",
    assumptions: [
      "You said \"around $120\". Handshake set a $135 all-in maximum to leave room for tax and shipping.",
      "Shipping capped at $8 — Handshake's default for footwear.",
      "Condition set to new. Used and refurbished listings will be blocked.",
    ],
    clarifications_needed: [],
    compiler_notes: [],
  },
];

function signed(body: Omit<Contract, "hil_version" | "agent_key" | "contract_hash" | "signature" | "previous_contract_id" | "escalate_if" | "selection_disclosure_required" | "revocable" | "data_sharing" | "substitution" | "instrument"> & Partial<Contract>): Contract {
  return {
    hil_version: "0.2",
    agent_key: "agent_demo",
    contract_hash: "sha256:" + body.id.replace(/\W/g, "").padEnd(16, "0").slice(0, 16),
    signature: "hmac:demo",
    previous_contract_id: null,
    escalate_if: ["any_unverifiable_hard"],
    selection_disclosure_required: true,
    revocable: true,
    data_sharing: { allowed_fields: ["name", "shipping_address"], merchant_may_contact: false },
    substitution: { allowed: false, same_model_other_color: false, same_brand: false, max_price_delta: null },
    instrument: { type: "virtual_single_use", fixed_by_contract: true },
    ...body,
  };
}

export const contracts: Contract[] = [
  signed({ ...shoeBase, id: "contract_shoes", status: "used",
    created_at: at(-20 * MIN), signed_at: at(-18 * MIN), expires_at: at(DAY) }),
  signed({ ...shoeBase, id: "contract_shoes_b", status: "active", goal: "Running shoes — marathon pair",
    created_at: at(-40 * MIN), signed_at: at(-38 * MIN), expires_at: at(DAY) }),
  signed({ ...shoeBase, id: "contract_shoes_c", status: "active", goal: "Running shoes — backup pair",
    merchants: { ...shoeBase.merchants },
    created_at: at(-60 * MIN), signed_at: at(-58 * MIN), expires_at: at(DAY) }),
  // Signed but its funding card is still waiting for approval: the dashboard's "Pending" group.
  signed({ ...shoeBase, id: "contract_shoes_d", status: "active", goal: "Running shoes — race-day pair",
    created_at: at(-4 * MIN), signed_at: at(-3 * MIN), expires_at: at(DAY) }),
  // Its funded card expired unused, so the contract page offers "Fund again".
  signed({ ...shoeBase, id: "contract_shoes_e", status: "active", goal: "Running shoes — spare pair",
    created_at: at(-DAY), signed_at: at(-DAY + MIN), expires_at: at(2 * DAY) }),
  // "used" while its authorized purchase holds it: the backend reserves a single-use contract at
  // authorization, and frees it again if that purchase is declined.
  signed({ ...shoeBase, id: "contract_trail", status: "used", goal: "Trail running shoes",
    spend: { ...shoeBase.spend, target: 130, hard_cap_all_in: 145, hard_cap_source: "user" },
    constraints: [
      { field: "category", operator: "eq", value: "trail running shoes", severity: "hard", source: "user" },
      { field: "size", operator: "eq", value: "10", severity: "hard", source: "user" },
      { field: "condition", operator: "eq", value: "new", severity: "hard", source: "inferred" },
    ],
    created_at: at(-30 * MIN), signed_at: at(-29 * MIN), expires_at: at(DAY) }),
  signed({
    ...shoeBase, id: "contract_headphones", status: "used", goal: "Noise-cancelling headphones", category: "electronics",
    spend: { ...shoeBase.spend, target: 280, hard_cap_all_in: 320, hard_cap_source: "user" },
    delivery: null,
    constraints: [
      { field: "category", operator: "eq", value: "over-ear headphones", severity: "hard", source: "user" },
      { field: "brand", operator: "in", value: ["Sony", "Bose"], severity: "hard", source: "user" },
      { field: "condition", operator: "eq", value: "new", severity: "hard", source: "inferred" },
    ],
    created_at: at(-3 * DAY), signed_at: at(-3 * DAY + 2 * MIN), expires_at: at(-2 * DAY),
  }),
  signed({
    ...shoeBase, id: "contract_tickets", status: "revoked", goal: "2 tickets — Warriors vs. Lakers", category: "tickets",
    spend: { ...shoeBase.spend, target: 300, hard_cap_all_in: 400, hard_cap_source: "user" },
    constraints: [
      { field: "ticket_quantity", operator: "eq", value: 2, severity: "hard", source: "user" },
      { field: "seats_together", operator: "eq", value: true, severity: "hard", source: "user" },
    ],
    created_at: at(-5 * DAY), signed_at: at(-5 * DAY + MIN), expires_at: at(2 * DAY),
  }),
  signed({
    ...shoeBase, id: "contract_pizza", status: "expired", goal: "Large pizza for tonight", category: "food",
    spend: { ...shoeBase.spend, target: 25, hard_cap_all_in: 35, hard_cap_source: "inferred" },
    delivery: { ...shoeBase.delivery, deliver_by: at(-6 * DAY + 3 * HOUR), max_shipping: 5 },
    constraints: [
      { field: "food_size", operator: "eq", value: "large", severity: "hard", source: "user" },
      { field: "dietary", operator: "contains", value: "vegetarian", severity: "hard", source: "user" },
    ],
    created_at: at(-6 * DAY), signed_at: at(-6 * DAY + MIN), expires_at: at(-6 * DAY + 4 * HOUR),
  }),
];

// ---------- Funding (signing = funding; one single-use card per contract) ----------

/** A simulated-provider funding, as GET /contracts/{id}/funding returns it. */
export function mockFunding(contractId: string, amount: number, state: FundingState, extra: Partial<Funding> = {}): Funding {
  const stored = state === "funded";
  return {
    funding_id: `funding_${contractId.replace(/^contract_/, "")}`, state, provider: "stub", provider_label: "Simulated provider",
    approval_url: `/contracts/${contractId}`, provider_reference: `spend_${contractId.replace(/^contract_/, "")}`,
    amount, currency: "USD", merchant_name: "Amazon.com", card_stored: stored,
    card_last4: state === "awaiting_approval" || state === "denied" ? null : "4242",
    valid_until: stored || state === "released" ? at(12 * HOUR) : null, released_purchase_id: null, last_error: null,
    ...extra,
  };
}

export const fundings: Record<string, Funding> = {
  contract_shoes: mockFunding("contract_shoes", 135, "used", { released_purchase_id: "purchase_pass" }),
  contract_shoes_b: mockFunding("contract_shoes_b", 135, "funded"),
  contract_shoes_c: mockFunding("contract_shoes_c", 135, "funded"),
  contract_shoes_d: mockFunding("contract_shoes_d", 135, "awaiting_approval"),
  contract_shoes_e: mockFunding("contract_shoes_e", 135, "expired"),
  contract_trail: mockFunding("contract_trail", 145, "funded"),
  contract_headphones: mockFunding("contract_headphones", 320, "used"),
  contract_tickets: mockFunding("contract_tickets", 400, "canceled", { card_last4: "4242" }),
  contract_pizza: mockFunding("contract_pizza", 35, "expired"),
};

// ---------- Purchases ----------

function proposal(p: Partial<TransactionProposal> & Pick<TransactionProposal, "id" | "contract_id" | "merchant" | "line_items" | "item_subtotal" | "total">): TransactionProposal {
  return {
    tax: 0, shipping: 0, fees: 0, discounts: 0, currency: "USD",
    recurring_billing: { detected: false, interval: null, amount: null, description: null },
    addons_detected: false, membership_detected: false, delivery: null, return_terms: null,
    extracted_attributes: {}, source_url: null, extracted_at: at(-5 * MIN), extractor_ids: ["mock"],
    extractors_agreed: true, evidence: {},
    ...p,
  };
}

const shoe = (name: string, price: number, extra: Record<string, unknown> = {}) => ({
  name, category: "running shoes", brand: name.split(" ")[0], model: name, gtin: null, mpn: null,
  condition: "new", quantity: 1, unit_price: price, attributes: { size: "10", ...extra },
});

const passProposal = proposal({
  id: "proposal_pass", contract_id: "contract_shoes",
  merchant: { name: "Nike", domain: "nike.example", merchant_id: "m_nike", seller_of_record: "Nike, Inc.", is_first_party: true, is_verified: true },
  line_items: [shoe("Nike Pegasus 41", 119.99, { color: "black/volt" })],
  item_subtotal: 119.99, tax: 8.4, shipping: 0, total: 128.39,
  delivery: { promised_by: at(2 * DAY), carrier: "UPS", tracking_available: true, verified: true, evidence: "Checkout page: 'Arrives in 2 days'" },
  source_url: "https://merchant.example/checkout/cs_demo_pass",
});

const blockedProposal = proposal({
  id: "proposal_blocked", contract_id: "contract_shoes_b",
  merchant: { name: "RunFastOutlet", domain: "runfast-outlet.example", merchant_id: null, seller_of_record: "RunFastOutlet LLC", is_first_party: false, is_verified: true },
  line_items: [
    shoe("Nike Pegasus 41", 119.99, { color: "black/volt" }),
    { name: "ShoeCare+ protection plan", category: "add-on", brand: null, model: null, gtin: null, mpn: null,
      condition: null, quantity: 1, unit_price: 14.99, attributes: { pre_checked: true } },
  ],
  item_subtotal: 134.98, tax: 6.75, shipping: 7.99, total: 149.72,
  addons_detected: true,
  delivery: { promised_by: at(3 * DAY), carrier: "USPS", tracking_available: true, verified: true, evidence: null },
  source_url: "https://merchant.example/checkout/cs_demo_blocked",
  evidence: { addon_checkbox: "Pre-checked box: 'Protect my shoes with ShoeCare+ ($14.99)'" },
});

const escalatedProposal = proposal({
  id: "proposal_escalated", contract_id: "contract_shoes_c",
  merchant: { name: "SneakerDeals123", domain: "sneakerdeals123.example", merchant_id: null, seller_of_record: "SneakerDeals123", is_first_party: false, is_verified: null },
  line_items: [shoe("Brooks Ghost 16", 114.95, { color: "blue" })],
  item_subtotal: 114.95, tax: 7.47, shipping: 5.0, total: 127.42,
  delivery: { promised_by: at(4 * DAY), carrier: null, tracking_available: false, verified: false, evidence: null },
  source_url: "https://merchant.example/checkout/cs_demo_escalated",
  evidence: { seller_page: "No business address. Domain registered 9 days ago." },
});

const pendingProposal = proposal({
  id: "proposal_pending", contract_id: "contract_trail",
  merchant: { name: "HOKA", domain: "hoka.example", merchant_id: "m_hoka", seller_of_record: "Deckers Brands", is_first_party: true, is_verified: true },
  line_items: [shoe("HOKA Speedgoat 6", 124.95, { color: "stone/oat" })],
  item_subtotal: 124.95, tax: 7.23, shipping: 0, total: 132.18,
  delivery: { promised_by: at(3 * DAY), carrier: "FedEx", tracking_available: true, verified: true, evidence: "Checkout page: 'Arrives in 3 days'" },
  source_url: "https://merchant.example/checkout/cs_demo_pending",
});

const money = (n: number) => `$${n.toFixed(2)}`;

function decision(id: string, proposalId: string, contractId: string, results: ValidationDecision["results"]): ValidationDecision {
  const hard = results.filter((r) => r.severity === "hard");
  const verdict = hard.some((r) => r.verdict === "fail") ? "fail"
    : hard.some((r) => r.verdict === "unverifiable") ? "unverifiable" : "pass";
  return { id, contract_id: contractId, proposal_id: proposalId, verdict, results, evaluated_at: at(-4 * MIN) };
}

const common = (p: TransactionProposal, overrides: Record<string, Partial<ValidationDecision["results"][number]>> = {}) => {
  const base: ValidationDecision["results"] = [
    { constraint: "constraint[0]:category:eq", field: "category", label: "Category", verdict: "pass", expected: "Running shoes", actual: p.line_items[0].name, reason: "Product is a running shoe.", severity: "hard" },
    { constraint: "constraint[1]:size:eq", field: "size", label: "Shoe size", verdict: "pass", expected: "10", actual: "10", reason: "Size 10 selected at checkout.", severity: "hard" },
    { constraint: "constraint[2]:condition:eq", field: "condition", label: "Condition", verdict: "pass", expected: "New", actual: "New", reason: "Listed as new.", severity: "hard" },
    { constraint: "hard_cap_all_in", field: "total_price", label: "Total price", verdict: p.total <= 135 ? "pass" : "fail", expected: "<= 135.00 USD", actual: `${p.total.toFixed(2)} USD`, reason: p.total <= 135 ? `${money(p.total)} is within the $135.00 all-in maximum.` : `${money(p.total)} is ${money(p.total - 135)} over the signed $135.00 maximum.`, severity: "hard" },
    { constraint: "max_shipping", field: "shipping", label: "Shipping cost", verdict: p.shipping <= 8 ? "pass" : "fail", expected: "<= 8.00 USD", actual: `${p.shipping.toFixed(2)} USD`, reason: `Shipping is ${money(p.shipping)}.`, severity: "hard" },
    { constraint: "no_subscription", field: "subscription", label: "No subscription", verdict: "pass", expected: "None", actual: "None", reason: "No recurring charge found at checkout.", severity: "hard" },
    { constraint: "no_addons", field: "addons", label: "No add-ons", verdict: p.addons_detected ? "fail" : "pass", expected: "None", actual: p.addons_detected ? "ShoeCare+ ($14.99)" : "None", reason: p.addons_detected ? "A pre-checked ShoeCare+ protection plan was added to the cart." : "Cart contains only the shoe.", severity: "hard" },
    { constraint: "delivery_deadline", field: "delivery_date", label: "Delivery date", verdict: "pass", expected: "By Monday", actual: "Before deadline", reason: "Promised delivery is before the deadline.", severity: "hard" },
    { constraint: "seller_requirement", field: "seller_of_record", label: "Seller", verdict: "pass", expected: "First-party or verified retailer", actual: p.merchant.seller_of_record, reason: p.merchant.is_first_party ? "Sold directly by the brand." : "Seller is a verified retailer.", severity: "hard" },
    { constraint: "checkout_link_merchant", field: "merchant_name", label: "Link belongs to merchant", verdict: "pass", expected: p.merchant.domain, actual: p.merchant.domain, reason: "The checkout link's origin belongs to the merchant the page claims to be.", severity: "hard" },
  ];
  return base.map((r) => ({ ...r, ...(overrides[r.constraint] ?? {}) }));
};

/** A simulated-provider payment, as PurchaseDetail.payment. */
export function mockPayment(amount: number, state: PaymentState, extra: Partial<PaymentInfo> = {}): PaymentInfo {
  return {
    state, provider: "stub", provider_label: "Simulated provider", approval_url: null,
    provider_reference: "spend_demo", amount, pay_amount: amount, currency: "USD", last4: null,
    credential_released: false, order_id: null, receipt: null, last_error: null, updated_at: at(-3 * MIN),
    ...extra,
  };
}

const TERMINAL_PAYMENT: PaymentState[] = ["completed", "denied", "expired", "checkout_changed", "failed"];
const NEXT_FOR_PAYMENT: Partial<Record<PaymentState, NextAction>> = {
  credential_ready: "get_payment_credential_and_pay", paying: "wait_for_merchant_order",
  paid: "wait_for_order_verification", unknown: "wait_for_reconciliation",
};

/** Recomputes the derived PurchaseDetail keys the backend fills in, after a mock mutation. */
export function syncDetail(d: PurchaseDetail): PurchaseDetail {
  const p = d.purchase;
  d.purchase_id = p.id;
  d.status = p.status;
  d.contract_id = p.contract_id;
  d.proposal_id = p.proposal_id;
  d.payment_state = d.payment?.state ?? null;
  d.approval_url = d.payment?.approval_url ?? null;
  const pay = d.payment?.state;
  const next: NextAction =
    p.status === "completed" ? "completed"
    : p.status === "escalated" ? "wait_for_user_decision"
    : p.status === "blocked" || p.status === "failed" ? (pay === "expired" || pay === "checkout_changed" ? "request_purchase_again" : "blocked_no_action")
    : p.status !== "authorized" || !pay ? "wait"
    : NEXT_FOR_PAYMENT[pay] ?? (TERMINAL_PAYMENT.includes(pay) ? "blocked_no_action" : "wait");
  d.next_action = next;
  return d;
}

function detail(purchase: Purchase, prop: TransactionProposal, dec: ValidationDecision, summary: string, payment: PaymentInfo | null = null): PurchaseDetail {
  return syncDetail({
    purchase_id: purchase.id, status: purchase.status, decision: dec, contract_id: purchase.contract_id,
    proposal_id: purchase.proposal_id, credential: null, summary, idempotent_replay: false,
    purchase, proposal: prop, payment, payment_state: null, funding: fundings[purchase.contract_id] ?? { state: "not_funded", card_stored: false },
    approval_url: null, resolution: null,
    next_action: "wait", review_url: `/purchases/${purchase.id}`,
  });
}

export const purchases: PurchaseDetail[] = [
  detail(
    { id: "purchase_pass", contract_id: "contract_shoes", proposal_id: passProposal.id, decision_id: "decision_pass", credential_id: "cred_pass",
      status: "completed", merchant_name: "Nike", authorized_amount: 128.39, charged_amount: 128.39, currency: "USD",
      created_at: at(-6 * MIN), completed_at: at(-4 * MIN), error: null },
    passProposal,
    decision("decision_pass", passProposal.id, "contract_shoes", common(passProposal)),
    "Completed: charged $128.39 at Nike.",
    mockPayment(128.39, "completed", { last4: "4242", order_id: "order_demo_1042", credential_released: true, updated_at: at(-4 * MIN) }),
  ),
  detail(
    { id: "purchase_pending", contract_id: "contract_trail", proposal_id: pendingProposal.id, decision_id: "decision_pending", credential_id: "cred_pending",
      status: "authorized", merchant_name: "HOKA", authorized_amount: 132.18, charged_amount: null, currency: "USD",
      created_at: at(-3 * MIN), completed_at: null, error: null },
    pendingProposal,
    decision("decision_pending", pendingProposal.id, "contract_trail", common(pendingProposal).map((r) =>
      r.constraint === "hard_cap_all_in" ? { ...r, expected: "<= 145.00 USD", reason: "$132.18 is within the $145.00 all-in maximum.", verdict: "pass" as const }
      : r.field === "category" ? { ...r, expected: "Trail running shoes" } : r)),
    "Authorized $132.18 at HOKA. A single-use credential was issued.",
    mockPayment(132.18, "credential_ready"),
  ),
  detail(
    { id: "purchase_blocked", contract_id: "contract_shoes_b", proposal_id: blockedProposal.id, decision_id: "decision_blocked", credential_id: null,
      status: "blocked", merchant_name: "RunFastOutlet", authorized_amount: null, charged_amount: null, currency: "USD",
      created_at: at(-6 * MIN), completed_at: null, error: null },
    blockedProposal,
    decision("decision_blocked", blockedProposal.id, "contract_shoes_b", common(blockedProposal)),
    "Blocked: $149.72 is $14.72 over the signed $135.00 maximum. A pre-checked ShoeCare+ protection plan was added to the cart.",
  ),
  detail(
    { id: "purchase_escalated", contract_id: "contract_shoes_c", proposal_id: escalatedProposal.id, decision_id: "decision_escalated", credential_id: null,
      status: "escalated", merchant_name: "SneakerDeals123", authorized_amount: null, charged_amount: null, currency: "USD",
      created_at: at(-6 * MIN), completed_at: null, error: null },
    escalatedProposal,
    decision("decision_escalated", escalatedProposal.id, "contract_shoes_c", common(escalatedProposal, {
      seller_requirement: { verdict: "unverifiable", actual: "SneakerDeals123", reason: "Handshake could not confirm this seller is an authorized retailer.",
        evidence: { seller: "SneakerDeals123", domain: "sneakerdeals123.example", note: "No business address. Domain registered 9 days ago." } },
      delivery_deadline: { verdict: "unverifiable", severity: "soft", actual: "No carrier or tracking", reason: "Seller promised delivery but gave no carrier or tracking. (Preference, not a hard rule.)" },
    })),
    "Needs your approval: Handshake could not confirm this seller is an authorized retailer.",
  ),
];

// ---------- Evidence ----------

type Story = "completed" | "awaiting_payment" | "fail" | "unverifiable";

function events(purchaseId: string, contractId: string, story: Story, total: number, passed: number, of: number): EvidenceEvent[] {
  let n = 0;
  // Signing and funding events belong to the contract (purchase_id null), as the backend's evidence chain returns them.
  const e = (i: number, event_type: EvidenceEvent["event_type"], message: string, data: EvidenceEvent["data"] = {}): EvidenceEvent =>
    ({ id: `event_${purchaseId}_${n++}`, purchase_id: event_type === "contract_signed" ? null : purchaseId, contract_id: contractId,
       event_type, timestamp: at(-20 * MIN + i * 45_000), message, data });
  const start = [
    e(0, "contract_signed", "Contract signed"),
    e(1, "contract_signed", "Asked the simulated provider for a single-use card for the all-in cap", { kind: "funding_requested" }),
    e(1, "contract_signed", "Simulated provider approval: you approved the (simulated) funding.", { kind: "simulated_provider_approval" }),
    e(2, "contract_signed", "Card ending 4242 stored encrypted on the contract, locked", { kind: "card_stored" }),
    e(2, "shopping_started", "Agent began shopping"),
    e(3, "proposal_created", `Handshake read the checkout · $${total.toFixed(2)}`),
    e(4, "validation_started", "Handshake checking contract"),
  ];
  const authorized = [
    e(5, "validation_completed", `${passed}/${of} constraints passed`),
    e(6, "purchase_authorized", `Purchase authorized for $${total.toFixed(2)}`),
    e(7, "purchase_authorized", "The contract's card is unlocked for this checkout only", { kind: "credential_ready" }),
  ];
  if (story === "completed") return [...start, ...authorized,
    e(8, "validation_completed", "Checkout rechecked before release: unchanged", { kind: "checkout_revalidated" }),
    e(9, "credential_created", "Card ending 4242 released once to the agent; stored copy wiped", { kind: "credential_released" }),
    e(10, "credential_used", `Paid $${total.toFixed(2)} with the single-use card`, { kind: "payment_submitted" }),
    e(11, "payment_completed", "Merchant order verified against the receipt", { kind: "receipt_verified" }),
  ];
  if (story === "awaiting_payment") return [...start, ...authorized];
  if (story === "fail") return [...start,
    e(5, "validation_completed", `${passed}/${of} constraints passed`),
    e(6, "purchase_blocked", "Purchase blocked · no card issued"),
  ];
  return [...start,
    e(5, "validation_completed", `${passed}/${of} passed · ${of - passed} could not be verified`),
    e(6, "purchase_escalated", "Waiting for your decision"),
  ];
}

export const evidence: Record<string, EvidenceEvent[]> = {
  purchase_pass: events("purchase_pass", "contract_shoes", "completed", 128.39, 10, 10),
  purchase_pending: events("purchase_pending", "contract_trail", "awaiting_payment", 132.18, 10, 10),
  purchase_blocked: events("purchase_blocked", "contract_shoes_b", "fail", 149.72, 8, 10),
  purchase_escalated: events("purchase_escalated", "contract_shoes_c", "unverifiable", 127.42, 8, 10),
};

// ---------- Agents connected to the account (GET /agents) ----------

/** One of each kind, plus a revoked key, so the Agents page shows every state. Never includes a token. */
export const agents: Agent[] = [
  { agent_id: "agt_muse", bound_agent_id: "hsc_muse", client_id: "hsc_muse", name: "Muse", kind: "oauth", client_name: "Muse", created_at: at(-2 * DAY), last_used_at: at(-20 * MIN), expires_at: at(28 * DAY), revoked_at: null },
  { agent_id: "agt_demo", bound_agent_id: "agent_demo", client_id: "agent_demo", name: "Handshake MCP (demo agent)", kind: "device", client_name: "Handshake MCP (demo agent)", created_at: at(-5 * DAY), last_used_at: at(-3 * DAY), expires_at: at(25 * DAY), revoked_at: null },
  { agent_id: "agt_oldkey", bound_agent_id: "key_oldkey", client_id: null, name: "Laptop script", kind: "key", client_name: null, created_at: at(-9 * DAY), last_used_at: null, expires_at: at(21 * DAY), revoked_at: at(-8 * DAY) },
];
