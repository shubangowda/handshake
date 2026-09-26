// Demo fixtures. Times are relative to page load so "expires tomorrow" stays true.
import type {
  Constraint, Contract, DraftRecord, EvidenceEvent, PurchaseDetail,
  TransactionProposal, ValidationDecision,
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
    signature: "ed25519:demo",
    previous_contract_id: null,
    escalate_if: ["any_unverifiable_hard"],
    selection_disclosure_required: true,
    revocable: true,
    data_sharing: { allowed_fields: ["name", "shipping_address"], merchant_may_contact: false },
    substitution: { allowed: false, same_model_other_color: false, same_brand: false, max_price_delta: null },
    instrument: { type: "virtual_single_use", fixed_by_contract: true },
    funding: { status: "held", amount_held: body.spend.hard_cap_all_in, amount_captured: 0, amount_refunded: 0, funded_at: body.signed_at, settled_at: null },
    ...body,
  };
}

const settled = (held: number, captured: number, at_: string) => ({
  status: (captured ? "captured" : "refunded") as "captured" | "refunded",
  amount_held: held, amount_captured: captured, amount_refunded: Math.round((held - captured) * 100) / 100,
  funded_at: at_, settled_at: at_,
});

export const contracts: Contract[] = [
  signed({ ...shoeBase, id: "contract_shoes", status: "used",
    created_at: at(-20 * MIN), signed_at: at(-18 * MIN), expires_at: at(DAY), funding: settled(135, 128.39, at(-4 * MIN)) }),
  signed({ ...shoeBase, id: "contract_shoes_b", status: "active", goal: "Running shoes — marathon pair",
    created_at: at(-40 * MIN), signed_at: at(-38 * MIN), expires_at: at(DAY) }),
  signed({ ...shoeBase, id: "contract_shoes_c", status: "active", goal: "Running shoes — backup pair",
    merchants: { ...shoeBase.merchants },
    created_at: at(-60 * MIN), signed_at: at(-58 * MIN), expires_at: at(DAY) }),
  signed({ ...shoeBase, id: "contract_trail", status: "active", goal: "Trail running shoes",
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
    created_at: at(-3 * DAY), signed_at: at(-3 * DAY + 2 * MIN), expires_at: at(-2 * DAY), funding: settled(320, 298.0, at(-3 * DAY + HOUR)),
  }),
  signed({
    ...shoeBase, id: "contract_tickets", status: "revoked", goal: "2 tickets — Warriors vs. Lakers", category: "tickets",
    spend: { ...shoeBase.spend, target: 300, hard_cap_all_in: 400, hard_cap_source: "user" },
    constraints: [
      { field: "ticket_quantity", operator: "eq", value: 2, severity: "hard", source: "user" },
      { field: "seats_together", operator: "eq", value: true, severity: "hard", source: "user" },
    ],
    created_at: at(-5 * DAY), signed_at: at(-5 * DAY + MIN), expires_at: at(2 * DAY), funding: settled(400, 0, at(-4 * DAY)),
  }),
  signed({
    ...shoeBase, id: "contract_pizza", status: "expired", goal: "Large pizza for tonight", category: "food",
    spend: { ...shoeBase.spend, target: 25, hard_cap_all_in: 35, hard_cap_source: "inferred" },
    delivery: { ...shoeBase.delivery, deliver_by: at(-6 * DAY + 3 * HOUR), max_shipping: 5 },
    constraints: [
      { field: "food_size", operator: "eq", value: "large", severity: "hard", source: "user" },
      { field: "dietary", operator: "contains", value: "vegetarian", severity: "hard", source: "user" },
    ],
    created_at: at(-6 * DAY), signed_at: at(-6 * DAY + MIN), expires_at: at(-6 * DAY + 4 * HOUR), funding: settled(35, 0, at(-6 * DAY + 4 * HOUR)),
  }),
];

// ---------- Purchases ----------

function proposal(p: Partial<TransactionProposal> & Pick<TransactionProposal, "id" | "contract_id" | "merchant" | "line_items" | "item_subtotal" | "total">): TransactionProposal {
  return {
    tax: 0, shipping: 0, fees: 0, discounts: 0, currency: "USD",
    recurring_billing: { detected: false, interval: null, amount: null, description: null },
    addons_detected: false, membership_detected: false, delivery: null,
    source_url: null, extracted_at: at(-5 * MIN), extractors_agreed: true, evidence: {},
    ...p,
  };
}

const shoe = (name: string, price: number, extra: Record<string, unknown> = {}) => ({
  name, category: "running shoes", brand: name.split(" ")[0], model: name, gtin: null, mpn: null,
  condition: "new", quantity: 1, unit_price: price, attributes: { size: "10", ...extra },
});

const passProposal = proposal({
  id: "proposal_pass", contract_id: "contract_shoes",
  merchant: { name: "Nike", domain: "nike.com", merchant_id: "m_nike", seller_of_record: "Nike, Inc.", is_first_party: true, is_verified: true },
  line_items: [shoe("Nike Pegasus 41", 119.99, { color: "black/volt" })],
  item_subtotal: 119.99, tax: 8.4, shipping: 0, total: 128.39,
  delivery: { promised_by: at(2 * DAY), carrier: "UPS", tracking_available: true, verified: true, evidence: "Checkout page: 'Arrives in 2 days'" },
  source_url: "https://www.nike.com/checkout",
});

const blockedProposal = proposal({
  id: "proposal_blocked", contract_id: "contract_shoes_b",
  merchant: { name: "RunFastOutlet", domain: "runfast-outlet.shop", merchant_id: null, seller_of_record: "RunFastOutlet LLC", is_first_party: false, is_verified: true },
  line_items: [
    shoe("Nike Pegasus 41", 119.99, { color: "black/volt" }),
    { name: "ShoeCare+ protection plan", category: "add-on", brand: null, model: null, gtin: null, mpn: null,
      condition: null, quantity: 1, unit_price: 14.99, attributes: { pre_checked: true } },
  ],
  item_subtotal: 134.98, tax: 6.75, shipping: 7.99, total: 149.72,
  addons_detected: true,
  delivery: { promised_by: at(3 * DAY), carrier: "USPS", tracking_available: true, verified: true, evidence: null },
  source_url: "https://runfast-outlet.shop/checkout",
  evidence: { addon_checkbox: "Pre-checked box: 'Protect my shoes with ShoeCare+ ($14.99)'" },
});

const escalatedProposal = proposal({
  id: "proposal_escalated", contract_id: "contract_shoes_c",
  merchant: { name: "SneakerDeals123", domain: "sneakerdeals123.store", merchant_id: null, seller_of_record: "SneakerDeals123", is_first_party: false, is_verified: null },
  line_items: [shoe("Brooks Ghost 16", 114.95, { color: "blue" })],
  item_subtotal: 114.95, tax: 7.47, shipping: 5.0, total: 127.42,
  delivery: { promised_by: at(4 * DAY), carrier: null, tracking_available: false, verified: false, evidence: null },
  source_url: "https://sneakerdeals123.store/cart",
  evidence: { seller_page: "No business address. Domain registered 9 days ago." },
});

const pendingProposal = proposal({
  id: "proposal_pending", contract_id: "contract_trail",
  merchant: { name: "HOKA", domain: "hoka.com", merchant_id: "m_hoka", seller_of_record: "Deckers Brands", is_first_party: true, is_verified: true },
  line_items: [shoe("HOKA Speedgoat 6", 124.95, { color: "stone/oat" })],
  item_subtotal: 124.95, tax: 7.23, shipping: 0, total: 132.18,
  delivery: { promised_by: at(3 * DAY), carrier: "FedEx", tracking_available: true, verified: true, evidence: "Checkout page: 'Arrives in 3 days'" },
  source_url: "https://www.hoka.com/checkout",
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
    { constraint: "category eq running shoes", field: "category", label: "Product", verdict: "pass", expected: "Running shoes", actual: p.line_items[0].name, reason: "Product is a running shoe.", severity: "hard" },
    { constraint: "size eq 10", field: "size", label: "Size", verdict: "pass", expected: "10", actual: "10", reason: "Size 10 selected at checkout.", severity: "hard" },
    { constraint: "condition eq new", field: "condition", label: "Condition", verdict: "pass", expected: "New", actual: "New", reason: "Listed as new.", severity: "hard" },
    { constraint: "total_price lte 135", field: "total_price", label: "Total price", verdict: p.total <= 135 ? "pass" : "fail", expected: "≤ $135.00", actual: money(p.total), reason: p.total <= 135 ? `${money(p.total)} is within the $135.00 all-in maximum.` : `${money(p.total)} is ${money(p.total - 135)} over the signed $135.00 maximum.`, severity: "hard" },
    { constraint: "shipping lte 8", field: "shipping", label: "Shipping", verdict: p.shipping <= 8 ? "pass" : "fail", expected: "≤ $8.00", actual: money(p.shipping), reason: `Shipping is ${money(p.shipping)}.`, severity: "hard" },
    { constraint: "subscription eq false", field: "subscription", label: "Subscription", verdict: "pass", expected: "None", actual: "None", reason: "No recurring charge found at checkout.", severity: "hard" },
    { constraint: "addons eq false", field: "addons", label: "Add-ons", verdict: p.addons_detected ? "fail" : "pass", expected: "None", actual: p.addons_detected ? "ShoeCare+ ($14.99)" : "None", reason: p.addons_detected ? "A pre-checked ShoeCare+ protection plan was added to the cart." : "Cart contains only the shoe.", severity: "hard" },
    { constraint: "delivery_date before deadline", field: "delivery_date", label: "Delivery", verdict: "pass", expected: "By Monday", actual: "Before deadline", reason: "Promised delivery is before the deadline.", severity: "hard" },
    { constraint: "seller first_party_or_verified", field: "seller_of_record", label: "Seller", verdict: "pass", expected: "First-party or verified retailer", actual: p.merchant.seller_of_record, reason: p.merchant.is_first_party ? "Sold directly by the brand." : "Seller is a verified retailer.", severity: "hard" },
  ];
  return base.map((r) => ({ ...r, ...(overrides[r.field!] ?? {}) }));
};

export const purchases: PurchaseDetail[] = [
  {
    purchase: { id: "purchase_pass", contract_id: "contract_shoes", proposal_id: passProposal.id, decision_id: "decision_pass", credential_id: "cred_pass",
      status: "completed", merchant_name: "Nike", authorized_amount: 128.39, charged_amount: 128.39, currency: "USD",
      created_at: at(-6 * MIN), completed_at: at(-4 * MIN), error: null },
    proposal: passProposal,
    decision: decision("decision_pass", passProposal.id, "contract_shoes", common(passProposal)),
  },
  {
    purchase: { id: "purchase_pending", contract_id: "contract_trail", proposal_id: pendingProposal.id, decision_id: "decision_pending", credential_id: null,
      status: "authorized", merchant_name: "HOKA", authorized_amount: 132.18, charged_amount: null, currency: "USD",
      created_at: at(-3 * MIN), completed_at: null, error: null },
    proposal: pendingProposal,
    decision: decision("decision_pending", pendingProposal.id, "contract_trail", common(pendingProposal).map((r) =>
      r.field === "total_price" ? { ...r, expected: "≤ $145.00", reason: "$132.18 is within the $145.00 all-in maximum.", verdict: "pass" as const }
      : r.field === "category" ? { ...r, expected: "Trail running shoes" } : r)),
  },
  {
    purchase: { id: "purchase_blocked", contract_id: "contract_shoes_b", proposal_id: blockedProposal.id, decision_id: "decision_blocked", credential_id: null,
      status: "blocked", merchant_name: "RunFastOutlet", authorized_amount: null, charged_amount: null, currency: "USD",
      created_at: at(-6 * MIN), completed_at: null, error: null },
    proposal: blockedProposal,
    decision: decision("decision_blocked", blockedProposal.id, "contract_shoes_b", common(blockedProposal)),
  },
  {
    purchase: { id: "purchase_escalated", contract_id: "contract_shoes_c", proposal_id: escalatedProposal.id, decision_id: "decision_escalated", credential_id: null,
      status: "escalated", merchant_name: "SneakerDeals123", authorized_amount: null, charged_amount: null, currency: "USD",
      created_at: at(-6 * MIN), completed_at: null, error: null, resolution: null },
    proposal: escalatedProposal,
    decision: decision("decision_escalated", escalatedProposal.id, "contract_shoes_c", common(escalatedProposal, {
      seller_of_record: { verdict: "unverifiable", actual: "SneakerDeals123", reason: "Handshake could not confirm this seller is an authorized retailer.",
        evidence: { seller: "SneakerDeals123", domain: "sneakerdeals123.store", note: "No business address. Domain registered 9 days ago." } },
      delivery_date: { verdict: "unverifiable", severity: "soft", actual: "No carrier or tracking", reason: "Seller promised delivery but gave no carrier or tracking. (Preference, not a hard rule.)" },
    })),
  },
];

// ---------- Evidence ----------

function events(purchaseId: string, contractId: string, verdict: "pass" | "awaiting_payment" | "fail" | "unverifiable", total: number, passed: number, of: number): EvidenceEvent[] {
  const e = (i: number, event_type: EvidenceEvent["event_type"], message: string, data: Record<string, unknown> = {}): EvidenceEvent =>
    ({ id: `event_${purchaseId}_${i}`, purchase_id: purchaseId, contract_id: contractId, event_type, timestamp: at(-20 * MIN + i * 45_000), message, data });
  const start = [
    e(0, "contract_signed", "Contract signed · funds held"),
    e(1, "shopping_started", "Agent began shopping"),
    e(3, "proposal_created", `Transaction proposed · $${total.toFixed(2)}`),
    e(4, "validation_started", "Handshake checking contract"),
  ];
  if (verdict === "pass") return [...start,
    e(5, "validation_completed", `${passed}/${of} constraints passed`),
    e(6, "purchase_authorized", "Waiting for your approval"),
    e(7, "escalation_approved", "You approved the purchase"),
    e(8, "payment_completed", `Paid $${total.toFixed(2)} from held funds`),
    e(9, "funds_refunded", `$${(135 - total).toFixed(2)} refunded to your card`),
  ];
  if (verdict === "awaiting_payment") return [...start,
    e(5, "validation_completed", `${passed}/${of} constraints passed`),
    e(6, "purchase_authorized", "Waiting for your approval"),
  ];
  if (verdict === "fail") return [...start,
    e(5, "validation_completed", `${passed}/${of} constraints passed`),
    e(6, "purchase_blocked", "Purchase blocked · no card issued"),
  ];
  return [...start,
    e(5, "validation_completed", `${passed}/${of} passed · ${of - passed} could not be verified`),
    e(6, "purchase_escalated", "Waiting for your decision"),
  ];
}

export const evidence: Record<string, EvidenceEvent[]> = {
  purchase_pass: events("purchase_pass", "contract_shoes", "pass", 128.39, 9, 9),
  purchase_pending: events("purchase_pending", "contract_trail", "awaiting_payment", 132.18, 9, 9),
  purchase_blocked: events("purchase_blocked", "contract_shoes_b", "fail", 149.72, 7, 9),
  purchase_escalated: events("purchase_escalated", "contract_shoes_c", "unverifiable", 127.42, 7, 9),
};
