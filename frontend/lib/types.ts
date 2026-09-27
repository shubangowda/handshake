// Mirrors handshake/models.py (hil_version 0.2) field for field.
// Everything below the "API-only" line is NOT in models.py: it is the extra JSON the
// HTTP API adds around those models (see docs/API.md, the single source of truth).

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

/** No per-field `source` in models.py, so the UI labels these "Default". */
export interface TermsPolicy {
  no_subscription: boolean;
  no_membership: boolean;
  no_addons: boolean;
  min_return_days: number | null;
  no_forced_account_creation: boolean;
}

/** No per-field `source` in models.py, so the UI labels these "Default". */
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

/** Fields shared by ContractDraft and Contract. */
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

export interface Contract extends ContractBody {
  status: ContractStatus;
  agent_key: string | null;
  signed_at: string;
  contract_hash: string;
  signature: string;
  previous_contract_id: string | null;
}

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

export interface DeliveryProposal {
  promised_by: string | null;
  carrier: string | null;
  tracking_available: boolean;
  verified: boolean;
  evidence: string | null;
}

export interface ReturnTerms {
  returnable: boolean | null;
  return_window_days: number | null;
  restocking_fee: number | null;
  evidence: string | null;
}

export interface RecurringBilling {
  detected: boolean;
  interval: string | null;
  amount: number | null;
  description: string | null;
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
  recurring_billing: RecurringBilling;
  addons_detected: boolean;
  membership_detected: boolean;
  delivery: DeliveryProposal | null;
  return_terms: ReturnTerms | null;
  extracted_attributes: Record<string, unknown>;
  source_url: string | null;
  extracted_at: string;
  extractor_ids: string[];
  extractors_agreed: boolean;
  evidence: Record<string, unknown>;
}

export interface ConstraintResult {
  constraint: string;
  verdict: ConstraintVerdict;
  expected?: unknown;
  actual?: unknown;
  reason: string;
  severity: ConstraintSeverity;
  evidence?: Record<string, unknown> | null;
  /** API-only (added when the API serializes a decision): stable field key. */
  field: string;
  /** API-only: human label. The UI shows this and never parses `constraint`. */
  label: string;
}

export interface ValidationDecision {
  id: string;
  contract_id: string;
  proposal_id: string;
  verdict: ConstraintVerdict;
  results: ConstraintResult[];
  evaluated_at: string;
}

export interface Credential {
  id: string;
  contract_id: string;
  proposal_id: string;
  merchant_name: string;
  merchant_id: string | null;
  max_amount: number;
  currency: string;
  single_use: boolean;
  status: CredentialStatus;
  created_at: string;
  expires_at: string;
  provider_reference: string | null;
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
}

export type EvidenceEventType =
  | "contract_created" | "contract_signed" | "contract_revoked"
  | "shopping_started" | "proposal_created"
  | "validation_started" | "validation_completed"
  | "purchase_blocked" | "purchase_escalated" | "purchase_authorized"
  | "credential_created" | "credential_used"
  | "payment_completed" | "payment_mismatch";

export interface EvidenceEvent {
  id: string;
  purchase_id: string | null;
  contract_id: string;
  event_type: EvidenceEventType;
  timestamp: string;
  message: string;
  data: EvidenceData;
}

export interface CandidateProduct {
  name: string;
  merchant: string;
  price: number;
  currency: string;
  sponsored: boolean;
  affiliate: boolean;
  url: string | null;
  attributes: Record<string, unknown>;
}

export interface SelectionReport {
  contract_id: string;
  selected_candidate: CandidateProduct;
  candidates: CandidateProduct[];
  reasoning_summary: string;
  created_at: string;
}

export interface CreateContractDraftRequest { intent: string }
export interface SignContractRequest { draft_id: string; agent_key?: string | null; signature?: string | null }
export interface PurchaseRequest { contract_id: string; checkout_url: string; selection_report?: SelectionReport | null }
export interface PurchaseStatusResponse { purchase_id: string; status: PurchaseStatus; decision: ValidationDecision | null }

export interface CompilerOutput {
  draft: ContractDraft;
  assumptions: string[];
  clarifications_needed: string[];
  compiler_notes: string[];
}

// =====================================================================
// API-only shapes (NOT in models.py). Defined by docs/API.md.
// =====================================================================

/** API-only: evidence `data`. `kind` is the precise subtype for steps models.py has no event_type for. */
export type EvidenceKind =
  | "funding_requested" | "simulated_provider_approval" | "card_stored" | "funding_denied" | "funding_expired"
  | "funding_failed" | "funding_unavailable" | "card_unavailable" | "authorization_expired"
  | "checkout_revalidated" | "checkout_changed" | "credential_ready" | "credential_released"
  | "payment_submitted" | "payment_outcome_unknown" | "payment_request_uncertain" | "payment_request_failed"
  | "payment_not_made" | "order_not_verified" | "order_mismatch" | "contract_tampered" | "contract_no_longer_valid"
  | "credential_retrieval_failed" | "payment_refused" | "receipt_verified" | "receipt_mismatch"
  | "purchase_declined" | "contract_amended" | "draft_edited" | "agent_action_denied" | "extraction_failed";

export interface EvidenceData {
  // `string & {}` keeps autocomplete for known kinds while allowing new ones the backend adds later.
  kind?: EvidenceKind | (string & {});
  human_approval?: boolean;
  human_rejection?: boolean;
  [key: string]: unknown;
}

/** API-only: a draft as GET /drafts returns it (ContractDraft flattened with compiler metadata). */
export interface DraftRecord extends ContractDraft {
  status: "draft";
  assumptions: string[];
  /** Lint problems are prefixed with "lint: ". */
  clarifications_needed: string[];
  compiler_notes: string[];
  /** Drafts created by amending a signed contract point back at it. */
  previous_contract_id: string | null;
  /** Set once this draft has been signed. */
  signed_contract_id: string | null;
  review_url: string;
  /** Lint errors that prevent signing (without the "lint: " prefix). */
  blocking_issues: string[];
  /** Only on the POST /drafts/compile response. */
  compiler_source?: "openai" | "fixture";
}

/** API-only: the live hash/signature check GET /contracts/{id} returns. */
export interface ContractVerification {
  valid: boolean;
  hash_matches?: boolean;
  signature_matches?: boolean;
  [key: string]: unknown;
}

/** API-only: where a contract's funding card is. Signing = funding (see API.md "Payment model"). */
export type FundingState =
  | "not_funded" | "awaiting_approval" | "funded" | "released" | "used"
  | "denied" | "expired" | "failed" | "canceled";

/**
 * API-only: a contract's funding. One single-use Link TEST card for the all-in hard cap, stored
 * encrypted on the contract and released once, only for a checkout Handshake authorized.
 * Never contains card data (at most card_last4). "not_funded" carries only state and card_stored.
 */
export interface Funding {
  state: FundingState;
  card_stored: boolean;
  funding_id?: string;
  provider?: "stub" | "link_test";
  provider_label?: string;
  /** Link's approval page, or (stub mode) the frontend contract page. */
  approval_url?: string | null;
  provider_reference?: string | null;
  amount?: number;
  currency?: string;
  merchant_name?: string | null;
  card_last4?: string | null;
  valid_until?: string | null;
  released_purchase_id?: string | null;
  last_error?: string | null;
}

/** A signed contract plus (API-only) its verification and funding, which api.ts attaches from GET /contracts/{id}. */
export type SignedContract = Contract & { verification?: ContractVerification | null; funding?: Funding };

export type ContractRecord = SignedContract | DraftRecord;

/** API-only: one row of GET /contracts. */
export interface ContractListItem {
  id: string;
  kind: "draft" | "contract";
  status: ContractStatus;
  goal: string;
  created_at: string;
  signed_at: string | null;
  signed_contract_id: string | null;
  draft_id: string | null;
  /** Contracts only. */
  funding_state: FundingState | null;
}

/** API-only: GET /contracts/{id}. */
export type ContractResponse =
  | { kind: "contract"; id: string; status: ContractStatus; draft_id: string | null; contract: Contract; verification: ContractVerification; funding: Funding }
  | { kind: "draft"; id: string; status: "draft"; signed_contract_id: string | null; draft: ContractDraft; verification: null;
      assumptions: string[]; clarifications_needed: string[]; compiler_notes: string[] };

/**
 * API-only: a purchase's payment. Funding happened at signing, so an authorized checkout starts at
 * credential_ready (the contract's card is unlocked for it); paying = the card was released.
 */
export type PaymentState =
  | "credential_ready" | "paying" | "paid" | "completed"
  | "denied" | "expired" | "checkout_changed" | "failed" | "unknown";

/** API-only: PurchaseDetail.payment. Never contains card data (at most last4). */
export interface PaymentInfo {
  state: PaymentState;
  provider: "stub" | "link_test";
  provider_label: string;
  approval_url: string | null;
  provider_reference: string | null;
  amount: number;
  pay_amount: number;
  currency: string;
  last4: string | null;
  credential_released: boolean;
  order_id: string | null;
  receipt: Record<string, unknown> | null;
  last_error: string | null;
  updated_at: string | null;
}

/** API-only: the hint for what happens next. */
export type NextAction =
  | "wait_for_user_decision" | "get_payment_credential_and_pay" | "wait_for_payment" | "wait_for_merchant_order"
  | "wait_for_order_verification" | "wait_for_reconciliation" | "blocked_no_action"
  | "request_purchase_again" | "completed" | "wait";

/** API-only: how a human decided an escalation (approve/reject) or declined an authorized purchase. */
export interface Resolution {
  action: "approve" | "reject" | "decline";
  resolved_at: string | null;
  /** Constraint names of the unverifiable results the user accepted. They stay UNVERIFIABLE. */
  accepted_constraints: string[];
  note: string | null;
}

/** API-only: the credential summary (never the card). */
export interface CredentialSummary {
  credential_id: string;
  merchant_name: string;
  merchant_id: string | null;
  max_amount: number;
  currency: string;
  single_use: boolean;
  status: CredentialStatus;
  expires_at: string;
}

/** API-only: every purchase response (GET /purchases/{id}, approve, reject, ...). */
export interface PurchaseDetail {
  purchase_id: string;
  status: PurchaseStatus;
  decision: ValidationDecision | null;
  contract_id: string;
  proposal_id: string | null;
  credential: CredentialSummary | null;
  summary: string;
  idempotent_replay: boolean;
  purchase: Purchase;
  proposal: TransactionProposal | null;
  payment: PaymentInfo | null;
  payment_state: PaymentState | null;
  /** The contract's funding (card_last4, state). */
  funding: Funding;
  approval_url: string | null;
  resolution: Resolution | null;
  next_action: NextAction;
  review_url: string;
}

/** API-only: GET /evidence/{purchase_id}. */
export interface EvidenceBundle {
  purchase: Purchase;
  status: PurchaseStatus;
  summary: string;
  contract: Contract | Record<string, unknown>;
  contract_verification: ContractVerification;
  proposal: TransactionProposal | null;
  proposal_raw_payload: unknown;
  decision: ValidationDecision | null;
  credential: CredentialSummary | null;
  ledger_intact: boolean;
  events: EvidenceEvent[];
}

/** API-only: GET /health. */
export interface Health {
  status: string;
  database: string;
  payment_mode: "stub" | "link_test";
  payment_label: string;
  /** agent_visible: the agent collects the released card and pays. executor: Handshake pays at authorization. */
  credential_mode: "agent_visible" | "executor";
  compiler_mode: "fixture" | "openai";
}

/** API-only: POST /auth/demo-login. */
export interface DemoLogin {
  token: string;
  token_type: "bearer";
  email: string;
  role: "user";
  /** Unix time in SECONDS (unlike the ISO strings elsewhere in the API). */
  expires_at: number;
  demo_auth: true;
}

/** API-only: an in-progress "connect your Link account" login (POST /link/connect, GET /link/status .login). */
export interface LinkLogin {
  state: "starting" | "pending" | "connected" | "failed" | "expired";
  /** Open this and approve in the Link app. */
  verification_url?: string | null;
  /** Link shows the same phrase, so the user can check it's their login. */
  phrase?: string | null;
  error?: string | null;
  provider_label: string;
  simulated: boolean;
}

/** API-only: GET /link/status (and POST /link/disconnect). The Link access token is never returned. */
export interface LinkStatus {
  connected: boolean;
  simulated: boolean;
  provider_label: string;
  login: LinkLogin | null;
}

/** API-only: GET /oauth/device?user_code= (the /connect approval screen). */
export interface DeviceAuthorization {
  user_code: string;
  client_id: string;
  client_name: string | null;
  status: "pending" | "approved" | "denied" | "consumed" | (string & {});
  expires_at: string;
  permissions: string[];
  never: string[];
}

/** API-only: POST /oauth/device/approve and /deny. */
export interface DeviceDecision {
  user_code: string;
  status: string;
  client_id: string;
}

/** API-only: PATCH /drafts/{id} body. Send only the keys that changed; unknown keys are refused. */
export interface DraftPatch {
  goal?: string;
  target?: number | null;
  hard_cap_all_in?: number;
  max_shipping?: number | null;
  deliver_by?: string | null;
  constraints?: Constraint[];
}

/** API-only: the one error shape every route uses. */
export interface ApiErrorBody {
  error: string;
  message: string;
  details?: Record<string, unknown>;
  error_description?: string;
}
