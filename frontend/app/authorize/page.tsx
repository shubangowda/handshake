"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AppHeader } from "@/components/handshake/app-header";
import { Button } from "@/components/ui/button";
import { ApiError, decideAuthorization, getAuthorizationRequest } from "@/lib/api";
import { formatTime } from "@/lib/format";
import { readSession } from "@/lib/session";
import type { OAuthAuthorizationRequest } from "@/lib/types";
import { Bot, Check, Clock, Loader2, OctagonX, Undo2, X } from "lucide-react";

/**
 * The consent screen of the OAuth 2.1 authorization code flow (how Muse and other MCP clients connect).
 * The backend's GET /oauth/authorize validates the client and its redirect_uri, then sends the browser
 * here with ?request_id=. The user sees who is asking and what it may and may never do, then approves
 * or denies; either way the backend answers with the client's redirect, which we follow.
 */
export default function AuthorizePage() {
  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-xl flex-1 px-4 py-10">
        {/* useSearchParams needs a Suspense boundary so the page can still prerender. */}
        <Suspense fallback={<div className="h-72 animate-pulse rounded-xl bg-muted" />}>
          <AuthorizeRequest />
        </Suspense>
      </main>
    </>
  );
}

// Fixed wording on purpose: this is what the API lets an agent token do, whatever scopes a client asks for.
const CAN = [
  "Draft contracts for you to review and sign",
  "Request purchases under contracts you signed",
  "Collect a funded card only for checkouts Handshake approves",
];
const CANNOT = ["Sign contracts", "Approve exceptions", "Change your limits"];

/** Schemes that run code or read local data: never navigate to them, whatever the backend says. */
const FORBIDDEN_SCHEMES = new Set(["javascript:", "data:", "file:", "vbscript:", "about:", "blob:"]);

/**
 * The backend builds redirect_to from the client's registered redirect_uri, but it is still a URL we're
 * about to navigate to. Follow http(s) only to the host the consent screen showed the user; native
 * apps may use their own scheme (e.g. cursor://, which the backend also allows); never script schemes.
 */
function safeRedirect(redirectTo: string, expectedHost: string): string | null {
  try {
    const url = new URL(redirectTo);
    if (FORBIDDEN_SCHEMES.has(url.protocol)) return null;
    if (url.protocol !== "https:" && url.protocol !== "http:") return expectedHost === url.protocol ? url.href : null;
    if (expectedHost && url.host !== expectedHost && url.hostname !== expectedHost) return null;
    return url.href;
  } catch {
    return null;
  }
}

type Failure = { title: string; message: string; expired?: boolean };

function failureOf(e: unknown): Failure {
  if (e instanceof ApiError && e.code === "authorization_request_expired") {
    return { title: "This request expired", message: "Sign-in requests only last a few minutes. Go back to your agent and connect Handshake again.", expired: true };
  }
  if (e instanceof ApiError && e.code === "authorization_request_not_found") {
    return { title: "Request not found", message: "This sign-in request doesn't exist or was already used. Go back to your agent and connect Handshake again." };
  }
  return { title: "Can't connect this agent", message: (e as Error).message };
}

function AuthorizeRequest() {
  const router = useRouter();
  const requestId = (useSearchParams().get("request_id") ?? "").trim();
  const [request, setRequest] = useState<OAuthAuthorizationRequest | null>(null);
  const [failure, setFailure] = useState<Failure | null>(requestId ? null : {
    title: "Missing request", message: "This link has no sign-in request. Start connecting Handshake again from your agent.",
  });
  const [busy, setBusy] = useState(false);
  // Set once decided: the page then only says where it's sending the user.
  const [returning, setReturning] = useState<{ approved: boolean; href: string } | null>(null);

  useEffect(() => {
    if (!requestId) return;
    // Only a logged-in user can let an agent act for them; come back to this exact request after login.
    if (!readSession()) {
      router.replace(`/login?next=${encodeURIComponent(`/authorize?request_id=${encodeURIComponent(requestId)}`)}`);
      return;
    }
    getAuthorizationRequest(requestId).then(setRequest, (e) => setFailure(failureOf(e)));
  }, [requestId, router]);

  async function decide(approve: boolean) {
    if (!request) return;
    setBusy(true);
    try {
      const { redirect_to } = await decideAuthorization(request.request_id, approve);
      const href = safeRedirect(redirect_to, request.redirect_host);
      if (!href) {
        setFailure({ title: "Unexpected return address", message: `Handshake recorded your choice, but the address it got back doesn't point to ${request.redirect_host}, so it won't send you there. Go back to your agent manually.` });
        return;
      }
      setReturning({ approved: approve, href });
      window.location.assign(href);
    } catch (e) {
      setFailure(failureOf(e));
      setBusy(false);
    }
  }

  if (returning && request) {
    return (
      <div className="rounded-2xl border bg-card p-6 text-center animate-in fade-in zoom-in-95 duration-300" role="status">
        {returning.approved
          ? <Check className="mx-auto size-10 text-pass" />
          : <OctagonX className="mx-auto size-10 text-muted-foreground" />}
        <h1 className="mt-3 text-2xl font-semibold">{returning.approved ? `${request.client_name} is connected` : "Request denied"}</h1>
        <p className="mt-2 flex items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />Returning you to {request.redirect_host}…
        </p>
        <a href={returning.href} className="mt-5 inline-block text-sm underline underline-offset-4">Not redirected? Continue to {request.redirect_host}</a>
      </div>
    );
  }

  if (failure) {
    const Icon = failure.expired ? Clock : OctagonX;
    return (
      <div className="rounded-2xl border border-fail/40 bg-fail-soft p-6 text-fail">
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Icon className="size-5" />{failure.title}</h1>
        <p className="mt-2 text-sm">{failure.message}</p>
        <Link href="/agents" className="mt-4 inline-block text-sm text-foreground underline underline-offset-4">See your connected agents</Link>
      </div>
    );
  }

  if (!request) return <div className="h-72 animate-pulse rounded-xl bg-muted" />;

  return (
    <div className="overflow-hidden rounded-2xl border bg-card shadow-sm">
      <div className="flex items-center gap-3 border-b bg-brand px-5 py-5 text-brand-foreground">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-white/15"><Bot className="size-5" /></span>
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-balance"><span className="break-words">{request.client_name}</span> wants to act as your shopping agent in Handshake</h1>
          <p className="truncate font-mono text-xs opacity-70">{request.client_id}</p>
        </div>
      </div>
      <div className="space-y-5 p-5">
        <p className="text-sm text-muted-foreground">Only approve if you just asked {request.client_name} to connect to Handshake.</p>
        <section>
          <h2 className="font-semibold">It can</h2>
          <ul className="mt-2 space-y-1.5 text-sm">
            {CAN.map((p) => <li key={p} className="flex items-start gap-2"><Check className="mt-0.5 size-4 shrink-0 text-pass" />{p}</li>)}
          </ul>
        </section>
        <section>
          <h2 className="font-semibold">It can never</h2>
          <ul className="mt-2 space-y-1.5 text-sm">
            {CANNOT.map((p) => <li key={p} className="flex items-start gap-2"><X className="mt-0.5 size-4 shrink-0 text-fail" />{p}</li>)}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">Those stay with you. You can disconnect it any time on the Agents page.</p>
        </section>
        <p className="flex items-start gap-2 rounded-lg bg-muted p-3 text-sm">
          <Undo2 className="mt-0.5 size-4 shrink-0" />
          <span>Either way, you&apos;ll be sent back to <b className="break-all">{request.redirect_host}</b>.</span>
        </p>
        {request.scopes.length > 0 && (
          <p className="font-mono text-[11px] text-muted-foreground">Scopes: {request.scopes.join(" · ")}</p>
        )}
        <div className="flex flex-wrap items-center gap-2 border-t pt-4">
          <Button size="lg" variant="outline" onClick={() => decide(false)} disabled={busy}>Deny</Button>
          <Button size="lg" onClick={() => decide(true)} disabled={busy} className="bg-brand text-brand-foreground hover:bg-brand/90">{busy ? "One sec…" : "Approve"}</Button>
          <span className="ml-auto text-xs text-muted-foreground">Request expires at {formatTime(request.expires_at)}</span>
        </div>
      </div>
    </div>
  );
}
