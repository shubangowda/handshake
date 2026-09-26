# Handshake: Permission and Protection for AI Shopping

Handshake is a proposed authorization layer for purchases made by AI agents. It turns a person's shopping request into clear, approved rules, checks the actual purchase against those rules, and releases payment only when the purchase is authorized.

The idea is simple: **the user sets the boundaries, the agent shops within them, and an independent payment controller enforces them.**

An AI agent can search for products, compare options, and prepare a checkout. It does not get unrestricted permission to spend money or decide what the user has agreed to. Handshake sits between the agent's proposed purchase and payment, checking that the item, seller, total price, delivery, and terms match the user's instructions.

This overview brings together the Handshake v0.2 specification, including its added protections and post-purchase workflow. It describes the intended product, rather than claiming that every capability is already built or supported by every merchant.

## Why Handshake exists

“Buy me running shoes for around $120” sounds straightforward, but leaves important questions unanswered. Does the budget include shipping and tax? Is another color acceptable? Can the agent buy from an unfamiliar marketplace seller? What if the discount requires a subscription?

Even when an agent understands the request, a checkout can change. Fees may appear late, a different seller may fulfill the order, or the promised delivery may be difficult to verify. A merchant page may also contain misleading instructions intended to influence the agent.

Handshake makes the user's permission explicit and checks the final purchase before money is released. Its purpose is to prevent unauthorized purchases while letting the agent handle routine shopping work.

## Who does what

| Participant | Responsibility |
| --- | --- |
| **User** | Defines the goal, approves spending rules, and decides on exceptions. |
| **Shopping agent** | Finds options, explains its selection, and prepares the purchase. |
| **Handshake's independent checker** | Compares the proposed purchase with the approved rules and flags violations or uncertainty. |
| **Payment controller** | Provides a restricted payment credential for an approved purchase without exposing it to the agent. |
| **Purchase record** | Preserves approvals, checks, receipts, and follow-up evidence. |
| **Merchant and seller** | Provide the product, checkout terms, fulfillment, and applicable after-sales service. |

The separation matters: the agent proposing a purchase cannot approve its own exceptions or rewrite the user's rules. Merchant content is information to evaluate, never permission to change those rules.

## The complete workflow

### 1. The user describes what they want

The user starts with a normal request, such as:

> “Find new running shoes in size 10, around $120, delivered by next Friday. I want at least 30 days to return them, and no subscriptions or extras.”

Handshake turns this into a draft purchase agreement, called a **contract**. Here, a contract means a recorded authorization defining what the agent may do; it is not a claim that Handshake resolves every legal question about the purchase.

Handshake asks about missing details that could materially change the outcome, such as shoe size, whether the delivery date is firm, or how much budget flexibility the user allows. Other details can use profile defaults, but inferred choices must be visible during review.

### 2. The user reviews and approves the boundaries

The review is written in plain language. It covers:

- **Product requirements:** item type, size, condition, and any required ratings or review counts.
- **Spending limits:** the preferred price and a maximum total including tax, shipping, and fees.
- **Substitutions:** whether a different color or other variation is allowed, and at what additional cost.
- **Delivery:** the destination, deadline, shipping limit, and required confidence in the delivery promise.
- **Purchase terms:** return window and whether subscriptions, memberships, extras, or account creation are permitted.
- **Seller rules:** allowed or excluded merchants, acceptable marketplace sellers, and when an unfamiliar merchant requires review.
- **Privacy and payment:** which personal details may be shared, whether merchant contact is allowed, and the payment method to use.
- **Permission limits:** how long the authorization lasts, which agent may use it, and whether it covers one purchase or a standing budget.

Handshake highlights anything it inferred. If it suggests a $135 maximum for a $120 target, the user sees that difference before approval. Two short examples show a purchase that would be allowed and one that would be blocked.

The rules distinguish between firm requirements, preferences used to rank choices, and situations that require another user decision. A small repeat purchase may use a standing budget with a one-tap confirmation; a large or unusual purchase receives fuller review and a secure signature, such as a passkey approval.

Once approved, the contract cannot be silently changed. An edit creates a new version, shows what changed, and requires renewed approval.

### 3. The agent shops and explains its choice

The agent searches, compares products, and assembles a cart within the approved boundaries.

It must also provide a **Selection Report** explaining the candidates considered, the criteria used, and any sponsored or affiliate relationships. This gives the user visibility into why an option was chosen. A missing report triggers review.

The agent identifies itself to merchants as acting on a user's behalf, without including personal details in that identity declaration. It respects merchant access and rate policies. This makes its role clearer, although merchants may still refuse automated shopping.

### 4. Handshake checks the actual checkout

Before payment, the independent checker examines what the merchant is actually asking the user to buy. It checks the product and condition, the merchant and actual seller, all charges, recurring-payment terms, delivery promises, return policy, and final payable total.

Where a merchant provides reliable structured checkout information, Handshake uses it. For ordinary checkout pages, it gathers the necessary information from the checkout itself. Two independent readings must agree; conflicting information is treated as uncertain.

Additional checks help confirm product identity, compare the price with other sources, identify suspicious lookalike merchant addresses, and verify marketplace sellers. The checkout is read again after the final total is calculated so that late changes are included.

### 5. The purchase passes, is blocked, or returns to the user

Each relevant requirement receives one of three outcomes:

| Outcome | What it means | What happens next |
| --- | --- | --- |
| **Pass** | The purchase is verified to meet the requirement. | It can continue if the other required checks also pass. |
| **Fail** | The purchase violates a firm requirement. | Payment is blocked and the reason is recorded. |
| **Cannot verify** | There is insufficient or conflicting evidence about a firm requirement. | Payment pauses and the user receives the evidence for a decision. |

Preferences help rank options; they do not automatically block a purchase. Separate review triggers can still pause a purchase that is below the maximum price—for example, an unfamiliar merchant or a price sufficiently above the preferred target.

Uncertainty never counts as approval. If the delivery promise cannot be confirmed, Handshake explains the gap. The user can choose another option or explicitly approve revised terms through a new contract version.

### 6. Payment is released with its own limits

Once the required checks pass and any review triggers are resolved, the payment controller supplies a single-use virtual card or payment token directly to checkout. The agent never sees the payment details.

The credential has its own spending cap, merchant restriction, and expiration. These restrictions provide a second layer of protection if an earlier check fails. The agent also cannot switch the approved payment method to suit its own incentives.

Handshake limits the personal information released through its controlled purchase flow to the fields and recipient the user authorized. Permission for merchant follow-up contact is a separate choice.

### 7. Handshake checks the charge and follows the order

After payment authorization, Handshake compares the processor's recorded amount with the approved amount. The design calls for an automatic void or dispute process if they differ; the available remedy and its outcome depend on payment-provider support and agreements.

The purchase record connects the user's authorization, the proposed checkout, the checking decision, payment references, and subsequent order events. Handshake then monitors delivery and refunds. If a delivery commitment is missed, the recorded evidence can support a claim.

This record helps explain what was approved and what happened. It can be exported as a dispute packet, but it does not guarantee that an issuer or merchant will accept a claim.

### 8. Returns, cancellations, and revocation remain under user control

A return or cancellation uses a separate authorization defining the follow-up action the agent may take. Refunds are tracked in the purchase record.

The user can also revoke an active contract and its payment credential using a stop control. This withdraws permission for further use; it should not be confused with automatically reversing an already completed purchase.

For repeat shopping, standing permissions and rolling budgets can reduce repeated approvals while keeping spending boundaries and exception handling in place.

## An example from request to receipt

Suppose the user approves new size-10 running shoes, a $120 target, a $135 all-in maximum, delivery by next Friday, at least 30 days for returns, and no subscriptions. A different color of the same model is acceptable within the approved substitution allowance.

The agent compares several options and selects a pair priced at $115. Shipping is $5 and tax is $9, bringing the checkout total to $129. Its Selection Report explains the choice and identifies any commercial relationships.

Handshake verifies the exact shoes, seller, delivery evidence, return window, and absence of recurring charges. Assuming all other rules and review triggers are satisfied, it releases a restricted payment credential, checks that the authorized charge is $129, and follows the order through delivery.

If a late fee pushes the total above $135, payment is blocked. If delivery by Friday cannot be verified, the purchase pauses for the user. If the user later wants to return the shoes, a separate return authorization governs that action and the refund is tracked.

## How Handshake fits into shopping

Handshake is intended to work with shopping agents, merchants, and payment services. It supplies the shared permission and enforcement layer between them.

Where a commerce platform already supports agent purchases and signed checkout information, Handshake can translate the user's authorization into that platform's format. It also aims to support ordinary online checkouts, although site changes or missing information may require the purchase to pause.

## What Handshake can and cannot promise

Handshake is designed to keep purchases within the authority the user granted, make exceptions visible, and preserve evidence when something goes wrong. It reduces reliance on the shopping agent's judgment by checking its proposed actions independently and restricting payment separately.

It cannot guarantee that a permitted product is the best choice, that a merchant's claims are true, or that an item will feel right when it arrives. Some claims will remain unverifiable. Merchants can block agents, checkout changes can interrupt verification, and users can approve rules that do not reflect what they really wanted. Liability and dispute outcomes also require agreements beyond Handshake itself.

## Planned rollout

The specification proposes building the core authorization, review, and purchase-checking flow first, then testing it against hidden subscriptions, late fees, product changes, misleading merchant content, and other failure cases. Restricted real payments and charge reconciliation follow, with broader commerce integrations, standing budgets, returns, and dispute exports added afterward.

Success means blocking unauthorized purchases while keeping unnecessary interruptions low. The important measures include incorrect approvals, unnecessary blocks, requests for user review, time to approve a contract, and whether purchase evidence is useful in disputes.

---

Source: [Handshake Spec v0.2](sources/Handshake-Specs.txt).