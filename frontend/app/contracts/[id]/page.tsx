"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/handshake/app-header";
import { FundsPanel } from "@/components/handshake/funds";
import { PaymentDialog } from "@/components/handshake/payment-dialog";
import { ContractSheet, contractRows } from "@/components/handshake/contract-sheet";
import { EditDraftDialog } from "@/components/handshake/edit-draft-dialog";
import { StatusBadge, SourceTag, SeverityTag } from "@/components/handshake/tags";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { amendContract, getContract, listPurchasesForContract, revokeContract, signContract, updateDraft } from "@/lib/api";
import { FIELD_LABELS, describeConstraint, formatDate, formatTime, isDraft, money, relativeExpiry, shortId } from "@/lib/format";
import type { Contract, ContractRecord, DraftPatch, PurchaseDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ArrowLeft, ArrowRight, GitBranch, PenLine, ShieldCheck, Sparkles, TriangleAlert } from "lucide-react";

function flatRows(c: ContractRecord) {
  const r = contractRows(c);
  return new Map([...r.spend, ...r.item, ...r.delivery, ...r.terms, ...r.prefs].map((x) => [x.label, x.value]));
}

/** Rows that differ between this draft and the signed contract it replaces. */
function VersionDiff({ from, to }: { from: ContractRecord; to: ContractRecord }) {
  const a = flatRows(from), b = flatRows(to);
  const labels = [...new Set([...a.keys(), ...b.keys()])].filter((l) => a.get(l) !== b.get(l));
  return (
    <div className="rounded-xl border bg-card p-4 sm:p-5">
      <h2 className="flex items-center gap-2 font-semibold"><GitBranch className="size-4" />Changes from the signed version</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        The <Link className="underline" href={`/contracts/${from.id}`}>current contract</Link> stays in force until you sign this one. Signing replaces it.
      </p>
      {labels.length ? (
        <dl className="mt-3 divide-y text-sm">
          {labels.map((l) => (
            <div key={l} className="grid grid-cols-[8rem_minmax(0,1fr)] gap-3 py-2">
              <dt className="text-muted-foreground">{l}</dt>
              <dd className="flex flex-wrap items-center gap-2">
                <span className="text-fail line-through">{a.get(l) ?? "—"}</span>
                <ArrowRight className="size-3.5 text-muted-foreground" />
                <span className="font-medium text-pass">{b.get(l) ?? "—"}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : <p className="mt-3 text-sm text-muted-foreground">No changes yet. Use Edit contract to change terms.</p>}
    </div>
  );
}

function InferredPanel({ contract: c }: { contract: ContractRecord & { assumptions?: string[] } }) {
  const inferred = [
    ...(c.spend.hard_cap_source !== "user" ? [{ label: "Maximum total", value: money(c.spend.hard_cap_all_in), source: c.spend.hard_cap_source }] : []),
    ...c.constraints.filter((k) => k.source !== "user").map((k) => ({ label: FIELD_LABELS[k.field], value: describeConstraint(k), source: k.source })),
    ...(c.delivery && c.delivery.max_shipping_source !== "user" ? [{ label: "Shipping", value: `≤ ${money(c.delivery.max_shipping)}`, source: c.delivery.max_shipping_source }] : []),
  ];
  const assumptions = c.assumptions ?? [];
  if (!inferred.length && !assumptions.length) return null;
  return (
    <div className="rounded-xl border border-warn/40 bg-warn-soft p-4 sm:p-5">
      <h2 className="flex items-center gap-2 font-semibold text-warn"><TriangleAlert className="size-4" />Inferred by Handshake</h2>
      <p className="mt-1 text-sm text-foreground/80">You didn&apos;t say these. Handshake filled them in. Check them before you sign.</p>
      <ul className="mt-3 flex flex-wrap gap-2">
        {inferred.map((x) => (
          <li key={x.label} className="rounded-lg border border-warn/30 bg-card px-3 py-1.5 text-sm">
            <span className="text-muted-foreground">{x.label}</span> <span className="font-semibold">{x.value}</span>
          </li>
        ))}
      </ul>
      {assumptions.length > 0 && (
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-foreground/80">
          {assumptions.map((a) => <li key={a}>{a}</li>)}
        </ul>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
      <span className="flex items-center gap-1.5"><SourceTag source="user" /> you said it</span>
      <span className="flex items-center gap-1.5"><SourceTag source="inferred" /> Handshake guessed</span>
      <span className="flex items-center gap-1.5"><SourceTag source="default" /> standard rule</span>
      <span className="flex items-center gap-1.5"><SeverityTag severity="hard" /> blocks if broken</span>
      <span className="flex items-center gap-1.5"><SeverityTag severity="soft" /> nice to have</span>
    </div>
  );
}

export default function ContractPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [contract, setContract] = useState<ContractRecord | null>(null);
  const [previous, setPrevious] = useState<ContractRecord | null>(null);
  const [purchases, setPurchases] = useState<PurchaseDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [confirmSign, setConfirmSign] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const c = await getContract(id);
      setContract(c);
      setPrevious(c.previous_contract_id ? await getContract(c.previous_contract_id).catch(() => null) : null);
      setPurchases(isDraft(c) ? [] : await listPurchasesForContract(c.id));
    } catch (e) { setError((e as Error).message); }
  }, [id]);
  // load() only sets state after awaiting the API.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { load(); }, [load]);

  if (error) return <><AppHeader /><main className="mx-auto max-w-3xl px-4 py-10 text-fail">{error}</main></>;
  if (!contract) return <><AppHeader /><main className="mx-auto w-full max-w-3xl px-4 py-10"><div className="h-96 animate-pulse rounded-xl bg-muted" /></main></>;

  const draft = isDraft(contract) ? contract : null;

  /** Runs after the card form is filled. Errors keep the payment dialog open. */
  async function sign() {
    try {
      const signed = await signContract(contract!.id);
      toast.success("Signed and paid", { description: `${money(signed.spend.hard_cap_all_in)} is held until your agent finds a match.` });
      router.replace(`/contracts/${signed.id}`);
    } catch (e) { toast.error((e as Error).message); throw e; }
  }
  async function revoke() {
    setBusy(true);
    try {
      const c = await revokeContract(contract!.id);
      toast("Contract revoked", { description: c.funding ? `${money(c.funding.amount_refunded)} refunded to your card.` : "Your agent can no longer use it." });
      await load();
    }
    catch (e) { toast.error((e as Error).message); } finally { setBusy(false); setConfirmRevoke(false); }
  }
  async function amend() {
    setBusy(true);
    try { const d = await amendContract(contract!.id); router.push(`/contracts/${d.id}`); }
    catch (e) { toast.error((e as Error).message); setBusy(false); }
  }
  async function save(patch: DraftPatch) {
    const d = await updateDraft(contract!.id, patch);
    setContract(d);
    toast.success("Draft updated");
  }

  return (
    <>
      <AppHeader />
      <main className={cn("mx-auto w-full max-w-3xl flex-1 px-4 py-8", draft && "pb-32")}>
        <Link href="/contracts" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" />Contracts</Link>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <StatusBadge status={contract.status} />
          <span className="text-sm text-muted-foreground">
            {draft ? "Not signed. Your agent can't use this yet." : contract.status === "active" ? `Your agent may use this · ${relativeExpiry(contract.expires_at).toLowerCase()}` : `Signed ${formatDate((contract as Contract).signed_at)} at ${formatTime((contract as Contract).signed_at)}`}
          </span>
        </div>

        <div className="mt-6 space-y-5">
          {draft && previous && <VersionDiff from={previous} to={draft} />}
          {draft && <InferredPanel contract={draft} />}
          <Legend />
          {!draft && <FundsPanel funding={(contract as Contract).funding} />}
          <ContractSheet contract={contract} />

          {!draft && (
            <div className="flex flex-wrap items-center gap-3">
              {contract.status === "active" && <Button variant="outline" onClick={amend} disabled={busy}><PenLine />Edit (creates new version)</Button>}
              {contract.status === "active" && contract.revocable && <Button variant="destructive" onClick={() => setConfirmRevoke(true)} disabled={busy}>Revoke</Button>}
              <span className="ml-auto font-mono text-[11px] text-muted-foreground">#{shortId(contract.id)} · {(contract as Contract).contract_hash.slice(0, 19)}</span>
            </div>
          )}

          {purchases.length > 0 && (
            <section>
              <h2 className="mb-2 font-semibold">Purchase attempts</h2>
              <ul className="divide-y rounded-xl border bg-card">
                {purchases.map(({ purchase: p, proposal, decision }) => (
                  <li key={p.id}>
                    <Link href={`/purchases/${p.id}`} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-muted/50">
                      <span>{proposal?.line_items[0]?.name} <span className="text-muted-foreground">· {p.merchant_name}</span></span>
                      <span className={cn("font-mono text-xs font-bold uppercase", decision?.verdict === "pass" ? "text-pass" : decision?.verdict === "fail" ? "text-fail" : "text-warn")}>
                        {p.status} · {money(proposal?.total)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </main>

      {draft && (
        <div className="fixed inset-x-0 bottom-0 z-20 border-t bg-background/95 backdrop-blur">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3 px-4 py-3">
            <p className="text-sm text-muted-foreground">
              You pay <span className="font-semibold text-foreground tabular-nums">{money(draft.spend.hard_cap_all_in)}</span> now · unused money is refunded
            </p>
            <div className="flex gap-2">
              <Button variant="outline" size="lg" onClick={() => setEditing(true)}><PenLine />Edit contract</Button>
              <Button size="lg" onClick={() => setConfirmSign(true)} className="bg-brand text-brand-foreground hover:bg-brand/90"><ShieldCheck />Sign &amp; pay {money(draft.spend.hard_cap_all_in)}</Button>
            </div>
          </div>
        </div>
      )}

      {draft && <EditDraftDialog key={draft.id + JSON.stringify(draft.spend)} draft={draft} open={editing} onOpenChange={setEditing} onSave={save} />}

      <PaymentDialog
        open={confirmSign}
        onOpenChange={setConfirmSign}
        amount={contract.spend.hard_cap_all_in}
        title={`Sign & pay ${money(contract.spend.hard_cap_all_in)}`}
        submitLabel={`Sign & pay ${money(contract.spend.hard_cap_all_in)}`}
        onPay={sign}
        description={<>
          Your agent may buy <b>{contract.goal.toLowerCase()}</b> for up to <b>{money(contract.spend.hard_cap_all_in)}</b> all-in,
          {contract.single_use ? " once" : " repeatedly"}, until {formatDate(contract.expires_at)}. Anything outside these terms is blocked.
        </>}
      >
        <ul className="space-y-1.5 rounded-lg border p-3 text-sm">
          <li><b>{money(contract.spend.hard_cap_all_in)}</b> is held by Handshake now.</li>
          <li>When a match is found, you approve it with one tap. The unspent amount is refunded.</li>
          <li>If nothing is found before it expires, or you revoke it, you get it all back.</li>
        </ul>
        {draft && contract.constraints.some((k) => k.source !== "user") && (
          <p className="flex items-start gap-2 rounded-lg bg-warn-soft p-3 text-sm text-warn"><Sparkles className="mt-0.5 size-4 shrink-0" />This includes values Handshake inferred. They&apos;re highlighted in the contract.</p>
        )}
      </PaymentDialog>

      <Dialog open={confirmRevoke} onOpenChange={setConfirmRevoke}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke this contract?</DialogTitle>
            <DialogDescription>
              Your agent immediately loses permission to buy {contract.goal.toLowerCase()}.
              {(contract as Contract).funding?.status === "held" && <> The {money((contract as Contract).funding!.amount_held)} you paid is refunded to your card.</>} This can&apos;t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmRevoke(false)}>Keep it</Button>
            <Button variant="destructive" onClick={revoke} disabled={busy}>Revoke</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
