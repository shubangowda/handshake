"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { AppHeader } from "@/components/handshake/app-header";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { createAgentKey, listAgents, MCP_URL, revokeAgent } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { readSession } from "@/lib/session";
import type { Agent, AgentKind, CreatedAgentKey } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Bot, Copy, KeyRound, MonitorSmartphone, Plug, Plus, TriangleAlert } from "lucide-react";

const KIND: Record<AgentKind, { label: string; icon: typeof Bot }> = {
  oauth: { label: "Connected app", icon: Plug },
  device: { label: "Device login", icon: MonitorSmartphone },
  key: { label: "Agent key", icon: KeyRound },
};

const shortDate = (iso: string) => formatDate(iso, { month: "short", day: "numeric", year: "numeric" });

/** "3 hours ago" style, so "last used" reads at a glance. */
function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  const [n, unit] = s < 3600 ? [s / 60, "minute"] : s < 86_400 ? [s / 3600, "hour"] : s < 30 * 86_400 ? [s / 86_400, "day"] : [0, ""];
  if (!unit) return `on ${shortDate(iso)}`;
  const k = Math.floor(n);
  return `${k} ${unit}${k === 1 ? "" : "s"} ago`;
}

const isExpired = (a: Agent) => !!a.expires_at && new Date(a.expires_at).getTime() <= Date.now();
const isActive = (a: Agent) => !a.revoked_at && !isExpired(a);

async function copy(text: string, what: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`${what} copied`);
  } catch {
    toast.error("Couldn't copy. Select the text and copy it yourself.");
  }
}

/**
 * Every agent that can act for the user: apps connected with OAuth (e.g. Muse), device logins from
 * /connect, and agent keys the user created. Revoking one cuts it off at once.
 */
export default function AgentsPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Agent | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    listAgents().then(setAgents, (e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    // Agents belong to an account: sign in first, then come straight back here.
    if (!readSession()) { router.replace(`/login?next=${encodeURIComponent("/agents")}`); return; }
    load();
  }, [load, router]);

  async function revoke() {
    if (!revoking) return;
    setBusy(true);
    try {
      const updated = await revokeAgent(revoking.agent_id);
      setAgents((xs) => xs?.map((a) => (a.agent_id === updated.agent_id ? updated : a)) ?? xs);
      toast(`${updated.name} revoked`, { description: "It can no longer act for you in Handshake." });
      setRevoking(null);
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  // Active first (newest first), then revoked or expired ones, greyed out.
  const sorted = [...(agents ?? [])].sort((a, b) => Number(isActive(b)) - Number(isActive(a)) || b.created_at.localeCompare(a.created_at));

  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-8">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Agents</h1>
            <p className="mt-1 max-w-lg text-sm text-muted-foreground">
              Agents that can shop for you. They can draft contracts and request purchases under contracts you signed. They can never sign, approve exceptions, or change your limits.
            </p>
          </div>
          <Button onClick={() => setCreating(true)} className="bg-brand text-brand-foreground hover:bg-brand/90"><Plus />Create agent key</Button>
        </div>

        {MCP_URL && <ConnectMuse url={MCP_URL} />}

        {error && <p className="mt-6 rounded-lg bg-fail-soft p-4 text-sm text-fail">Couldn&apos;t load agents: {error}</p>}

        <ul className="mt-6 divide-y rounded-xl border bg-card">
          {!agents && !error && Array.from({ length: 2 }, (_, i) => <li key={i} className="h-20 animate-pulse bg-muted/50" />)}
          {agents && !agents.length && (
            <li className="px-4 py-10 text-center text-sm text-muted-foreground">No agents yet. Connect Muse or create an agent key to get started.</li>
          )}
          {sorted.map((a) => <AgentRow key={a.agent_id} agent={a} onRevoke={() => setRevoking(a)} />)}
        </ul>
      </main>

      <CreateKeyDialog open={creating} onOpenChange={setCreating} onCreated={load} />

      <Dialog open={!!revoking} onOpenChange={(o) => { if (!o) setRevoking(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke {revoking?.name}?</DialogTitle>
            <DialogDescription>
              It immediately loses access to your Handshake account: no more drafting contracts, requesting purchases, or collecting cards. This can&apos;t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevoking(null)}>Keep it</Button>
            <Button variant="destructive" onClick={revoke} disabled={busy}>{busy ? "Revoking…" : "Revoke"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function AgentRow({ agent: a, onRevoke }: { agent: Agent; onRevoke: () => void }) {
  const { label, icon: Icon } = KIND[a.kind] ?? KIND.key;
  const active = isActive(a);
  const status = a.revoked_at ? `Revoked ${shortDate(a.revoked_at)}` : isExpired(a) ? `Expired ${shortDate(a.expires_at!)}` : null;
  return (
    <li className={cn("flex flex-wrap items-center gap-3 px-4 py-3.5", !active && "bg-muted/40 text-muted-foreground")}>
      <span className={cn("grid size-9 shrink-0 place-items-center rounded-lg", active ? "bg-brand text-brand-foreground" : "bg-muted")}><Icon className="size-4" /></span>
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-x-2 font-medium">
          <span className={cn("truncate", !active && "line-through decoration-muted-foreground/50")}>{a.name}</span>
          <span className="font-mono text-[10px] font-semibold tracking-wider text-muted-foreground uppercase">{label}</span>
        </p>
        <p className="text-xs text-muted-foreground">
          {a.client_name && a.client_name !== a.name && <>{a.client_name} · </>}
          Connected {shortDate(a.created_at)} · {a.last_used_at ? `last used ${ago(a.last_used_at)}` : "never used"}
          {active && a.expires_at && <> · expires {shortDate(a.expires_at)}</>}
        </p>
      </div>
      {status
        ? <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase">{status}</span>
        : <Button variant="outline" size="sm" onClick={onRevoke}>Revoke</Button>}
    </li>
  );
}

/** How to connect Muse: the MCP server URL (NEXT_PUBLIC_MCP_URL), plus sign-in or an agent key. */
function ConnectMuse({ url }: { url: string }) {
  const say = `Build a custom integration to Handshake. Its MCP server URL is ${url}`;
  return (
    <section className="mt-6 rounded-xl border bg-card p-4 text-sm sm:p-5">
      <h2 className="flex items-center gap-2 font-semibold"><Plug className="size-4" />Connect Muse</h2>
      <p className="mt-1 text-muted-foreground">Tell Muse:</p>
      <div className="mt-2 flex items-start gap-2 rounded-lg bg-muted p-3">
        <p className="min-w-0 flex-1 break-words">&ldquo;{say}&rdquo;</p>
        <Button variant="ghost" size="icon-sm" aria-label="Copy what to tell Muse" onClick={() => copy(say, "Instructions")}><Copy /></Button>
      </div>
      <p className="mt-2 text-muted-foreground">
        When Muse asks, sign in to Handshake and approve it. Or create an agent key here and paste it into Muse&apos;s secure credential prompt.
      </p>
      <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        MCP server URL: <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground break-all">{url}</code>
        <button type="button" className="underline underline-offset-4 hover:text-foreground" onClick={() => copy(url, "MCP server URL")}>Copy</button>
      </p>
    </section>
  );
}

/**
 * Name a key, then show its token exactly once. The token lives only in this dialog's state: it is never
 * written to localStorage, and closing the dialog drops it.
 */
function CreateKeyDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (open: boolean) => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedAgentKey | null>(null);

  function close(o: boolean) {
    if (o) { onOpenChange(true); return; }
    onOpenChange(false);
    setCreated(null); setName(""); setError(null);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true); setError(null);
    try {
      setCreated(await createAgentKey(name.trim()));
      onCreated();
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  return (
    // While the token is on screen, a stray click outside must not throw it away.
    <Dialog open={open} onOpenChange={close} disablePointerDismissal={!!created}>
      <DialogContent className="sm:max-w-md">
        {created ? (
          <>
            <DialogHeader>
              <DialogTitle>Agent key for {created.name}</DialogTitle>
              <DialogDescription>{created.expires_at ? `Valid until ${shortDate(created.expires_at)}.` : "Valid until you revoke it."}</DialogDescription>
            </DialogHeader>
            <p className="flex items-start gap-2 rounded-lg border border-warn/40 bg-warn-soft p-3 text-sm text-warn">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" />
              <span><b>Copy it now; Handshake won&apos;t show it again.</b> Paste it into Muse&apos;s secure credential prompt, never into a chat.</span>
            </p>
            <div className="flex items-center gap-2">
              <Input readOnly value={created.token} aria-label="Agent key" className="h-10 font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
              <Button variant="outline" className="h-10" onClick={() => copy(created.token, "Agent key")}><Copy />Copy</Button>
            </div>
            <DialogFooter>
              <Button onClick={() => close(false)} className="bg-brand text-brand-foreground hover:bg-brand/90">I&apos;ve copied it</Button>
            </DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="grid gap-4">
            <DialogHeader>
              <DialogTitle>Create agent key</DialogTitle>
              <DialogDescription>For an agent that can&apos;t sign in by itself. It gets the same limits as any agent: it can never sign, approve, or change your limits.</DialogDescription>
            </DialogHeader>
            <div className="grid gap-1.5">
              <Label htmlFor="agent-name">Name</Label>
              <Input id="agent-name" placeholder="e.g. Muse on my laptop" maxLength={80} value={name} onChange={(e) => setName(e.target.value)} className="h-10" autoFocus />
            </div>
            {error && <p className="rounded-lg bg-fail-soft p-3 text-sm text-fail">{error}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => close(false)}>Cancel</Button>
              <Button type="submit" disabled={busy || !name.trim()} className="bg-brand text-brand-foreground hover:bg-brand/90">{busy ? "Creating…" : "Create key"}</Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
