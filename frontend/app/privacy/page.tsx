import type { Metadata } from "next";
import { ContactLine, LegalPage } from "@/components/handshake/legal-page";

export const metadata: Metadata = {
  title: "Privacy · Handshake",
  description: "What the Handshake prototype stores, why, and how to get it deleted.",
};

/** Public (no login): agent directories link here before anyone has an account. Keep it short and true. */
export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy" intro="Handshake is a prototype. It only ever handles test-mode payments, so no real money moves and no real card is ever charged. Here is exactly what it keeps.">
      <section>
        <h2>What Handshake stores</h2>
        <ul>
          <li><b>Your email address</b>, from Google sign-in (or the demo login where it&apos;s enabled). It identifies your account; Handshake doesn&apos;t use anything else from your Google profile.</li>
          <li><b>Your contracts</b>: what you asked your agent to buy, the drafts, and the terms you signed.</li>
          <li><b>Purchase attempts</b> your agent made under those contracts, including the checkout details Handshake read from the merchant and its decision.</li>
          <li><b>Evidence</b>: a tamper-evident timeline of every step (signing, funding, checks, approvals, payment) so you can see why something was allowed or blocked.</li>
          <li><b>Connected agents</b>: which agents or agent keys you connected, when, and when they were last used, so you can revoke them.</li>
        </ul>
      </section>
      <section>
        <h2>Payment cards</h2>
        <p>
          Cards are single-use Stripe Link <b>test-mode</b> cards, or simulated ones. A funded card is stored encrypted on its contract, released once only
          for a checkout Handshake approved, and the stored copy is wiped right after. Revoking a contract wipes its card too. Handshake never asks for or
          stores your real card details.
        </p>
      </section>
      <section>
        <h2>What Handshake doesn&apos;t do</h2>
        <ul>
          <li>It doesn&apos;t sell or share your data, and it has no ads or trackers.</li>
          <li>It doesn&apos;t make real charges. It&apos;s test mode only.</li>
        </ul>
      </section>
      <section>
        <h2>Deleting your data</h2>
        <p>
          You can revoke contracts and agents yourself at any time. Because this is a prototype, the data may also be reset without notice. To have your
          account and data deleted, or for any question, email <ContactLine />.
        </p>
      </section>
    </LegalPage>
  );
}
