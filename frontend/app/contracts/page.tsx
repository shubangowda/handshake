"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AppHeader } from "@/components/handshake/app-header";
import { ContractCard } from "@/components/handshake/contract-card";
import { useSession } from "@/components/handshake/user-menu";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { approvePurchase, compileDraft, getHealth, listContracts, listPurchases, USE_MOCKS } from "@/lib/api";
import { DEMO_INTENT } from "@/lib/mock-data";
import { toast } from "sonner";
import { displayStatus, pendingPurchase, type DisplayStatus } from "@/lib/status";
import type { ContractRecord, Health, PurchaseDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CircleCheck, OctagonX, CircleHelp, Hourglass, Sparkles } from "lucide-react";

type Tab = DisplayStatus | "all";
const TABS: Tab[] = ["all", "pending", "active", "draft", "used", "rejected"];

const DEMOS = [
  { href: "/purchases/purchase_pending", icon: Hourglass, tone: "text-brand", title: "Waiting for payment approval", body: "HOKA · $132.18" },
  { href: "/purchases/purchase_pass", icon: CircleCheck, tone: "text-pass", title: "Honest checkout", body: "Nike · $128.39" },
  { href: "/purchases/purchase_blocked", icon: OctagonX, tone: "text-fail", title: "Malicious merchant", body: "Hidden add-on · $149.72" },
  { href: "/purchases/purchase_escalated", icon: CircleHelp, tone: "text-warn", title: "Unknown seller", body: "SneakerDeals123 · $127.42" },
];

/** Compiles a request into a draft right here, so the demo works without an MCP agent. */
function DescribeBox() {
  const router = useRouter();
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!intent.trim()) return;
    setBusy(true); setError(null);
    try {
      const draft = await compileDraft(intent.trim());
      router.push(`/contracts/${draft.id}`);
    } catch (err) { setError((err as Error).message); setBusy(false); }
  }

  return (
    <form onSubmit={submit} className="mt-6 rounded-xl border bg-card p-4 sm:p-5">
      <label htmlFor="intent" className="flex items-center gap-2 font-semibold"><Sparkles className="size-4" />Describe what to buy</label>
      <p className="mt-1 text-sm text-muted-foreground">Handshake turns it into a draft contract for you to review. Nothing is allowed until you sign.</p>
      <Textarea id="intent" rows={3} className="mt-3" placeholder={DEMO_INTENT} value={intent} onChange={(e) => setIntent(e.target.value)} />
      {error && <p className="mt-2 rounded-lg bg-fail-soft p-3 text-sm text-fail">{error}</p>}
      <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={() => setIntent(DEMO_INTENT)} disabled={busy}>Use the demo request</Button>
        <Button type="submit" disabled={busy || !intent.trim()} className="bg-brand text-brand-foreground hover:bg-brand/90">{busy ? "Drafting…" : "Draft contract"}</Button>
      </div>
    </form>
  );
}

export default function ContractsPage() {
  const [contracts, setContracts] = useState<ContractRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [purchases, setPurchases] = useState<PurchaseDetail[]>([]);
  const [tab, setTab] = useState<Tab>("all");
  const [approving, setApproving] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const session = useSession();

  const load = useCallback(() => {
    Promise.all([listContracts(), listPurchases()]).then(([c, p]) => { setContracts(c); setPurchases(p); }, (e: Error) => setError(e.message));
    // Health decides the approve button (Link vs. simulated provider); it's cached after the first call.
    getHealth().then(setHealth, () => {});
  }, []);
  useEffect(load, [load]);

  async function approve(p: PurchaseDetail) {
    setApproving(p.purchase.id);
    try {
      await approvePurchase(p);
      if (health?.payment_mode === "link_test") toast("Approve the payment in the Link tab", { description: "Open the purchase to watch it go through." });
      else toast.success("Simulated provider approval sent", { description: "Handshake rechecks the checkout, then pays." });
      load();
    } catch (e) { toast.error((e as Error).message); } finally { setApproving(null); }
  }

  const status = (c: ContractRecord) => displayStatus(c, purchases);
  const count = (t: Tab) => contracts?.filter((c) => t === "all" || status(c) === t).length ?? 0;
  // Needs-attention first: waiting for payment, drafts, active, then history.
  const rank: Record<DisplayStatus, number> = { pending: 0, draft: 1, active: 2, used: 3, rejected: 4 };
  const shown = (contracts ?? []).filter((c) => tab === "all" || status(c) === tab).sort((a, b) => rank[status(a)] - rank[status(b)]);

  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{session ? `Hi, ${session.email.split("@")[0]}` : "Contracts"}</h1>
            <p className="mt-1 text-sm text-muted-foreground">Everything your agent is allowed to buy, right now.</p>
          </div>
        </div>

        <DescribeBox />

        <div role="tablist" className="mt-6 flex gap-1 overflow-x-auto border-b">
          {TABS.map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
              className={cn("-mb-px shrink-0 border-b-2 px-3 py-2 text-sm capitalize transition",
                tab === t ? "border-foreground font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}>
              {t} <span className={cn("ml-1 text-xs tabular-nums", t === "pending" && count(t) ? "rounded-full bg-brand px-1.5 text-brand-foreground" : "text-muted-foreground")}>{contracts ? count(t) : ""}</span>
            </button>
          ))}
        </div>

        {error && <p className="mt-6 rounded-lg bg-fail-soft p-4 text-sm text-fail">Couldn&apos;t load contracts: {error}</p>}

        <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {!contracts && !error && Array.from({ length: 3 }, (_, i) => <div key={i} className="h-48 animate-pulse rounded-xl bg-muted" />)}
          {shown.map((c) => <ContractCard key={c.id} contract={c} status={status(c)} pending={pendingPurchase(c, purchases)} onApprove={approve} approving={approving === pendingPurchase(c, purchases)?.purchase.id}
            approveLabel={health?.payment_mode === "stub" ? "Simulated provider approval" : "Approve in Link"} />)}
          {contracts && !shown.length && <p className="col-span-full py-10 text-center text-sm text-muted-foreground">No {tab} contracts.</p>}
        </div>

        {USE_MOCKS && (
          <section className="mt-12">
            <h2 className="font-mono text-[11px] font-semibold tracking-widest text-muted-foreground uppercase">Demo · agent purchase attempts</h2>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {DEMOS.map(({ href, icon: Icon, tone, title, body }) => (
                <Link key={href} href={href} className="flex items-center gap-3 rounded-xl border bg-card p-4 transition hover:shadow-md">
                  <Icon className={cn("size-6 shrink-0", tone)} />
                  <div>
                    <p className="font-medium">{title}</p>
                    <p className="text-sm text-muted-foreground">{body}</p>
                  </div>
                </Link>
              ))}
            </div>
          </section>
        )}
      </main>
    </>
  );
}
