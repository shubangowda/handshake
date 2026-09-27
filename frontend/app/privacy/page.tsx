import type { Metadata } from "next";
import Link from "next/link";
import { ContactLine, LegalPage } from "@/components/handshake/legal-page";

export const metadata: Metadata = {
  title: "Privacy Policy · Handshake",
  description: "What the Handshake prototype collects, why, who it is shared with, and how to get it deleted.",
};

/**
 * Public (no login). Google's sign-in review and Muse's connector directory link here, so it must be complete
 * and true: when the app's data handling changes, change this page (and LEGAL_UPDATED) in the same commit.
 */
export default function PrivacyPage() {
  return (
    <LegalPage
      title="Privacy Policy"
      intro="Handshake lets an AI shopping agent buy things for you only within limits you sign. It is a prototype that handles test-mode payments only: no real money moves and no real card is ever charged. This page lists exactly what it collects and where that data goes."
    >
      <section>
        <h2>Information from your Google account</h2>
        <p>
          When you sign in with Google, Handshake receives your <b>email address</b> (and Google&apos;s confirmation that it is verified). It asks
          only for the basic <code>openid</code>, <code>email</code>, and <code>profile</code> permissions, and it uses only your email address,
          to identify your account and label what belongs to you. It does not read your Gmail, contacts, files, or anything else in your Google
          account, and it never receives your Google password.
        </p>
        <p>
          Handshake&apos;s use of information received from Google APIs adheres to the{" "}
          <a className="underline underline-offset-4" href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>,
          including the Limited Use requirements.
        </p>
      </section>
      <section>
        <h2>What Handshake stores</h2>
        <ul>
          <li><b>Your contracts</b>: what you asked your agent to buy, the drafts, and the terms you signed.</li>
          <li><b>Purchase attempts</b> your agent made under those contracts, including the checkout details Handshake read from the store and its decision.</li>
          <li><b>Evidence</b>: a tamper-evident timeline of every step (signing, funding, checks, approvals, payment), so you can see why something was allowed or blocked.</li>
          <li><b>Connected agents</b>: which agents and agent keys you connected, when, when they were last used, and whether you revoked them.</li>
          <li><b>Your Stripe Link connection</b>, only if you choose to connect your own Stripe Link account: the Link sign-in that Link&apos;s tool saves, kept in a private folder for your account on Handshake&apos;s server so Handshake can request test-mode cards for you. Disconnecting removes it.</li>
        </ul>
      </section>
      <section>
        <h2>Payment cards</h2>
        <p>
          Cards are single-use Stripe Link <b>test-mode</b> cards (if you connected Link) or simulated ones. A funded card is stored encrypted on its
          contract, released once and only for a checkout Handshake approved, and the stored copy is deleted as it is released. Revoking a contract
          deletes its card too. Handshake never asks for or stores your real card details.
        </p>
      </section>
      <section>
        <h2>Who else receives data</h2>
        <ul>
          <li><b>The agents you connect.</b> An agent you approve (for example Muse, operated by Meta) can read your contracts and purchases, and receives the single-use test card for a checkout Handshake approved. What it does with that is covered by that agent&apos;s own privacy policy. You can disconnect any agent on the Agents page.</li>
          <li><b>Stripe</b>, only if you connect Stripe Link: the amount, store, and a short description of each test-mode card request go to your Link account so you can approve it.</li>
          <li><b>OpenAI</b>, only if the operator turns on the AI contract drafter: the text of your shopping request is sent to OpenAI to draft the contract. Otherwise drafts are made on Handshake&apos;s own server.</li>
          <li><b>Google</b>, for sign-in, under Google&apos;s own privacy policy.</li>
          <li><b>Fly.io</b>, which hosts Handshake. Like any web server, it keeps request logs, which include IP addresses.</li>
        </ul>
        <p>Handshake does not sell your data, does not use it for advertising, and has no ads or third-party trackers.</p>
      </section>
      <section>
        <h2>In your browser</h2>
        <p>
          Handshake keeps your sign-in token in your browser&apos;s local storage so you stay signed in; signing out removes it. It sets no
          tracking cookies. The Sign in with Google button is provided by Google and may use Google&apos;s own cookies.
        </p>
      </section>
      <section>
        <h2>Keeping and deleting your data</h2>
        <p>
          Your data is kept while this prototype runs, and it may be reset without notice. You can revoke contracts and agents, and disconnect
          Stripe Link, yourself at any time. To have your account and all its data deleted, or for any question about this policy, email{" "}
          <ContactLine />.
        </p>
      </section>
      <section>
        <h2>Children</h2>
        <p>Handshake is not intended for anyone under 18.</p>
      </section>
      <section>
        <h2>Changes</h2>
        <p>
          If this policy changes, the new version will be posted here with a new date. See also the{" "}
          <Link className="underline underline-offset-4" href="/terms">Terms of Service</Link>.
        </p>
      </section>
    </LegalPage>
  );
}
