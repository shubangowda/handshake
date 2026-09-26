"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { demoLogin } from "@/lib/api";
import { readSession, safeNext, writeSession } from "@/lib/session";
import { cn } from "@/lib/utils";
import {
  ArrowLeft, ArrowRight, Check, Footprints, Handshake, Headphones, Home as HomeIcon,
  Gift, Pizza, Plane, ShieldCheck, Shirt, Sparkles, Ticket,
} from "lucide-react";

const INTERESTS = [
  { id: "sneakers", label: "Sneakers & running gear", icon: Footprints },
  { id: "electronics", label: "Electronics", icon: Headphones },
  { id: "clothing", label: "Clothing", icon: Shirt },
  { id: "food", label: "Food & groceries", icon: Pizza },
  { id: "tickets", label: "Tickets & events", icon: Ticket },
  { id: "home", label: "Home goods", icon: HomeIcon },
  { id: "travel", label: "Travel", icon: Plane },
  { id: "gifts", label: "Gifts", icon: Gift },
  { id: "health", label: "Health & beauty", icon: Sparkles },
];

const BUDGETS = ["Under $50", "$50–$200", "$200–$1,000", "$1,000+"];

/** Left panel: a tiny signed contract, so the product is visible before you even log in. */
function BrandPanel() {
  return (
    <aside className="relative flex flex-col justify-between gap-10 overflow-hidden bg-brand p-6 text-brand-foreground sm:p-10 lg:min-h-svh">
      <div aria-hidden className="pointer-events-none absolute inset-0"
        style={{ background: "radial-gradient(circle at 18% 8%, rgba(68,125,98,.72), transparent 34%), radial-gradient(circle at 82% 20%, rgba(251,251,250,.12), transparent 28%), radial-gradient(circle at 48% 78%, rgba(9,48,35,.5), transparent 44%)" }} />
      <Link href="/" className="relative flex items-center gap-2 font-semibold tracking-tight">
        <span className="grid size-8 place-items-center rounded-md bg-white/15"><Handshake className="size-4" /></span>
        Handshake
      </Link>

      <div className="relative hidden max-w-sm lg:block">
        <div className="rotate-[-2deg] rounded-xl bg-white p-5 text-[#0c1212] shadow-2xl shadow-black/30">
          <p className="font-mono text-[10px] font-semibold tracking-[0.3em] text-[#6b716c]">HANDSHAKE</p>
          <p className="mt-1 text-xl font-semibold">Running shoes</p>
          <dl className="mt-4 space-y-2 text-sm">
            {[["Maximum total", "$135.00"], ["Size", "10"], ["Delivery", "By Monday"], ["Subscription", "Not allowed"]].map(([k, v]) => (
              <div key={k} className="flex justify-between border-b border-black/5 pb-2 last:border-0"><dt className="text-[#6b716c]">{k}</dt><dd className="font-medium">{v}</dd></div>
            ))}
          </dl>
          <p className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-pass px-2 py-1 font-mono text-[10px] font-semibold tracking-wider text-white"><ShieldCheck className="size-3" />SIGNED</p>
        </div>
      </div>

      <div className="relative">
        <p className="text-2xl leading-tight font-medium tracking-tight text-balance sm:text-3xl">Your agent shops.<br />You set the terms.</p>
        <p className="mt-2 max-w-sm text-sm text-white/75">Sign once. Every checkout is checked against what you signed, and nothing outside it gets bought.</p>
      </div>
    </aside>
  );
}

function StepDots({ step }: { step: 1 | 2 }) {
  return (
    <div className="flex items-center gap-2" aria-label={`Step ${step} of 2`}>
      {[1, 2].map((n) => <span key={n} className={cn("h-1.5 rounded-full transition-all", n === step ? "w-6 bg-brand" : "w-1.5 bg-border")} />)}
    </div>
  );
}

export default function LoginPage() {
  return (
    <main className="grid flex-1 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <BrandPanel />
      {/* useSearchParams (for ?next=) needs a Suspense boundary so the page can still prerender. */}
      <Suspense fallback={<section />}>
        <LoginForm />
      </Suspense>
    </main>
  );
}

function LoginForm() {
  const router = useRouter();
  // Where to go after login, e.g. /connect?code=... when an agent's login link sent the user here.
  const next = safeNext(useSearchParams().get("next"));
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [step, setStep] = useState<1 | 2>(1);
  const [email, setEmail] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [interests, setInterests] = useState<string[]>([]);
  const [budget, setBudget] = useState<string | null>(null);

  const emailError = !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && "Enter a valid email";

  async function submitCredentials(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (emailError) return;
    setBusy(true);
    setLoginError(null);
    try {
      // DEMO AUTH: POST /auth/demo-login trades an email for a token. No password.
      const login = await demoLogin(email);
      // expires_at arrives as Unix seconds; the session keeps an ISO string.
      writeSession({ email: login.email, token: login.token, expires_at: new Date(login.expires_at * 1000).toISOString(), interests: [], budget: null });
    } catch (err) {
      setLoginError((err as Error).message);
      return;
    } finally { setBusy(false); }
    // Someone mid-task (e.g. connecting an agent) goes straight back; onboarding can wait.
    if (next !== "/contracts") { toast.success("Signed in"); router.replace(next); return; }
    setStep(2);
  }

  function finish(skip = false) {
    const s = readSession();
    if (s) writeSession({ ...s, interests: skip ? [] : interests, budget: skip ? null : budget });
    toast.success(mode === "signup" ? "Account created" : "Welcome back");
    router.push(next);
  }

  const toggle = (id: string) => setInterests((xs) => (xs.includes(id) ? xs.filter((x) => x !== id) : [...xs, id]));

  return (
      <section className="flex items-center justify-center px-4 py-10 sm:px-8">
        <div className="w-full max-w-md">
          <StepDots step={step} />

          {step === 1 ? (
            <form onSubmit={submitCredentials} noValidate className="mt-6 animate-in fade-in duration-300">
              <h1 className="text-3xl font-semibold tracking-tight">{mode === "signin" ? "Welcome back" : "Create your account"}</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                {mode === "signin" ? "Sign in to see what your agent is allowed to buy." : "Set the rules once. Let your agent do the shopping."}
              </p>

              <p className="mt-6 rounded-lg border border-warn/40 bg-warn-soft px-3 py-2 text-sm font-medium text-warn">
                Demo login (no password; to be replaced by passkeys/OAuth)
              </p>

              <div className="mt-6 grid gap-4">
                <div className="grid gap-1.5">
                  <Label htmlFor="email">Email</Label>
                  <Input id="email" type="email" autoComplete="email" placeholder="you@example.com" className="h-10"
                    value={email} onChange={(e) => setEmail(e.target.value)} aria-invalid={touched && !!emailError} />
                  {touched && emailError && <p className="text-xs text-fail">{emailError}</p>}
                </div>
              </div>

              <Button type="submit" size="lg" disabled={busy} className="mt-6 h-10 w-full bg-brand text-brand-foreground hover:bg-brand/90">
                {busy ? "One sec…" : mode === "signin" ? "Sign in" : "Create account"}<ArrowRight />
              </Button>
              {loginError && <p className="mt-3 rounded-lg bg-fail-soft p-3 text-sm text-fail">{loginError}</p>}

              <p className="mt-6 text-center text-sm text-muted-foreground">
                {mode === "signin" ? "New to Handshake? " : "Already have an account? "}
                <button type="button" className="font-medium text-foreground underline-offset-4 hover:underline"
                  onClick={() => { setMode(mode === "signin" ? "signup" : "signin"); setTouched(false); }}>
                  {mode === "signin" ? "Create an account" : "Sign in"}
                </button>
              </p>
            </form>
          ) : (
            <div className="mt-6 animate-in fade-in slide-in-from-right-4 duration-300">
              <button type="button" onClick={() => setStep(1)} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="size-4" />Back</button>
              <h1 className="mt-3 text-3xl font-semibold tracking-tight text-balance">What would you have your agent buy first?</h1>
              <p className="mt-2 text-sm text-muted-foreground">Pick any. Handshake uses this to suggest sensible defaults, and you can always change them before signing.</p>

              <div className="mt-6 grid grid-cols-2 gap-2 sm:grid-cols-3" role="group" aria-label="Categories">
                {INTERESTS.map(({ id, label, icon: Icon }) => {
                  const on = interests.includes(id);
                  return (
                    <button key={id} type="button" aria-pressed={on} onClick={() => toggle(id)}
                      className={cn("relative flex min-h-24 flex-col items-start justify-between gap-3 rounded-xl border p-3 text-left text-sm font-medium transition",
                        on ? "border-brand bg-brand text-brand-foreground shadow-md" : "bg-card hover:border-foreground/30 hover:shadow-sm")}>
                      <Icon className={cn("size-5", on ? "text-white" : "text-muted-foreground")} />
                      {label}
                      {on && <Check className="absolute top-3 right-3 size-4" strokeWidth={3} />}
                    </button>
                  );
                })}
              </div>

              <p className="mt-6 text-sm font-medium">Usual spend per purchase</p>
              <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Usual spend">
                {BUDGETS.map((b) => (
                  <button key={b} type="button" role="radio" aria-checked={budget === b} onClick={() => setBudget(b)}
                    className={cn("h-9 rounded-full border px-4 text-sm transition",
                      budget === b ? "border-brand bg-brand text-brand-foreground" : "bg-card hover:border-foreground/30")}>
                    {b}
                  </button>
                ))}
              </div>

              <div className="mt-8 flex items-center justify-between gap-3">
                <button type="button" onClick={() => finish(true)} className="text-sm text-muted-foreground hover:text-foreground">Skip for now</button>
                <Button size="lg" onClick={() => finish()} disabled={!interests.length} className="h-10 bg-brand text-brand-foreground hover:bg-brand/90">
                  Continue{interests.length > 0 && ` · ${interests.length} picked`}<ArrowRight />
                </Button>
              </div>
            </div>
          )}

          <p className="mt-10 text-center text-xs text-muted-foreground">Demo login: anyone who types an email gets a session. No real account is created.</p>
        </div>
      </section>
  );
}
