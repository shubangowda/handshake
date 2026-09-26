"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import GlyphPortal from "@/components/ui/glyph-portal";
import { USE_MOCKS } from "@/lib/api";
import { ArrowRight, FileSignature, Bot, ShieldCheck, Handshake } from "lucide-react";

const FALLBACK = '"Arial Black", Arial, sans-serif';

/** GlyphPortal freezes the face at mount, so wait for Geist (or give up after 1.5s). */
function usePortalFont() {
  const [face, setFace] = useState<string | null>(null);
  useEffect(() => {
    let done = false;
    const finish = (f: string) => { if (!done) { done = true; setFace(f); } };
    const family = getComputedStyle(document.documentElement).getPropertyValue("--font-geist-sans").trim();
    if (!family) { finish(FALLBACK); return; }
    const timer = window.setTimeout(() => finish(FALLBACK), 1500);
    document.fonts.load(`900 100px ${family}`, "HANDSHAKE")
      .then((faces) => finish(faces.length ? `${family}, ${FALLBACK}` : FALLBACK), () => finish(FALLBACK));
    return () => { done = true; clearTimeout(timer); };
  }, []);
  return face;
}

const STEPS = [
  { icon: FileSignature, title: "You sign a contract", body: "Plain English, not JSON. Max spend, size, delivery, no subscriptions. Anything Handshake guessed is flagged before you sign." },
  { icon: Bot, title: "Your agent shops", body: "It can browse anywhere, but it never holds your card. It can only propose a checkout." },
  { icon: ShieldCheck, title: "Handshake checks the checkout", body: "Every line is compared to what you signed. If it all passes, you approve the payment in Link and a single-use card pays it. Any failure blocks it. If Handshake can't verify something, it asks you." },
];

export default function Home() {
  const face = usePortalFont();

  return (
    <main data-hs-landing className="flex-1 bg-white">
      <style>{`
        [data-hs-landing] [data-gp-caption]{inset:calc(var(--gp-word-bottom,50%) + 88px) 24px auto;justify-content:center;}
        [data-hs-landing] [data-gp-hint]{display:none;}
        [data-hs-landing] [data-gp-enter]{min-height:46px;padding:0 20px;gap:20px;background:#0b3b2a;border-radius:10px;color:#fff;font-size:14px;font-weight:500;transition:background .18s;}
        [data-hs-landing] [data-gp-enter]:hover{background:#14573f;}
        [data-hs-landing] [data-gp-touch-picker]{top:auto;bottom:18px;}
        [data-hs-head]{position:absolute;inset:24px clamp(16px,5vw,64px) auto;display:flex;align-items:center;justify-content:space-between;gap:16px;}
        [data-hs-eyebrow]{position:absolute;inset:auto 16px calc(100% - var(--gp-word-top,35%) + 28px);margin:0;text-align:center;}
        [data-hs-support]{position:absolute;inset:calc(var(--gp-word-bottom,50%) + 28px) 16px auto;margin:0;text-align:center;}
        [data-hs-scroll]{position:absolute;inset:auto 16px 6%;text-align:center;}
        [data-hs-landing] [data-gp-content]{font-family:inherit;padding:5rem clamp(1rem,5vw,5rem) 6rem;}
      `}</style>

      {face ? (
        <GlyphPortal
          word="HANDSHAKE"
          fontFamily={face}
          fontWeight={900}
          scrollLength={2.4}
          enterLabel="See how it works"
          style={{ fontFamily: "var(--font-geist-sans), Arial, sans-serif" }}
          front={<>
            <div data-hs-head>
              <span className="flex items-center gap-2 text-[17px] font-semibold tracking-tight text-[#0c1212]">
                <span className="grid size-7 place-items-center rounded-md bg-[#0b3b2a] text-white"><Handshake className="size-4" /></span>
                Handshake
              </span>
              <Link href="/login" className="rounded-md bg-[#0b3b2a] px-3 py-1.5 text-sm font-medium text-white hover:bg-[#14573f]">Sign in</Link>
            </div>
            <p data-hs-eyebrow className="text-sm text-[#6b716c]">Your agent shops. You set the terms.</p>
            <p data-hs-support className="text-base text-[#4a524d] sm:text-lg">Sign once. Every checkout is checked against what you signed.</p>
            <span data-hs-scroll className="text-xs text-[#7c817b]">Scroll to step inside ↓</span>
          </>}
        >
          <div className="mx-auto flex w-full max-w-5xl flex-col gap-12">
            <h2 className="max-w-2xl text-3xl leading-tight font-medium tracking-tight text-balance sm:text-4xl">
              An AI agent with your card can buy anything. With Handshake, it can only buy what you signed for.
            </h2>
            <div className="grid gap-8 sm:grid-cols-3 sm:gap-10">
              {STEPS.map(({ icon: Icon, title, body }, i) => (
                <div key={title} className="border-t border-white/25 pt-4">
                  <p className="flex items-center gap-2 font-mono text-xs whitespace-nowrap text-white/70"><Icon className="size-4" />0{i + 1}</p>
                  <h3 className="mt-2 text-lg font-medium [overflow-wrap:normal]">{title}</h3>
                  <p className="mt-2 text-[15px] leading-relaxed text-white/80">{body}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-3">
              <Link href="/login" className="inline-flex h-11 items-center gap-2 rounded-lg bg-white px-5 text-sm font-semibold text-[#0b3b2a] hover:bg-white/90">
                Get started <ArrowRight className="size-4" />
              </Link>
              {/* The sample ids only exist in the mock store. */}
              {USE_MOCKS && <Link href="/contracts/draft_shoes01" className="inline-flex h-11 items-center gap-2 rounded-lg border border-white/40 px-5 text-sm font-medium text-white hover:bg-white/10">
                Review a sample contract
              </Link>}
              <Link href="/contracts" className="inline-flex h-11 items-center gap-2 rounded-lg border border-white/40 px-5 text-sm font-medium text-white hover:bg-white/10">
                Contracts dashboard
              </Link>
              {USE_MOCKS && <Link href="/purchases/purchase_blocked" className="inline-flex h-11 items-center gap-2 rounded-lg border border-white/40 px-5 text-sm font-medium text-white hover:bg-white/10">
                Watch it stop a bad merchant
              </Link>}
            </div>
          </div>
        </GlyphPortal>
      ) : (
        <div role="status" className="grid h-svh place-items-center text-xs text-muted-foreground">Loading…</div>
      )}
    </main>
  );
}
