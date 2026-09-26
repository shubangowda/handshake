"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AppHeader } from "@/components/handshake/app-header";
import { Button } from "@/components/ui/button";
import { approveDevice, denyDevice, getDeviceAuthorization } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { readSession } from "@/lib/session";
import type { DeviceAuthorization } from "@/lib/types";
import { Bot, Check, CircleCheck, OctagonX, X } from "lucide-react";

/**
 * The page an agent's login link opens (OAuth device flow): /connect?code=USER-CODE.
 * The user sees which agent is asking and what it may and may never do, then approves or denies.
 */
export default function ConnectPage() {
  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-xl flex-1 px-4 py-10">
        {/* useSearchParams needs a Suspense boundary so the page can still prerender. */}
        <Suspense fallback={<div className="h-72 animate-pulse rounded-xl bg-muted" />}>
          <ConnectRequest />
        </Suspense>
      </main>
    </>
  );
}

type Outcome = "approved" | "denied";

function ConnectRequest() {
  const router = useRouter();
  const code = (useSearchParams().get("code") ?? "").trim();
  const [request, setRequest] = useState<DeviceAuthorization | null>(null);
  const [error, setError] = useState<string | null>(code ? null : "This link has no code. Ask your agent for a new connect link.");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  useEffect(() => {
    if (!code) return;
    // Only a logged-in user can connect an agent to their account; come back here after login.
    if (!readSession()) { router.replace(`/login?next=${encodeURIComponent(`/connect?code=${code}`)}`); return; }
    getDeviceAuthorization(code).then(setRequest, (e: Error) => setError(e.message));
  }, [code, router]);

  async function decide(approve: boolean) {
    setBusy(true);
    try {
      const r = approve ? await approveDevice(code) : await denyDevice(code);
      setOutcome(r.status === "approved" ? "approved" : "denied");
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  if (outcome) {
    return (
      <div className="rounded-2xl border bg-card p-6 text-center animate-in fade-in zoom-in-95 duration-300">
        {outcome === "approved"
          ? <><CircleCheck className="mx-auto size-10 text-pass" /><h1 className="mt-3 text-2xl font-semibold">Agent connected. You can return to your agent.</h1>
              <p className="mt-2 text-sm text-muted-foreground">It can now draft contracts for you to review. It still can&apos;t sign, approve, or pay anything without you.</p></>
          : <><OctagonX className="mx-auto size-10 text-muted-foreground" /><h1 className="mt-3 text-2xl font-semibold">Request denied</h1>
              <p className="mt-2 text-sm text-muted-foreground">The agent was not connected to your account. You can close this tab.</p></>}
        <Link href="/contracts" className="mt-5 inline-block text-sm underline underline-offset-4">Go to your contracts</Link>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-2xl border border-fail/40 bg-fail-soft p-6 text-fail">
        <h1 className="flex items-center gap-2 text-xl font-semibold"><OctagonX className="size-5" />Can&apos;t connect this agent</h1>
        <p className="mt-2 text-sm">{error}</p>
      </div>
    );
  }

  if (!request) return <div className="h-72 animate-pulse rounded-xl bg-muted" />;

  const pending = request.status === "pending";
  return (
    <div className="overflow-hidden rounded-2xl border bg-card shadow-sm">
      <div className="flex items-center gap-3 border-b bg-brand px-5 py-5 text-brand-foreground">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-white/15"><Bot className="size-5" /></span>
        <div className="min-w-0">
          <p className="text-sm opacity-80">An agent wants to connect to your Handshake account</p>
          <h1 className="truncate text-xl font-semibold">{request.client_name || request.client_id}</h1>
          <p className="truncate font-mono text-xs opacity-70">{request.client_id} · code {request.user_code}</p>
        </div>
      </div>
      <div className="space-y-5 p-5">
        <p className="text-sm text-muted-foreground">Only approve if you just asked this agent to connect and the code matches what it showed you.</p>
        <section>
          <h2 className="font-semibold">It may</h2>
          <ul className="mt-2 space-y-1.5 text-sm">
            {request.permissions.map((p) => <li key={p} className="flex items-start gap-2"><Check className="mt-0.5 size-4 shrink-0 text-pass" />{p}</li>)}
          </ul>
        </section>
        <section>
          <h2 className="font-semibold">It may never</h2>
          <ul className="mt-2 space-y-1.5 text-sm">
            {request.never.map((p) => <li key={p} className="flex items-start gap-2"><X className="mt-0.5 size-4 shrink-0 text-fail" />{p}</li>)}
          </ul>
        </section>
        {pending ? (
          <div className="flex flex-wrap items-center gap-2 border-t pt-4">
            <Button size="lg" variant="outline" onClick={() => decide(false)} disabled={busy}>Deny</Button>
            <Button size="lg" onClick={() => decide(true)} disabled={busy} className="bg-brand text-brand-foreground hover:bg-brand/90">{busy ? "One sec…" : "Approve"}</Button>
            <span className="ml-auto text-xs text-muted-foreground">Request expires at {formatTime(request.expires_at)}</span>
          </div>
        ) : (
          <p className="rounded-lg bg-muted p-3 text-sm">This request was already {request.status}. Ask your agent for a new link if you need to connect again.</p>
        )}
      </div>
    </div>
  );
}
