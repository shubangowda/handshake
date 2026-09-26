"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/handshake/app-header";
import { PaymentPanel } from "@/components/handshake/payment-status";
import { Progression, PROGRESSION_STEPS, type Outcome } from "@/components/handshake/progression";
import { Timeline } from "@/components/handshake/timeline";
import { VerdictRows, showValue } from "@/components/handshake/verdict-rows";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { approvePurchase, declinePurchase, getContract, getEvidence, getHealth, getPurchase, resolveEscalation } from "@/lib/api";
import { formatTime, money } from "@/lib/format";
import { isTerminal } from "@/lib/status";
import type { ContractRecord, EvidenceEvent, Health, PurchaseDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ArrowLeft, Check, CircleHelp, Loader2, OctagonX, RotateCcw, ShieldAlert, ShoppingBag } from "lucide-react";

const POLL_MS = 2500;

/** Animates the progression, but never past what the real evidence shows (`reached`). */
function useStagedReveal(key: string | null, reached: number) {
  const [step, setStep] = useState(0);
  const [run, setRun] = useState(0);
  useEffect(() => {
    if (!key) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timers = Array.from({ length: PROGRESSION_STEPS }, (_, i) =>
      setTimeout(() => setStep(i + 1), reduced ? 0 : 650 * (i + 1)));
    return () => timers.forEach(clearTimeout);
  }, [key, run]);
  return { step: Math.min(step, reached), replay: () => { setStep(0); setRun((r) => r + 1); } };
}

/** How far the agent's attempt got, from the evidence events and the purchase itself. */
function reachedStep(d: PurchaseDetail, events: EvidenceEvent[]): number {
  const has = (t: EvidenceEvent["event_type"]) => events.some((e) => e.event_type === t && e.purchase_id === d.purchase.id);
  let n = 1; // The purchase exists, so the agent searched and picked a checkout.
  if (d.proposal || has("proposal_created")) n = 2;
  if (d.decision || has("validation_started")) n = 3;
  if (d.decision || has("validation_completed")) n = 4;
  if (d.status !== "pending" && d.status !== "validating") n = PROGRESSION_STEPS; // A verdict exists.
  return n;
}

/** Handshake's verdict on the checkout (not the payment). */
function outcomeOf(d: PurchaseDetail): Outcome {
  if (!d.decision) return d.status === "escalated" ? "unverifiable" : d.status === "blocked" || d.status === "failed" ? "fail" : "pass";
  // Passed the checks but refused at authorization (e.g. the contract was already used).
  if (d.decision.verdict === "pass" && (d.status === "blocked" || d.status === "failed") && !d.payment && !d.resolution) return "fail";
  return d.decision.verdict;
}

function Attempt({ detail }: { detail: PurchaseDetail }) {
  const p = detail.proposal!;
  const item = p.line_items[0];
  return (
    <div className="flex items-center gap-4 rounded-xl border bg-card p-4 sm:p-5">
      <span className="grid size-12 shrink-0 place-items-center rounded-lg bg-muted"><ShoppingBag className="size-5 text-muted-foreground" /></span>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Agent wants to purchase</p>
        <p className="truncate text-lg font-semibold">{item?.name ?? "Unnamed item"}{p.line_items.length > 1 && <span className="font-normal text-muted-foreground"> + {p.line_items.length - 1} more</span>}</p>
        <p className="truncate text-sm text-muted-foreground">from {p.merchant.name}{p.merchant.domain && ` · ${p.merchant.domain}`}</p>
      </div>
      <p className="text-right text-2xl font-semibold tabular-nums">{money(p.total, p.currency)}<span className="block text-xs font-normal text-muted-foreground">total</span></p>
    </div>
  );
}

function Blocked({ detail, cap }: { detail: PurchaseDetail; cap: number }) {
  const fails = detail.decision?.results.filter((r) => r.verdict === "fail" && r.severity !== "soft") ?? [];
  const total = detail.proposal?.total;
  const failed = detail.status === "failed";
  return (
    <div className="overflow-hidden rounded-2xl border-2 border-fail shadow-[0_0_0_6px_var(--color-fail-soft)] animate-in fade-in zoom-in-95 duration-300">
      <div className="flex items-center gap-3 bg-fail px-5 py-4 text-white">
        <OctagonX className="size-8 shrink-0" strokeWidth={2.5} />
        <div>
          <p className="text-2xl font-black tracking-tight sm:text-3xl">{failed ? "PURCHASE FAILED" : "PURCHASE BLOCKED"}</p>
          <p className="text-sm text-white/85">Nothing was charged and no card was issued.</p>
        </div>
      </div>
      <div className="grid gap-4 bg-card p-5 sm:grid-cols-2">
        {total != null && (
          <>
            <div>
              <p className="text-sm text-muted-foreground">Agent attempted</p>
              <p className={cn("text-4xl font-semibold tabular-nums", total > cap && "text-fail")}>{money(total)}</p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">You signed a maximum of</p>
              <p className="text-4xl font-semibold tabular-nums">{money(cap)}</p>
            </div>
          </>
        )}
        <ul className="space-y-1.5 sm:col-span-2">
          {fails.map((f) => (
            <li key={f.constraint} className="text-[15px]"><span className="font-semibold whitespace-nowrap text-fail">✕ {f.label}:</span> {f.reason}</li>
          ))}
          {!fails.length && <li className="text-[15px]">{detail.purchase.error ?? detail.summary}</li>}
        </ul>
      </div>
    </div>
  );
}

/**
 * An escalated purchase needs two separate consents: accepting the unverifiable checks here,
 * then approving the payment itself in Link. Neither one implies the other.
 */
function Escalated({ detail, health, cap, busy, onResolve, onApprovePayment, onDecline }: {
  detail: PurchaseDetail; health: Health | null; cap: number; busy: boolean;
  onResolve: (a: "approve" | "reject") => void; onApprovePayment: () => void; onDecline: () => void;
}) {
  const [confirm, setConfirm] = useState(false);
  const results = detail.decision?.results ?? [];
  const unknown = results.filter((r) => r.verdict === "unverifiable" && r.severity !== "soft");
  const res = detail.resolution;
  const deciding = detail.status === "escalated";
  const accepted = res?.action === "approve";
  const labelOf = (name: string) => results.find((r) => r.constraint === name)?.label ?? name;
  const title = deciding ? "HANDSHAKE NEEDS YOU" : accepted ? "EXCEPTION ACCEPTED" : res?.action === "reject" ? "REJECTED BY YOU" : "PURCHASE BLOCKED";
  const subtitle = deciding ? "Everything else checked out, but some things couldn't be confirmed. Two separate approvals are needed before anything is paid."
    : accepted ? "You accepted what couldn't be verified. It stays marked unverified, not passed."
    : "Nothing was charged and no card was issued.";
  return (
    <div className="overflow-hidden rounded-2xl border-2 border-warn animate-in fade-in zoom-in-95 duration-300">
      <div className="flex items-center gap-3 bg-warn px-5 py-4 text-white">
        <CircleHelp className="size-8 shrink-0" strokeWidth={2.5} />
        <div>
          <p className="text-2xl font-black tracking-tight sm:text-3xl">{title}</p>
          <p className="text-sm text-white/90">{subtitle}</p>
        </div>
      </div>
      <div className="space-y-4 bg-card p-5">
        <ol className="grid gap-2 rounded-lg bg-muted p-3 text-sm sm:grid-cols-2">
          <li><span className="font-semibold">1. Accept the exception in Handshake.</span> You take responsibility for the checks Handshake couldn&apos;t verify.</li>
          <li><span className="font-semibold">2. Then approve the payment in Link.</span> The payment provider asks you separately; nothing is paid without it.</li>
        </ol>

        <section className="space-y-3 rounded-xl border p-4">
          <h3 className="flex items-center gap-2 font-semibold">{res ? <Check className="size-4 text-pass" /> : <span className="grid size-5 place-items-center rounded-full bg-warn text-xs text-white">1</span>}Accept the exception in Handshake</h3>
          <div>
            <p className="text-sm text-muted-foreground">{deciding ? "You'd be accepting these unverifiable checks" : "Checks that couldn't be verified"}</p>
            {unknown.map((r) => <p key={r.constraint} className="text-lg font-semibold">{r.label}: <span className="font-normal">{r.reason}</span></p>)}
            {!unknown.length && <p className="text-sm">{detail.summary}</p>}
          </div>
          {detail.proposal && (
            <div className="flex flex-wrap gap-8">
              <div><p className="text-sm text-muted-foreground">Requested total</p><p className="text-2xl font-semibold tabular-nums">{money(detail.proposal.total, detail.proposal.currency)}</p></div>
              <div><p className="text-sm text-muted-foreground">Seller</p><p className="text-2xl font-semibold">{detail.proposal.merchant.name}</p></div>
            </div>
          )}
          {unknown.some((r) => r.evidence) && (
            <div className="rounded-lg bg-muted p-3 text-sm">
              <p className="mb-1 font-mono text-[11px] font-semibold tracking-widest text-muted-foreground uppercase">Evidence</p>
              {unknown.flatMap((r) => Object.entries(r.evidence ?? {}).map(([k, v]) => (
                <p key={r.constraint + k}><span className="text-muted-foreground capitalize">{k.replace(/_/g, " ")}:</span> {showValue(v)}</p>
              )))}
            </div>
          )}
          {deciding && (
            <div className="flex flex-wrap gap-2 pt-1">
              <Button size="lg" variant="outline" onClick={() => onResolve("reject")} disabled={busy} className="flex-1 sm:flex-none">Reject</Button>
              <Button size="lg" onClick={() => setConfirm(true)} disabled={busy} className="flex-1 bg-warn text-white hover:bg-warn/90 sm:flex-none">Accept exception</Button>
            </div>
          )}
          {res && (
            <p className="rounded-lg bg-muted p-3 text-sm">
              {res.action === "approve" ? "You accepted the exception" : "You rejected this purchase"}
              {res.resolved_at && ` at ${formatTime(res.resolved_at)}`}.
              {res.accepted_constraints.length > 0 && <> Accepted as unverified: {res.accepted_constraints.map(labelOf).join(", ")}.</>}
              {res.note && <> Note: {res.note}</>}
            </p>
          )}
        </section>

        <section className={cn("space-y-3 rounded-xl border p-4", !accepted && "opacity-60")}>
          <h3 className="flex items-center gap-2 font-semibold"><span className="grid size-5 place-items-center rounded-full bg-muted-foreground/60 text-xs text-white">2</span>Then approve the payment in Link</h3>
          {accepted
            ? <PaymentPanel detail={detail} health={health} cap={cap} busy={busy} onApprove={onApprovePayment} onDecline={onDecline} />
            : <p className="text-sm text-muted-foreground">{deciding ? "Available after you accept the exception." : "Not needed: nothing will be paid."}</p>}
        </section>
      </div>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><ShieldAlert className="size-5 text-warn" />Accept an unverified purchase?</DialogTitle>
            <DialogDescription>
              Handshake still can&apos;t verify {unknown.map((u) => u.label.toLowerCase()).join(", ") || "some checks"}. Accepting records this as <b>your exception</b>. It is not marked as passed.
              {detail.proposal && <> Next, you approve the {money(detail.proposal.total, detail.proposal.currency)} payment to {detail.proposal.merchant.name} in Link.</>}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(false)}>Cancel</Button>
            <Button onClick={() => { setConfirm(false); onResolve("approve"); }} className="bg-warn text-white hover:bg-warn/90">Accept exception</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Breakdown({ detail, cap }: { detail: PurchaseDetail; cap: number }) {
  const p = detail.proposal!;
  const rows: [string, number, string?][] = [
    ...p.line_items.map((li) => [li.name, li.unit_price * li.quantity, li.attributes.pre_checked ? "pre-checked by merchant" : undefined] as [string, number, string?]),
    ["Shipping", p.shipping], ["Tax", p.tax],
    ...(p.fees ? [["Fees", p.fees] as [string, number]] : []),
    ...(p.discounts ? [["Discounts", -p.discounts] as [string, number]] : []),
  ];
  return (
    <div className="rounded-xl border bg-card p-4 text-sm sm:p-5">
      <h2 className="mb-2 font-semibold">Checkout, as Handshake read it</h2>
      <dl className="space-y-1.5">
        {rows.map(([label, amount, note], i) => (
          <div key={`${label}${i}`} className={cn("flex justify-between gap-3", note && "text-fail")}>
            <dt>{label}{note && <span className="ml-2 rounded bg-fail-soft px-1.5 py-0.5 text-[11px] font-medium">{note}</span>}</dt>
            <dd className="tabular-nums">{money(amount, p.currency)}</dd>
          </div>
        ))}
        <div className={cn("flex justify-between border-t pt-2 font-semibold", p.total > cap && "text-fail")}>
          <dt>Total</dt><dd className="tabular-nums">{money(p.total, p.currency)}</dd>
        </div>
        <div className="flex justify-between text-muted-foreground"><dt>Your signed maximum</dt><dd className="tabular-nums">{money(cap, p.currency)}</dd></div>
      </dl>
      {p.source_url && <p className="mt-3 truncate font-mono text-[11px] text-muted-foreground">Source: {p.source_url}</p>}
    </div>
  );
}

export default function PurchasePage() {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<PurchaseDetail | null>(null);
  const [contract, setContract] = useState<ContractRecord | null>(null);
  const [events, setEvents] = useState<EvidenceEvent[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { step, replay } = useStagedReveal(detail ? detail.purchase.id : null, detail ? reachedStep(detail, events) : 0);

  const load = useCallback(async () => {
    try {
      const [d, h] = await Promise.all([getPurchase(id), getHealth()]);
      const [c, ev] = await Promise.all([getContract(d.purchase.contract_id), getEvidence(id)]);
      setDetail(d); setContract(c); setEvents(ev); setHealth(h);
    } catch (e) { setError((e as Error).message); }
  }, [id]);
  // load() only sets state after awaiting the API.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { load(); }, [load]);

  // Poll while the purchase can still change. GET /purchases/{id} also runs the payment
  // refresh on the backend, so polling is what moves the payment along.
  const terminal = detail ? isTerminal(detail) : true;
  useEffect(() => {
    if (terminal) return;
    let inFlight = false;
    const timer = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const d = await getPurchase(id);
        setDetail(d);
        setEvents(await getEvidence(id));
      } catch { /* transient; the next poll retries (a 401 already redirected to login) */ } finally { inFlight = false; }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [id, terminal]);

  async function refreshAfter(d: PurchaseDetail) {
    setDetail(d);
    setEvents(await getEvidence(id));
  }

  async function resolve(action: "approve" | "reject") {
    setBusy(true);
    try {
      await refreshAfter(await resolveEscalation(id, action));
      toast(action === "approve" ? "Exception accepted" : "Purchase rejected", action === "approve" ? { description: "Next: approve the payment in Link." } : undefined);
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  async function approvePayment() {
    setBusy(true);
    try {
      // Called straight from the click so Link's tab isn't treated as a pop-up.
      const d = await approvePurchase(detail!);
      await refreshAfter(d);
      toast(health?.payment_mode === "link_test" ? "Approve the payment in the Link tab" : "Simulated provider approval sent", { description: "This page updates on its own." });
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  async function decline() {
    setBusy(true);
    try {
      await refreshAfter(await declinePurchase(id));
      toast("Declined", { description: "Nothing was charged. The agent can keep looking." });
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  if (error) return <><AppHeader /><main className="mx-auto max-w-3xl px-4 py-10 text-fail">{error}</main></>;
  if (!detail || !contract) return <><AppHeader /><main className="mx-auto w-full max-w-3xl px-4 py-10"><div className="h-96 animate-pulse rounded-xl bg-muted" /></main></>;

  const decision = detail.decision;
  const outcome = outcomeOf(detail);
  const cap = contract.spend.hard_cap_all_in;
  const done = step >= PROGRESSION_STEPS;
  const passed = decision?.results.filter((r) => r.verdict === "pass").length ?? 0;
  const total = decision?.results.length ?? 0;
  const checking = detail.status === "pending" || detail.status === "validating";
  const escalation = detail.status === "escalated" || detail.resolution?.action === "approve" || detail.resolution?.action === "reject" || decision?.verdict === "unverifiable";
  const paymentView = !escalation && (detail.payment != null || detail.status === "authorized" || detail.status === "completed");

  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-3xl flex-1 space-y-5 px-4 py-8">
        <div className="flex items-center justify-between gap-3">
          <Link href={`/contracts/${contract.id}`} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
            <ArrowLeft className="size-4" />{contract.goal} contract
          </Link>
          <Button variant="ghost" size="sm" onClick={replay}><RotateCcw />Replay</Button>
        </div>

        <Progression step={step} outcome={outcome} />

        {step >= 2 && detail.proposal && <div className="animate-in fade-in slide-in-from-bottom-2 duration-300"><Attempt detail={detail} /></div>}

        {step >= 4 && !done && (
          <p className="animate-pulse text-sm text-muted-foreground">Comparing {total} constraints against your signed contract…</p>
        )}
        {checking && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" />Handshake is reading the checkout and checking it against your contract…</p>
        )}

        {done && !checking && (
          <>
            {escalation
              ? <Escalated detail={detail} health={health} cap={cap} busy={busy} onResolve={resolve} onApprovePayment={approvePayment} onDecline={decline} />
              : paymentView
                ? <PaymentPanel detail={detail} health={health} cap={cap} busy={busy} onApprove={approvePayment} onDecline={decline} />
                : <Blocked detail={detail} cap={cap} />}

            {decision && (
              <section className="animate-in fade-in duration-500">
                <div className="mb-2 flex items-baseline justify-between">
                  <h2 className="font-semibold">Contract vs. checkout</h2>
                  <span className={cn("font-mono text-sm font-semibold tabular-nums", outcome === "pass" ? "text-pass" : outcome === "fail" ? "text-fail" : "text-warn")}>{passed}/{total} passed</span>
                </div>
                <VerdictRows results={decision.results} acceptedByUser={detail.resolution?.accepted_constraints} />
              </section>
            )}

            <div className="grid gap-5 sm:grid-cols-2">
              {detail.proposal && <Breakdown detail={detail} cap={cap} />}
              <section className={cn("rounded-xl border bg-card p-4 sm:p-5", !detail.proposal && "sm:col-span-2")}>
                <h2 className="mb-3 font-semibold">Activity</h2>
                <Timeline events={events} />
              </section>
            </div>
          </>
        )}
      </main>
    </>
  );
}
