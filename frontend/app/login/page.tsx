"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { writeSession } from "@/lib/session";
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
  const router = useRouter();
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [step, setStep] = useState<1 | 2>(1);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [interests, setInterests] = useState<string[]>([]);
  const [budget, setBudget] = useState<string | null>(null);

  const emailError = !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && "Enter a valid email";
  const passwordError = password.length < 8 && "At least 8 characters";

  async function submitCredentials(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (emailError || passwordError) return;
    setBusy(true);
    await new Promise((r) => setTimeout(r, 500)); // PLACEHOLDER: real auth call goes here.
    setBusy(false);
    setPassword("");
    setStep(2);
  }

  function finish(skip = false) {
    writeSession({ email, interests: skip ? [] : interests, budget: skip ? null : budget });
    toast.success(mode === "signup" ? "Account created" : "Welcome back");
    router.push("/contracts");
  }

  const toggle = (id: string) => setInterests((xs) => (xs.includes(id) ? xs.filter((x) => x !== id) : [...xs, id]));

  return (
    <main className="grid flex-1 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <BrandPanel />

      <section className="flex items-center justify-center px-4 py-10 sm:px-8">
        <div className="w-full max-w-md">
          <StepDots step={step} />

          {step === 1 ? (
            <form onSubmit={submitCredentials} noValidate className="mt-6 animate-in fade-in duration-300">
              <h1 className="text-3xl font-semibold tracking-tight">{mode === "signin" ? "Welcome back" : "Create your account"}</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                {mode === "signin" ? "Sign in to see what your agent is allowed to buy." : "Set the rules once. Let your agent do the shopping."}
              </p>

              <Button type="button" variant="outline" size="lg" className="mt-6 w-full"
                onClick={() => toast("Google sign-in isn't connected yet", { description: "Use email for now." })}>
                <svg viewBox="0 0 24 24" className="size-4" aria-hidden><path fill="#4285F4" d="M22.5 12.3c0-.8-.1-1.5-.2-2.2H12v4.2h5.9a5 5 0 0 1-2.2 3.3v2.7h3.5c2.1-1.9 3.3-4.7 3.3-8z" /><path fill="#34A853" d="M12 23c3 0 5.5-1 7.3-2.7l-3.5-2.7c-1 .7-2.3 1.1-3.8 1.1-2.9 0-5.4-2-6.3-4.6H2.1v2.8A11 11 0 0 0 12 23z" /><path fill="#FBBC05" d="M5.7 14.1a6.6 6.6 0 0 1 0-4.2V7.1H2.1a11 11 0 0 0 0 9.8l3.6-2.8z" /><path fill="#EA4335" d="M12 5.4c1.6 0 3.1.6 4.2 1.7l3.1-3.1A11 11 0 0 0 2.1 7.1l3.6 2.8C6.6 7.4 9.1 5.4 12 5.4z" /></svg>
                Continue with Google
              </Button>

              <div className="my-6 flex items-center gap-3 text-xs text-muted-foreground"><span className="h-px flex-1 bg-border" />or with email<span className="h-px flex-1 bg-border" /></div>

              <div className="grid gap-4">
                <div className="grid gap-1.5">
                  <Label htmlFor="email">Email</Label>
                  <Input id="email" type="email" autoComplete="email" placeholder="you@example.com" className="h-10"
                    value={email} onChange={(e) => setEmail(e.target.value)} aria-invalid={touched && !!emailError} />
                  {touched && emailError && <p className="text-xs text-fail">{emailError}</p>}
                </div>
                <div className="grid gap-1.5">
                  <div className="flex items-baseline justify-between">
                    <Label htmlFor="password">Password</Label>
                    {mode === "signin" && <button type="button" className="text-xs text-muted-foreground hover:text-foreground" onClick={() => toast("Password reset isn't set up yet")}>Forgot?</button>}
                  </div>
                  <Input id="password" type="password" autoComplete={mode === "signin" ? "current-password" : "new-password"} className="h-10"
                    value={password} onChange={(e) => setPassword(e.target.value)} aria-invalid={touched && !!passwordError} />
                  {touched && passwordError && <p className="text-xs text-fail">{passwordError}</p>}
                </div>
              </div>

              <Button type="submit" size="lg" disabled={busy} className="mt-6 h-10 w-full bg-brand text-brand-foreground hover:bg-brand/90">
                {busy ? "One sec…" : mode === "signin" ? "Sign in" : "Create account"}<ArrowRight />
              </Button>

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

          <p className="mt-10 text-center text-xs text-muted-foreground">Demo sign-in. No real account is created yet.</p>
        </div>
      </section>
    </main>
  );
}
