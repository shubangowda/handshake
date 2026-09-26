
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


# ============================================================
# Helpers
# ============================================================


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# Enums
# ============================================================


class ConstraintOperator(str, Enum):
    EQ = "eq"
    NEQ = "neq"

    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"

    IN = "in"
    NOT_IN = "not_in"

    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"

    BEFORE = "before"
    AFTER = "after"


class ConstraintSeverity(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    ESCALATING = "escalating"


class ValueSource(str, Enum):
    USER = "user"
    INFERRED = "inferred"
    DEFAULT = "default"


class ContractStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ConstraintVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"


class PurchaseStatus(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    AUTHORIZED = "authorized"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    COMPLETED = "completed"
    FAILED = "failed"


class CredentialStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ProductCondition(str, Enum):
    NEW = "new"
    USED = "used"
    REFURBISHED = "refurbished"
    OPEN_BOX = "open_box"


class SellerRequirement(str, Enum):
    ANY = "any"
    FIRST_PARTY = "first_party"
    VERIFIED = "verified"
    FIRST_PARTY_OR_VERIFIED = "first_party_or_verified"


class MerchantPolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


# ============================================================
# Generic Constraint System
# ============================================================


ConstraintField = Literal[
    # Product identity
    "category",
    "brand",
    "model",
    "product_name",
    "condition",
    "color",
    "size",
    "quantity",

    # Electronics
    "screen_size_inches",
    "display_type",
    "refresh_rate_hz",
    "storage_gb",
    "memory_gb",

    # Clothing
    "material",
    "gender",
    "fit",

    # Food
    "food_size",
    "toppings",
    "dietary",

    # Tickets
    "event_name",
    "ticket_quantity",
    "seats_together",
    "section",

    # Merchant
    "merchant_name",
    "seller_of_record",

    # Terms
    "return_days",
    "subscription",
    "membership",
    "addons",

    # Delivery
    "delivery_date",

    # Pricing
    "item_price",
    "shipping",
    "fees",
    "tax",
    "total_price",
]

SUPPORTED_FIELDS = set(get_args(ConstraintField))


# Explicit types keep constraint values compatible with Structured Outputs.
ConstraintScalar = str | int | float | bool
ConstraintValue = ConstraintScalar | list[ConstraintScalar] | None


class Constraint(BaseModel):
    """
    Generic category-specific constraint.

    Example:
    {
        "field": "refresh_rate_hz",
        "operator": "gte",
        "value": 120,
        "severity": "hard",
        "source": "user"
    }
    """

    field: ConstraintField
    operator: ConstraintOperator
    value: ConstraintValue

    severity: ConstraintSeverity = ConstraintSeverity.HARD
    source: ValueSource = ValueSource.USER

    description: str | None = None

# ============================================================
# Spend
# ============================================================


class SpendPolicy(BaseModel):
    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
    )

    target: float | None = Field(
        default=None,
        ge=0,
    )

    hard_cap_all_in: float = Field(
        gt=0,
    )

    includes: list[
        Literal[
            "item",
            "tax",
            "shipping",
            "fees",
        ]
    ] = Field(
        default_factory=lambda: [
            "item",
            "tax",
            "shipping",
            "fees",
        ]
    )

    target_source: ValueSource = ValueSource.USER
    hard_cap_source: ValueSource = ValueSource.USER

    @model_validator(mode="after")
    def validate_spend(self):
        if (
            self.target is not None
            and self.target > self.hard_cap_all_in
        ):
            raise ValueError(
                "target cannot exceed hard_cap_all_in"
            )

        required = {
            "item",
            "tax",
            "shipping",
            "fees",
        }

        if not required.issubset(set(self.includes)):
            raise ValueError(
                "hard_cap_all_in must include item, tax, shipping, and fees"
            )

        return self


# ============================================================
# Delivery
# ============================================================


class DeliveryPolicy(BaseModel):
    deliver_by: datetime | None = None

    max_shipping: float | None = Field(
        default=None,
        ge=0,
    )

    require_verified_estimate: bool = False

    deliver_by_source: ValueSource = ValueSource.USER
    max_shipping_source: ValueSource = ValueSource.DEFAULT


# ============================================================
# Terms
# ============================================================


class TermsPolicy(BaseModel):
    no_subscription: bool = True
    no_membership: bool = True
    no_addons: bool = True

    min_return_days: int | None = Field(
        default=None,
        ge=0,
    )

    no_forced_account_creation: bool = False


# ============================================================
# Merchant Restrictions
# ============================================================


class MerchantPolicy(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    seller_requirement: SellerRequirement = (
        SellerRequirement.FIRST_PARTY_OR_VERIFIED
    )

    new_merchant: MerchantPolicyAction = (
        MerchantPolicyAction.ESCALATE
    )

    @model_validator(mode="after")
    def validate_merchants(self):
        overlap = set(self.allow) & set(self.deny)

        if overlap:
            raise ValueError(
                f"Merchants cannot appear in both allow and deny: {overlap}"
            )

        return self


# ============================================================
# Substitutions
# ============================================================


class SubstitutionPolicy(BaseModel):
    allowed: bool = False

    same_model_other_color: bool = False
    same_brand: bool = False

    max_price_delta: float | None = Field(
        default=None,
        ge=0,
    )


# ============================================================
# Data Sharing
# ============================================================


class DataSharingPolicy(BaseModel):
    allowed_fields: list[
        Literal[
            "name",
            "email",
            "phone",
            "shipping_address",
            "billing_address",
        ]
    ] = Field(default_factory=list)

    merchant_may_contact: bool = False


# ============================================================
# Payment Instrument
# ============================================================


class InstrumentPolicy(BaseModel):
    type: Literal[
        "virtual_single_use",
        "network_token",
        "stub",
    ] = "virtual_single_use"

    fixed_by_contract: bool = True


# ============================================================
# Contract Draft
# ============================================================


class ContractDraft(BaseModel):
    """
    Generated by the Handshake compiler.

    This object has NOT yet been signed by the user.
    """

    id: str = Field(
        default_factory=lambda: generate_id("draft")
    )

    hil_version: str = "0.2"

    goal: str = Field(min_length=1)

    category: str | None = None

    spend: SpendPolicy

    delivery: DeliveryPolicy | None = None

    terms: TermsPolicy = Field(
        default_factory=TermsPolicy
    )

    merchants: MerchantPolicy = Field(
        default_factory=MerchantPolicy
    )

    substitution: SubstitutionPolicy = Field(
        default_factory=SubstitutionPolicy
    )

    data_sharing: DataSharingPolicy = Field(
        default_factory=DataSharingPolicy
    )

    instrument: InstrumentPolicy = Field(
        default_factory=InstrumentPolicy
    )

    constraints: list[Constraint] = Field(
        default_factory=list
    )

    escalate_if: list[str] = Field(
        default_factory=lambda: [
            "any_unverifiable_hard"
        ]
    )

    selection_disclosure_required: bool = True

    single_use: bool = True
    revocable: bool = True

    expires_at: datetime | None = None

    created_at: datetime = Field(
        default_factory=utc_now
    )

    @model_validator(mode="after")
    def validate_expiration(self):
        if (
            self.expires_at is not None
            and self.expires_at <= self.created_at
        ):
            raise ValueError(
                "expires_at must be after created_at"
            )

        return self


# ============================================================
# Signed Contract
# ============================================================


class Contract(BaseModel):
    """
    Canonical user-authorized contract.
    """

    id: str = Field(
        default_factory=lambda: generate_id("contract")
    )

    hil_version: str = "0.2"

    status: ContractStatus = ContractStatus.ACTIVE

    goal: str
    category: str | None = None

    spend: SpendPolicy

    delivery: DeliveryPolicy | None = None
    terms: TermsPolicy
    merchants: MerchantPolicy
    substitution: SubstitutionPolicy
    data_sharing: DataSharingPolicy
    instrument: InstrumentPolicy

    constraints: list[Constraint] = Field(
        default_factory=list
    )

    escalate_if: list[str] = Field(
        default_factory=list
    )

    selection_disclosure_required: bool = True

    single_use: bool = True
    revocable: bool = True

    agent_key: str | None = None

    created_at: datetime
    signed_at: datetime

    expires_at: datetime | None = None

    contract_hash: str
    signature: str

    previous_contract_id: str | None = None

    @model_validator(mode="after")
    def validate_signed_contract(self):
        if self.signed_at < self.created_at:
            raise ValueError(
                "signed_at cannot be before created_at"
            )

        if (
            self.expires_at is not None
            and self.expires_at <= self.signed_at
        ):
            raise ValueError(
                "expires_at must be after signed_at"
            )

        return self


# ============================================================
# Merchant / Seller
# ============================================================


class MerchantIdentity(BaseModel):
    name: str

    domain: str | None = None

    merchant_id: str | None = None

    seller_of_record: str | None = None

    is_first_party: bool | None = None

    is_verified: bool | None = None


# ============================================================
# Line Items
# ============================================================


class LineItem(BaseModel):
    name: str

    category: str | None = None
    brand: str | None = None
    model: str | None = None

    gtin: str | None = None
    mpn: str | None = None

    condition: ProductCondition | str | None = None

    quantity: int = Field(
        default=1,
        ge=1,
    )

    unit_price: float = Field(
        ge=0,
    )

    attributes: dict[str, Any] = Field(
        default_factory=dict
    )

    @property
    def subtotal(self) -> float:
        return self.unit_price * self.quantity


# ============================================================
# Delivery Proposal
# ============================================================


class DeliveryProposal(BaseModel):
    promised_by: datetime | None = None

    carrier: str | None = None

    tracking_available: bool = False

    verified: bool = False

    evidence: str | None = None


# ============================================================
# Return Terms
# ============================================================


class ReturnTerms(BaseModel):
    returnable: bool | None = None

    return_window_days: int | None = Field(
        default=None,
        ge=0,
    )

    restocking_fee: float | None = Field(
        default=None,
        ge=0,
    )

    evidence: str | None = None


# ============================================================
# Recurring Billing
# ============================================================


class RecurringBilling(BaseModel):
    detected: bool = False

    interval: str | None = None

    amount: float | None = Field(
        default=None,
        ge=0,
    )

    description: str | None = None


# ============================================================
# Transaction Proposal
# ============================================================


class TransactionProposal(BaseModel):
    """
    Structured facts extracted independently from checkout.

    The shopping agent should not be trusted as the authoritative
    source for these values.
    """

    id: str = Field(
        default_factory=lambda: generate_id("proposal")
    )

    contract_id: str

    merchant: MerchantIdentity

    line_items: list[LineItem] = Field(
        min_length=1
    )

    item_subtotal: float = Field(
        ge=0,
    )

    tax: float = Field(
        default=0,
        ge=0,
    )

    shipping: float = Field(
        default=0,
        ge=0,
    )

    fees: float = Field(
        default=0,
        ge=0,
    )

    discounts: float = Field(
        default=0,
        ge=0,
    )

    total: float = Field(
        ge=0,
    )

    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
    )

    recurring_billing: RecurringBilling = Field(
        default_factory=RecurringBilling
    )

    addons_detected: bool = False
    membership_detected: bool = False

    delivery: DeliveryProposal | None = None

    return_terms: ReturnTerms | None = None

    extracted_attributes: dict[str, Any] = Field(
        default_factory=dict
    )

    source_url: str | None = None

    extracted_at: datetime = Field(
        default_factory=utc_now
    )

    extractor_ids: list[str] = Field(
        default_factory=list
    )

    extractors_agreed: bool = True

    evidence: dict[str, Any] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_total(self):
        calculated = (
            self.item_subtotal
            + self.tax
            + self.shipping
            + self.fees
            - self.discounts
        )

        tolerance = 0.02

        if abs(calculated - self.total) > tolerance:
            raise ValueError(
                f"Transaction total mismatch. "
                f"Expected {calculated:.2f}, received {self.total:.2f}"
            )

        return self


# ============================================================
# Validation Results
# ============================================================


class ConstraintResult(BaseModel):
    constraint: str

    verdict: ConstraintVerdict

    expected: Any | None = None
    actual: Any | None = None

    reason: str

    severity: ConstraintSeverity = (
        ConstraintSeverity.HARD
    )

    evidence: dict[str, Any] | None = None


class ValidationDecision(BaseModel):
    id: str = Field(
        default_factory=lambda: generate_id("decision")
    )

    contract_id: str
    proposal_id: str

    verdict: ConstraintVerdict

    results: list[ConstraintResult]

    evaluated_at: datetime = Field(
        default_factory=utc_now
    )

    @model_validator(mode="after")
    def validate_overall_verdict(self):
        hard_results = [
            result
            for result in self.results
            if result.severity == ConstraintSeverity.HARD
        ]

        has_fail = any(
            result.verdict == ConstraintVerdict.FAIL
            for result in hard_results
        )

        has_unverifiable = any(
            result.verdict == ConstraintVerdict.UNVERIFIABLE
            for result in hard_results
        )

        expected: ConstraintVerdict

        if has_fail:
            expected = ConstraintVerdict.FAIL

        elif has_unverifiable:
            expected = ConstraintVerdict.UNVERIFIABLE

        else:
            expected = ConstraintVerdict.PASS

        if self.verdict != expected:
            raise ValueError(
                f"Decision verdict should be {expected.value}"
            )

        return self


# ============================================================
# Credential
# ============================================================


class Credential(BaseModel):
    """
    Represents authorization issued by Handshake.

    Do not expose raw payment credentials to the shopping agent.
    """

    id: str = Field(
        default_factory=lambda: generate_id("cred")
    )

    contract_id: str
    proposal_id: str

    merchant_name: str

    merchant_id: str | None = None

    max_amount: float = Field(
        gt=0,
    )

    currency: str = "USD"

    single_use: bool = True

    status: CredentialStatus = (
        CredentialStatus.CREATED
    )

    created_at: datetime = Field(
        default_factory=utc_now
    )

    expires_at: datetime

    provider_reference: str | None = None

    @model_validator(mode="after")
    def validate_credential(self):
        if self.expires_at <= self.created_at:
            raise ValueError(
                "credential expiration must be in the future"
            )

        return self


# ============================================================
# Purchase
# ============================================================


class Purchase(BaseModel):
    id: str = Field(
        default_factory=lambda: generate_id("purchase")
    )

    contract_id: str

    proposal_id: str | None = None
    decision_id: str | None = None
    credential_id: str | None = None

    status: PurchaseStatus = (
        PurchaseStatus.PENDING
    )

    merchant_name: str | None = None

    authorized_amount: float | None = Field(
        default=None,
        ge=0,
    )

    charged_amount: float | None = Field(
        default=None,
        ge=0,
    )

    currency: str = "USD"

    created_at: datetime = Field(
        default_factory=utc_now
    )

    completed_at: datetime | None = None

    error: str | None = None


# ============================================================
# Evidence Ledger
# ============================================================


class EvidenceEventType(str, Enum):
    CONTRACT_CREATED = "contract_created"
    CONTRACT_SIGNED = "contract_signed"
    CONTRACT_REVOKED = "contract_revoked"

    SHOPPING_STARTED = "shopping_started"

    PROPOSAL_CREATED = "proposal_created"

    VALIDATION_STARTED = "validation_started"
    VALIDATION_COMPLETED = "validation_completed"

    PURCHASE_BLOCKED = "purchase_blocked"
    PURCHASE_ESCALATED = "purchase_escalated"
    PURCHASE_AUTHORIZED = "purchase_authorized"

    CREDENTIAL_CREATED = "credential_created"
    CREDENTIAL_USED = "credential_used"

    PAYMENT_COMPLETED = "payment_completed"
    PAYMENT_MISMATCH = "payment_mismatch"


class EvidenceEvent(BaseModel):
    id: str = Field(
        default_factory=lambda: generate_id("event")
    )

    purchase_id: str | None = None
    contract_id: str

    event_type: EvidenceEventType

    timestamp: datetime = Field(
        default_factory=utc_now
    )

    message: str

    data: dict[str, Any] = Field(
        default_factory=dict
    )


# ============================================================
# Selection Report
# ============================================================


class CandidateProduct(BaseModel):
    name: str

    merchant: str

    price: float = Field(
        ge=0
    )

    currency: str = "USD"

    sponsored: bool = False
    affiliate: bool = False

    url: str | None = None

    attributes: dict[str, Any] = Field(
        default_factory=dict
    )


class SelectionReport(BaseModel):
    contract_id: str

    selected_candidate: CandidateProduct

    candidates: list[CandidateProduct]

    reasoning_summary: str

    created_at: datetime = Field(
        default_factory=utc_now
    )


# ============================================================
# MCP / API Request Models
# ============================================================


class CreateContractDraftRequest(BaseModel):
    intent: str = Field(
        min_length=1
    )


class SignContractRequest(BaseModel):
    draft_id: str

    agent_key: str | None = None

    signature: str | None = None


class PurchaseRequest(BaseModel):
    contract_id: str

    checkout_url: str

    selection_report: SelectionReport | None = None


class PurchaseStatusResponse(BaseModel):
    purchase_id: str

    status: PurchaseStatus

    decision: ValidationDecision | None = None


# ============================================================
# Compiler Output
# ============================================================


class CompilerOutput(BaseModel):
    """
    Exact object the contract-compiler LLM should return.
    """

    draft: ContractDraft

    assumptions: list[str] = Field(
        default_factory=list
    )

    clarifications_needed: list[str] = Field(
        default_factory=list
    )

    compiler_notes: list[str] = Field(
        default_factory=list
    )
