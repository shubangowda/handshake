"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/handshake/app-header";
import { SignDialog } from "@/components/handshake/sign-dialog";
import { FundingCard } from "@/components/handshake/funding";
import { signingIsSimulated, useLinkStatus } from "@/components/handshake/link-connect";
import { ContractSheet, contractRows } from "@/components/handshake/contract-sheet";
import { EditDraftDialog } from "@/components/handshake/edit-draft-dialog";
import { StatusBadge, SourceTag, SeverityTag } from "@/components/handshake/tags";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ApiError, amendContract, getContract, getHealth, listPurchasesForContract, revokeContract, signContract, updateDraft } from "@/lib/api";
import { FIELD_LABELS, describeConstraint, formatDate, formatTime, isDraft, money, relativeExpiry, shortId } from "@/lib/format";
import { paymentLabel } from "@/lib/status";
import type { ContractRecord, DraftPatch, Health, PurchaseDetail, SignedContract } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ArrowLeft, ArrowRight, GitBranch, OctagonX, PenLine, ShieldAlert, ShieldCheck, TriangleAlert } from "lucide-react";

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

function InferredPanel({ contract: c }: { contract: ContractRecord & { assumptions?: string[]; clarifications_needed?: string[] } }) {
  const inferred = [
    ...(c.spend.hard_cap_source !== "user" ? [{ label: "Maximum total", value: money(c.spend.hard_cap_all_in), source: c.spend.hard_cap_source }] : []),
    ...c.constraints.filter((k) => k.source !== "user").map((k) => ({ label: FIELD_LABELS[k.field], value: describeConstraint(k), source: k.source })),
    ...(c.delivery && c.delivery.max_shipping_source !== "user" ? [{ label: "Shipping", value: `≤ ${money(c.delivery.max_shipping)}`, source: c.delivery.max_shipping_source }] : []),
  ];
  const assumptions = c.assumptions ?? [];
  // "lint: " entries are blocking issues (shown separately); the rest are the compiler's open questions.
  const questions = (c.clarifications_needed ?? []).filter((q) => !q.startsWith("lint: "));
  if (!inferred.length && !assumptions.length && !questions.length) return null;
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
      {questions.length > 0 && (
        <>
          <p className="mt-3 text-sm font-medium">Handshake wasn&apos;t sure about</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-foreground/80">{questions.map((q) => <li key={q}>{q}</li>)}</ul>
        </>
      )}
    </div>
  );
}

/** The backend re-checks the stored contract's hash and signature on every read. */
function Verification({ contract: c }: { contract: SignedContract }) {
  const v = c.verification;
  if (!v) return null;
  return (
    <div className={cn("flex items-start gap-3 rounded-xl border p-4 text-sm", v.valid ? "bg-card" : "border-fail/40 bg-fail-soft text-fail")}>
      {v.valid ? <ShieldCheck className="mt-0.5 size-4 shrink-0 text-pass" /> : <ShieldAlert className="mt-0.5 size-4 shrink-0" />}
      <div>
        <p className="font-semibold">{v.valid ? "Signature valid" : "Signature NOT valid"}</p>
        <p className={cn(v.valid ? "text-muted-foreground" : "")}>
          {v.valid
            ? "Handshake re-checked this contract's hash and signature just now. It is exactly what you signed."
            : `This contract no longer matches what you signed (hash ${v.hash_matches ? "matches" : "differs"}, signature ${v.signature_matches ? "matches" : "differs"}). Purchases against it are blocked.`}
        </p>
      </div>
    </div>
  );
}

function BlockingIssues({ issues }: { issues: string[] }) {
  if (!issues.length) return null;
  return (
    <div className="rounded-xl border border-fail/40 bg-fail-soft p-4 text-sm text-fail sm:p-5">
      <h2 className="flex items-center gap-2 font-semibold"><OctagonX className="size-4" />Fix before signing</h2>
      <ul className="mt-2 list-disc space-y-1 pl-5">{issues.map((b) => <li key={b}>{b}</li>)}</ul>
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
  // Blocking issues a sign attempt returned (409 draft_has_blocking_issues), until the draft reloads.
  const [signIssues, setSignIssues] = useState<string[] | null>(null);
  // The last sign attempt returned 409 link_not_connected: show the Connect Stripe Link panel.
  const [needsLink, setNeedsLink] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  // Refreshed when Link is connected or disconnected anywhere on the page (header, sign dialog).
  const { status: linkStatus } = useLinkStatus();

  const load = useCallback(async () => {
    try {
      const [c, h] = await Promise.all([getContract(id), getHealth()]);
      setHealth(h);
      // A signed draft lives on as a record; show the contract it became instead.
      if (isDraft(c) && c.signed_contract_id) { router.replace(`/contracts/${c.signed_contract_id}`); return; }
      setContract(c);
      setSignIssues(null);
      setPrevious(c.previous_contract_id ? await getContract(c.previous_contract_id).catch(() => null) : null);
      setPurchases(isDraft(c) ? [] : await listPurchasesForContract(c.id));
    } catch (e) { setError((e as Error).message); }
  }, [id, router]);
  // load() only sets state after awaiting the API.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { load(); }, [load]);

  if (error) return <><AppHeader /><main className="mx-auto max-w-3xl px-4 py-10 text-fail">{error}</main></>;
  if (!contract) return <><AppHeader /><main className="mx-auto w-full max-w-3xl px-4 py-10"><div className="h-96 animate-pulse rounded-xl bg-muted" /></main></>;

  const draft = isDraft(contract) ? contract : null;
  const signed = draft ? null : (contract as SignedContract);
  const blockingIssues = signIssues ?? draft?.blocking_issues ?? [];
  // Stub mode, or link_optional without the user's Link: signing gets a simulated card, so say so.
  const simulated = signingIsSimulated(health, linkStatus) ?? health?.payment_mode === "stub";

  /** Runs from the review dialog. Errors keep the dialog open; blocking issues and Link connect are shown in it. */
  async function sign() {
    setNeedsLink(false);
    try {
      const c = await signContract(contract!.id);
      setConfirmSign(false);
      toast.success("Contract signed", { description: `Now approve the single-use card for up to ${money(c.spend.hard_cap_all_in)} to fund it.` });
      router.replace(`/contracts/${c.id}`);
    } catch (e) {
      const issues = e instanceof ApiError && e.code === "draft_has_blocking_issues" ? e.details.blocking_issues : null;
      if (Array.isArray(issues)) setSignIssues(issues.map(String));
      else if (e instanceof ApiError && e.code === "link_not_connected") setNeedsLink(true);
      else toast.error((e as Error).message);
    }
  }
  async function revoke() {
    setBusy(true);
    try {
      await revokeContract(contract!.id);
      toast("Contract revoked", { description: "Your agent can no longer use it." });
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
    try {
      const d = await updateDraft(contract!.id, patch);
      setContract(d);
      setSignIssues(null);
      toast.success("Draft updated");
    } catch (e) { toast.error((e as Error).message); throw e; }
  }

  return (
    <>
      <AppHeader />
      <main className={cn("mx-auto w-full max-w-3xl flex-1 px-4 py-8", draft && "pb-32")}>
        <Link href="/contracts" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" />Contracts</Link>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <StatusBadge status={contract.status} />
          <span className="text-sm text-muted-foreground">
            {draft ? "Not signed. Your agent can't use this yet." : contract.status === "active" ? `Your agent may use this · ${relativeExpiry(contract.expires_at).toLowerCase()}` : `Signed ${formatDate(signed!.signed_at)} at ${formatTime(signed!.signed_at)}`}
          </span>
        </div>

        <div className="mt-6 space-y-5">
          {draft && previous && <VersionDiff from={previous} to={draft} />}
          {draft && <BlockingIssues issues={blockingIssues} />}
          {draft && <InferredPanel contract={draft} />}
          {signed && <Verification contract={signed} />}
          {signed && signed.funding && (
            // Keyed so a reload (e.g. after revoke) resets the card's own polling state.
            <FundingCard key={`${signed.status}:${signed.funding.funding_id ?? ""}:${signed.funding.state}`} contractId={signed.id}
              active={signed.status === "active"} initial={signed.funding} />
          )}
          <Legend />
          <ContractSheet contract={contract} />

          {!draft && (
            <div className="flex flex-wrap items-center gap-3">
              {contract.status === "active" && <Button variant="outline" onClick={amend} disabled={busy}><PenLine />Edit (creates new version)</Button>}
              {contract.status === "active" && contract.revocable && <Button variant="destructive" onClick={() => setConfirmRevoke(true)} disabled={busy}>Revoke</Button>}
              <span className="ml-auto font-mono text-[11px] text-muted-foreground">#{shortId(contract.id)} · {signed!.contract_hash.slice(0, 19)}</span>
            </div>
          )}

          {purchases.length > 0 && (
            <section>
              <h2 className="mb-2 font-semibold">Purchase attempts</h2>
              <ul className="divide-y rounded-xl border bg-card">
                {purchases.map((d) => {
                  const { purchase: p, proposal, decision } = d;
                  const pay = paymentLabel(d);
                  return (
                    <li key={p.id}>
                      <Link href={`/purchases/${p.id}`} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-muted/50">
                        <span className="min-w-0">
                          {proposal?.line_items[0]?.name ?? "Checkout not read"} <span className="text-muted-foreground">· {p.merchant_name ?? "unknown merchant"}</span>
                          {pay && <span className="block text-xs text-muted-foreground">Payment: {pay}</span>}
                        </span>
                        <span className={cn("shrink-0 font-mono text-xs font-bold uppercase", p.status === "completed" || decision?.verdict === "pass" ? "text-pass" : p.status === "blocked" || p.status === "failed" ? "text-fail" : "text-warn")}>
                          {p.status} · {money(proposal?.total)}
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
        </div>
      </main>

      {draft && (
        <div className="fixed inset-x-0 bottom-0 z-20 border-t bg-background/95 backdrop-blur">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3 px-4 py-3">
            <p className="text-sm text-muted-foreground">
              Up to <span className="font-semibold text-foreground tabular-nums">{money(draft.spend.hard_cap_all_in)}</span> all-in · signing asks {simulated ? "the simulated provider" : "your Link account"} for a single-use card for this amount
            </p>
            <div className="flex gap-2">
              <Button variant="outline" size="lg" onClick={() => setEditing(true)}><PenLine />Edit contract</Button>
              <Button size="lg" onClick={() => setConfirmSign(true)} className="bg-brand text-brand-foreground hover:bg-brand/90"><ShieldCheck />Review, sign &amp; fund</Button>
            </div>
          </div>
        </div>
      )}

      {draft && <EditDraftDialog key={draft.id + JSON.stringify(draft.spend)} draft={draft} open={editing} onOpenChange={setEditing} onSave={save} />}

      {draft && <SignDialog draft={draft} blockingIssues={blockingIssues} needsLink={needsLink} simulated={simulated} onLinkConnected={() => setNeedsLink(false)}
        open={confirmSign} onOpenChange={setConfirmSign} onSign={sign} />}

      <Dialog open={confirmRevoke} onOpenChange={setConfirmRevoke}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke this contract?</DialogTitle>
            <DialogDescription>
              Your agent immediately loses permission to buy {contract.goal.toLowerCase()}. Its funding is canceled and any stored card is wiped. This can&apos;t be undone.
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
