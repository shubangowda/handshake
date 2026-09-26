"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/handshake/app-header";
import { Progression, PROGRESSION_STEPS, type Outcome } from "@/components/handshake/progression";
import { Timeline } from "@/components/handshake/timeline";
import { VerdictRows } from "@/components/handshake/verdict-rows";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { approvePurchase, declinePurchase, getContract, getEvidence, getPurchase, resolveEscalation } from "@/lib/api";
import { money } from "@/lib/format";
import type { ContractRecord, EvidenceEvent, PurchaseDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ArrowLeft, Check, CircleCheck, CircleHelp, OctagonX, RotateCcw, ShieldAlert, ShoppingBag } from "lucide-react";

function useStagedReveal(key: string | null) {
  const [step, setStep] = useState(0);
  const [run, setRun] = useState(0);
  useEffect(() => {
    if (!key) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timers = Array.from({ length: PROGRESSION_STEPS }, (_, i) =>
      setTimeout(() => setStep(i + 1), reduced ? 0 : 650 * (i + 1)));
    return () => timers.forEach(clearTimeout);
  }, [key, run]);
  return { step, replay: () => { setStep(0); setRun((r) => r + 1); } };
}

function Attempt({ detail }: { detail: PurchaseDetail }) {
  const p = detail.proposal!;
  const item = p.line_items[0];
  return (
    <div className="flex items-center gap-4 rounded-xl border bg-card p-4 sm:p-5">
      <span className="grid size-12 shrink-0 place-items-center rounded-lg bg-muted"><ShoppingBag className="size-5 text-muted-foreground" /></span>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-muted-foreground">Agent wants to purchase</p>
        <p className="truncate text-lg font-semibold">{item.name}{p.line_items.length > 1 && <span className="font-normal text-muted-foreground"> + {p.line_items.length - 1} more</span>}</p>
        <p className="truncate text-sm text-muted-foreground">from {p.merchant.name}{p.merchant.domain && ` · ${p.merchant.domain}`}</p>
      </div>
      <p className="text-right text-2xl font-semibold tabular-nums">{money(p.total)}<span className="block text-xs font-normal text-muted-foreground">total</span></p>
    </div>
  );
}

function Blocked({ detail, cap }: { detail: PurchaseDetail; cap: number }) {
  const fails = detail.decision!.results.filter((r) => r.verdict === "fail");
  const total = detail.proposal!.total;
  return (
    <div className="overflow-hidden rounded-2xl border-2 border-fail shadow-[0_0_0_6px_var(--color-fail-soft)] animate-in fade-in zoom-in-95 duration-300">
      <div className="flex items-center gap-3 bg-fail px-5 py-4 text-white">
        <OctagonX className="size-8 shrink-0" strokeWidth={2.5} />
        <div>
          <p className="text-2xl font-black tracking-tight sm:text-3xl">PURCHASE BLOCKED</p>
          <p className="text-sm text-white/85">Nothing was charged. Your money is still held and the agent keeps looking.</p>
        </div>
      </div>
      <div className="grid gap-4 bg-card p-5 sm:grid-cols-2">
        <div>
          <p className="text-sm text-muted-foreground">Agent attempted</p>
          <p className="text-4xl font-semibold text-fail tabular-nums">{money(total)}</p>
        </div>
        <div>
          <p className="text-sm text-muted-foreground">You signed a maximum of</p>
          <p className="text-4xl font-semibold tabular-nums">{money(cap)}</p>
        </div>
        <ul className="space-y-1.5 sm:col-span-2">
          {fails.map((f) => (
            <li key={f.constraint} className="text-[15px]"><span className="font-semibold whitespace-nowrap text-fail">✕ {f.label}:</span> {f.reason}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Authorized({ detail, cap, onDecide, busy }: { detail: PurchaseDetail; cap: number; onDecide: (a: "approve" | "decline") => void; busy: boolean }) {
  const total = detail.proposal!.total;
  const refund = cap - total;
  const awaiting = detail.purchase.status === "authorized";
  const declined = detail.purchase.status === "blocked";
  return (
    <div className={cn("flex flex-wrap items-center gap-4 rounded-2xl border-2 p-5 animate-in fade-in zoom-in-95 duration-300",
      declined ? "border-border bg-muted/50" : "border-pass bg-pass-soft")}>
      <CircleCheck className={cn("size-10", declined ? "text-muted-foreground" : "text-pass")} strokeWidth={2.5} />
      <div className="flex-1">
        <p className={cn("text-2xl font-black tracking-tight", declined ? "text-muted-foreground" : "text-pass")}>
          {awaiting ? "MATCH FOUND · APPROVE IT?" : declined ? "DECLINED BY YOU" : "PURCHASED"}
        </p>
        <p className="text-sm">
          {awaiting && <>Every check passed. Approve and {money(total)} is paid from the {money(cap)} you already put down. {money(refund)} comes back to your card.</>}
          {declined && "Nothing was charged. Your money is still held and the agent keeps looking."}
          {!awaiting && !declined && <>Paid {money(total)} from your held funds. {money(refund)} was refunded to your card.</>}
        </p>
      </div>
      <div className="text-right">
        <p className="text-sm text-muted-foreground">{money(total)} of {money(cap)}</p>
        <div className="mt-1 h-2 w-40 overflow-hidden rounded-full bg-white"><div className="h-full rounded-full bg-pass" style={{ width: `${Math.min(100, (total / cap) * 100)}%` }} /></div>
      </div>
      {awaiting && (
        <div className="flex w-full flex-wrap gap-2">
          <Button size="lg" onClick={() => onDecide("approve")} disabled={busy} className="flex-1 bg-brand text-brand-foreground hover:bg-brand/90 sm:flex-none">
            <Check />{busy ? "Approving…" : `Approve purchase · ${money(total)}`}
          </Button>
          <Button size="lg" variant="outline" onClick={() => onDecide("decline")} disabled={busy} className="flex-1 sm:flex-none">Not this one</Button>
        </div>
      )}
    </div>
  );
}

function Escalated({ detail, onResolve, busy }: { detail: PurchaseDetail; onResolve: (a: "approve" | "reject") => void; busy: boolean }) {
  const [confirm, setConfirm] = useState(false);
  const unknown = detail.decision!.results.filter((r) => r.verdict === "unverifiable" && r.severity === "hard");
  const resolution = detail.purchase.resolution;
  return (
    <div className="overflow-hidden rounded-2xl border-2 border-warn animate-in fade-in zoom-in-95 duration-300">
      <div className="flex items-center gap-3 bg-warn px-5 py-4 text-white">
        <CircleHelp className="size-8 shrink-0" strokeWidth={2.5} />
        <div>
          <p className="text-2xl font-black tracking-tight sm:text-3xl">{resolution ? (resolution.action === "approve" ? "EXCEPTION APPROVED" : "REJECTED BY YOU") : "HANDSHAKE NEEDS YOU"}</p>
          <p className="text-sm text-white/90">{resolution ? (resolution.action === "approve" ? `You approved it even though it couldn't be verified. Paid from your held funds.` : "Nothing was charged. Your money is still held and the agent keeps looking.") : "Everything else checked out, but one thing couldn't be confirmed."}</p>
        </div>
      </div>
      <div className="space-y-4 bg-card p-5">
        <div>
          <p className="text-sm text-muted-foreground">We could not verify</p>
          {unknown.map((r) => <p key={r.constraint} className="text-lg font-semibold">{r.label}: {r.reason.replace(/^Handshake could not confirm /, "").replace(/\.$/, "")}</p>)}
        </div>
        <div className="flex flex-wrap gap-8">
          <div><p className="text-sm text-muted-foreground">Requested total</p><p className="text-2xl font-semibold tabular-nums">{money(detail.proposal!.total)}</p></div>
          <div><p className="text-sm text-muted-foreground">Seller</p><p className="text-2xl font-semibold">{detail.proposal!.merchant.name}</p></div>
        </div>
        {unknown.some((r) => r.evidence) && (
          <div className="rounded-lg bg-muted p-3 text-sm">
            <p className="mb-1 font-mono text-[11px] font-semibold tracking-widest text-muted-foreground uppercase">Evidence</p>
            {unknown.flatMap((r) => Object.entries(r.evidence ?? {})).map(([k, v]) => (
              <p key={k}><span className="text-muted-foreground capitalize">{k}:</span> {String(v)}</p>
            ))}
          </div>
        )}
        {!resolution && (
          <div className="flex flex-wrap gap-2 pt-1">
            <Button size="lg" variant="outline" onClick={() => onResolve("reject")} disabled={busy} className="flex-1 sm:flex-none">Reject</Button>
            <Button size="lg" onClick={() => setConfirm(true)} disabled={busy} className="flex-1 bg-warn text-white hover:bg-warn/90 sm:flex-none">Approve exception</Button>
          </div>
        )}
      </div>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2"><ShieldAlert className="size-5 text-warn" />Approve an unverified purchase?</DialogTitle>
            <DialogDescription>
              Handshake still can&apos;t verify {unknown.map((u) => u.label?.toLowerCase()).join(", ")}. Approving records this as <b>your exception</b>. It is not marked as passed.
              {money(detail.proposal!.total)} will be paid to {detail.proposal!.merchant.name} from your held funds, and the rest refunded.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(false)}>Cancel</Button>
            <Button onClick={() => { setConfirm(false); onResolve("approve"); }} className="bg-warn text-white hover:bg-warn/90">Approve exception</Button>
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
        {rows.map(([label, amount, note]) => (
          <div key={label} className={cn("flex justify-between gap-3", note && "text-fail")}>
            <dt>{label}{note && <span className="ml-2 rounded bg-fail-soft px-1.5 py-0.5 text-[11px] font-medium">{note}</span>}</dt>
            <dd className="tabular-nums">{money(amount)}</dd>
          </div>
        ))}
        <div className={cn("flex justify-between border-t pt-2 font-semibold", p.total > cap && "text-fail")}>
          <dt>Total</dt><dd className="tabular-nums">{money(p.total)}</dd>
        </div>
        <div className="flex justify-between text-muted-foreground"><dt>Your signed maximum</dt><dd className="tabular-nums">{money(cap)}</dd></div>
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
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { step, replay } = useStagedReveal(detail ? detail.purchase.id : null);

  const load = useCallback(async () => {
    try {
      const d = await getPurchase(id);
      const [c, ev] = await Promise.all([getContract(d.purchase.contract_id), getEvidence(id)]);
      setDetail(d); setContract(c); setEvents(ev);
    } catch (e) { setError((e as Error).message); }
  }, [id]);
  // load() only sets state after awaiting the API.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { load(); }, [load]);

  async function resolve(action: "approve" | "reject") {
    setBusy(true);
    try {
      const d = await resolveEscalation(id, action);
      setDetail(d); setEvents(await getEvidence(id));
      toast(action === "approve" ? "Exception approved" : "Purchase rejected");
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  async function decide(action: "approve" | "decline") {
    setBusy(true);
    try {
      const d = action === "approve" ? await approvePurchase(id) : await declinePurchase(id);
      setDetail(d); setEvents(await getEvidence(id));
      if (action === "approve") {
        const total = d.proposal?.total ?? 0;
        toast.success("Purchase approved", { description: `${money(total)} paid · ${money(contract!.spend.hard_cap_all_in - total)} refunded to your card.` });
      } else toast("Declined", { description: "Your money stays held. The agent keeps looking." });
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  if (error) return <><AppHeader /><main className="mx-auto max-w-3xl px-4 py-10 text-fail">{error}</main></>;
  if (!detail || !contract) return <><AppHeader /><main className="mx-auto w-full max-w-3xl px-4 py-10"><div className="h-96 animate-pulse rounded-xl bg-muted" /></main></>;

  const decision = detail.decision;
  const outcome: Outcome = decision?.verdict ?? "unverifiable";
  const cap = contract.spend.hard_cap_all_in;
  const done = step >= PROGRESSION_STEPS;
  const passed = decision?.results.filter((r) => r.verdict === "pass").length ?? 0;
  const total = decision?.results.length ?? 0;

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

        {done && decision && detail.proposal && (
          <>
            {outcome === "fail" && <Blocked detail={detail} cap={cap} />}
            {outcome === "pass" && <Authorized detail={detail} cap={cap} onDecide={decide} busy={busy} />}
            {outcome === "unverifiable" && <Escalated detail={detail} onResolve={resolve} busy={busy} />}

            <section className="animate-in fade-in duration-500">
              <div className="mb-2 flex items-baseline justify-between">
                <h2 className="font-semibold">Contract vs. checkout</h2>
                <span className={cn("font-mono text-sm font-semibold tabular-nums", outcome === "pass" ? "text-pass" : outcome === "fail" ? "text-fail" : "text-warn")}>{passed}/{total} passed</span>
              </div>
              <VerdictRows results={decision.results} acceptedByUser={detail.purchase.resolution?.accepted_constraints} />
            </section>

            <div className="grid gap-5 sm:grid-cols-2">
              <Breakdown detail={detail} cap={cap} />
              <section className="rounded-xl border bg-card p-4 sm:p-5">
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
