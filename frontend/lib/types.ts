// Mirrors the backend Pydantic models (hil_version 0.2).
// Fields marked "PROPOSED" are not in the backend yet — they are the frontend's
// suggested additions and should be confirmed with the backend owner.

export type ConstraintOperator =
  | "eq" | "neq" | "lt" | "lte" | "gt" | "gte"
  | "in" | "not_in" | "contains" | "not_contains"
  | "before" | "after";

export type ConstraintSeverity = "hard" | "soft" | "escalating";
export type ValueSource = "user" | "inferred" | "default";
export type ContractStatus = "draft" | "active" | "used" | "revoked" | "expired";
export type ConstraintVerdict = "pass" | "fail" | "unverifiable";
export type PurchaseStatus =
  | "pending" | "validating" | "authorized" | "blocked"
  | "escalated" | "completed" | "failed";
export type CredentialStatus = "created" | "active" | "used" | "revoked" | "expired";
export type ProductCondition = "new" | "used" | "refurbished" | "open_box";
export type SellerRequirement = "any" | "first_party" | "verified" | "first_party_or_verified";
export type MerchantPolicyAction = "allow" | "deny" | "escalate";

export type ConstraintField =
  | "category" | "brand" | "model" | "product_name" | "condition" | "color" | "size" | "quantity"
  | "screen_size_inches" | "display_type" | "refresh_rate_hz" | "storage_gb" | "memory_gb"
  | "material" | "gender" | "fit"
  | "food_size" | "toppings" | "dietary"
  | "event_name" | "ticket_quantity" | "seats_together" | "section"
  | "merchant_name" | "seller_of_record"
  | "return_days" | "subscription" | "membership" | "addons"
  | "delivery_date"
  | "item_price" | "shipping" | "fees" | "tax" | "total_price";

export type ConstraintScalar = string | number | boolean;
export type ConstraintValue = ConstraintScalar | ConstraintScalar[] | null;

export interface Constraint {
  field: ConstraintField;
  operator: ConstraintOperator;
  value: ConstraintValue;
  severity: ConstraintSeverity;
  source: ValueSource;
  description?: string | null;
}

export interface SpendPolicy {
  currency: string;
  target: number | null;
  hard_cap_all_in: number;
  includes: ("item" | "tax" | "shipping" | "fees")[];
  target_source: ValueSource;
  hard_cap_source: ValueSource;
}

export interface DeliveryPolicy {
  deliver_by: string | null;
  max_shipping: number | null;
  require_verified_estimate: boolean;
  deliver_by_source: ValueSource;
  max_shipping_source: ValueSource;
}

export interface TermsPolicy {
  no_subscription: boolean;
  no_membership: boolean;
  no_addons: boolean;
  min_return_days: number | null;
  no_forced_account_creation: boolean;
}

export interface MerchantPolicy {
  allow: string[];
  deny: string[];
  seller_requirement: SellerRequirement;
  new_merchant: MerchantPolicyAction;
}

export interface SubstitutionPolicy {
  allowed: boolean;
  same_model_other_color: boolean;
  same_brand: boolean;
  max_price_delta: number | null;
}

export interface DataSharingPolicy {
  allowed_fields: ("name" | "email" | "phone" | "shipping_address" | "billing_address")[];
  merchant_may_contact: boolean;
}

export interface InstrumentPolicy {
  type: "virtual_single_use" | "network_token" | "stub";
  fixed_by_contract: boolean;
}

/** Fields shared by drafts and signed contracts. */
interface ContractBody {
  id: string;
  hil_version: string;
  goal: string;
  category: string | null;
  spend: SpendPolicy;
  delivery: DeliveryPolicy | null;
  terms: TermsPolicy;
  merchants: MerchantPolicy;
  substitution: SubstitutionPolicy;
  data_sharing: DataSharingPolicy;
  instrument: InstrumentPolicy;
  constraints: Constraint[];
  escalate_if: string[];
  selection_disclosure_required: boolean;
  single_use: boolean;
  revocable: boolean;
  expires_at: string | null;
  created_at: string;
}

export type ContractDraft = ContractBody;

/**
 * PROPOSED: prepaid funds for a contract. The user pays the hard cap when signing;
 * on purchase the total is captured and the rest refunded; with no purchase, all of it is refunded.
 */
export interface Funding {
  status: "held" | "captured" | "refunded";
  amount_held: number;
  amount_captured: number;
  amount_refunded: number;
  funded_at: string;
  settled_at: string | null;
}

export interface Contract extends ContractBody {
  status: ContractStatus;
  funding?: Funding | null;
  agent_key: string | null;
  signed_at: string;
  contract_hash: string;
  signature: string;
  previous_contract_id: string | null;
}

/** Draft as the frontend shows it: CompilerOutput flattened onto the draft. */
export interface DraftRecord extends ContractDraft {
  status: "draft";
  assumptions: string[];
  clarifications_needed: string[];
  compiler_notes: string[];
  /** PROPOSED: drafts created by editing a signed contract point back at it. */
  previous_contract_id: string | null;
}

export type ContractRecord = Contract | DraftRecord;

export interface MerchantIdentity {
  name: string;
  domain: string | null;
  merchant_id: string | null;
  seller_of_record: string | null;
  is_first_party: boolean | null;
  is_verified: boolean | null;
}

export interface LineItem {
  name: string;
  category: string | null;
  brand: string | null;
  model: string | null;
  gtin: string | null;
  mpn: string | null;
  condition: ProductCondition | string | null;
  quantity: number;
  unit_price: number;
  attributes: Record<string, unknown>;
}

export interface TransactionProposal {
  id: string;
  contract_id: string;
  merchant: MerchantIdentity;
  line_items: LineItem[];
  item_subtotal: number;
  tax: number;
  shipping: number;
  fees: number;
  discounts: number;
  total: number;
  currency: string;
  recurring_billing: { detected: boolean; interval: string | null; amount: number | null; description: string | null };
  addons_detected: boolean;
  membership_detected: boolean;
  delivery: { promised_by: string | null; carrier: string | null; tracking_available: boolean; verified: boolean; evidence: string | null } | null;
  source_url: string | null;
  extracted_at: string;
  extractors_agreed: boolean;
  evidence: Record<string, unknown>;
}

export interface ConstraintResult {
  constraint: string;
  /** PROPOSED: stable field key + human label so the UI never parses `constraint`. */
  field?: string;
  label?: string;
  verdict: ConstraintVerdict;
  expected?: unknown;
  actual?: unknown;
  reason: string;
  severity: ConstraintSeverity;
  evidence?: Record<string, unknown> | null;
}

export interface ValidationDecision {
  id: string;
  contract_id: string;
  proposal_id: string;
  verdict: ConstraintVerdict;
  results: ConstraintResult[];
  evaluated_at: string;
}

/** PROPOSED: how a human decision on an UNVERIFIABLE purchase is recorded. */
export interface EscalationResolution {
  action: "approve" | "reject";
  resolved_at: string;
  /** Which unverifiable results the user explicitly accepted. They stay UNVERIFIABLE. */
  accepted_constraints: string[];
}

export interface Purchase {
  id: string;
  contract_id: string;
  proposal_id: string | null;
  decision_id: string | null;
  credential_id: string | null;
  status: PurchaseStatus;
  merchant_name: string | null;
  authorized_amount: number | null;
  charged_amount: number | null;
  currency: string;
  created_at: string;
  completed_at: string | null;
  error: string | null;
  resolution?: EscalationResolution | null;
}

export type EvidenceEventType =
  | "contract_created" | "contract_signed" | "contract_revoked"
  | "shopping_started" | "proposal_created"
  | "validation_started" | "validation_completed"
  | "purchase_blocked" | "purchase_escalated" | "purchase_authorized"
  | "credential_created" | "credential_used"
  | "payment_completed" | "payment_mismatch"
  // PROPOSED
  | "escalation_approved" | "escalation_rejected"
  | "contract_funded" | "funds_refunded" | "purchase_declined";

export interface EvidenceEvent {
  id: string;
  purchase_id: string | null;
  contract_id: string;
  event_type: EvidenceEventType;
  timestamp: string;
  message: string;
  data: Record<string, unknown>;
}

/** PROPOSED: PurchaseStatusResponse + the proposal, so the UI can show what the agent attempted. */
export interface PurchaseDetail {
  purchase: Purchase;
  proposal: TransactionProposal | null;
  decision: ValidationDecision | null;
}

/** Editable subset of a draft. */
export interface DraftPatch {
  goal?: string;
  target?: number | null;
  hard_cap_all_in?: number;
  max_shipping?: number | null;
  deliver_by?: string | null;
  constraints?: Constraint[];
}
