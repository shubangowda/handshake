import type { Metadata } from "next";
import Link from "next/link";
import { ContactLine, LegalPage } from "@/components/handshake/legal-page";

export const metadata: Metadata = {
  title: "Terms · Handshake",
  description: "The terms for using the Handshake prototype: test-mode payments only, provided as is.",
};

/** Public (no login), like /privacy. */
export default function TermsPage() {
  return (
    <LegalPage title="Terms" intro="By using Handshake you agree to these short terms. Handshake is a prototype for letting an AI shopping agent buy things only within limits you sign.">
      <section>
        <h2>A prototype, in test mode</h2>
        <ul>
          <li>All payments are Stripe Link <b>test-mode</b> or simulated. No real money moves, no real card is charged, and nothing is actually bought or shipped through Handshake.</li>
          <li>Don&apos;t enter real card numbers or other payment details anywhere in Handshake.</li>
          <li>Handshake is provided as is, without warranties. It may change, go offline, or have its data reset at any time.</li>
        </ul>
      </section>
      <section>
        <h2>You and your agents</h2>
        <ul>
          <li>An agent you connect can draft contracts for you to review, request purchases under contracts you signed, and collect a funded card only for a checkout Handshake approved.</li>
          <li>An agent can never sign a contract, approve an exception, or change your limits. Only you can.</li>
          <li>You&apos;re responsible for the agents and agent keys you connect. Keep agent keys secret, and revoke any agent you no longer trust from the Agents page.</li>
        </ul>
      </section>
      <section>
        <h2>Fair use</h2>
        <p>Don&apos;t use Handshake to break the law, attack the service, or get around its checks. Accounts that do may be removed.</p>
      </section>
      <section>
        <h2>Contact</h2>
        <p>Questions about these terms: <ContactLine />. How your data is handled is described in the <Link className="underline underline-offset-4" href="/privacy">privacy notice</Link>.</p>
      </section>
    </LegalPage>
  );
}
