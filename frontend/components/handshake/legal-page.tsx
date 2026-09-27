import type { ReactNode } from "react";
import { AppHeader } from "./app-header";

export const CONTACT_EMAIL = "shubansbackup@gmail.com";
export const LEGAL_UPDATED = "September 27, 2026";

/** Shared frame for /privacy and /terms: public pages (no login), plain text, easy to read on a phone. */
export function LegalPage({ title, intro, children }: { title: string; intro: string; children: ReactNode }) {
  return (
    <>
      <AppHeader />
      <main className="mx-auto w-full max-w-2xl flex-1 px-4 py-10">
        <p className="font-mono text-[11px] font-semibold tracking-widest text-muted-foreground uppercase">Last updated {LEGAL_UPDATED}</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight">{title}</h1>
        <p className="mt-3 text-muted-foreground">{intro}</p>
        <div className="mt-8 space-y-8 text-sm leading-relaxed [&_h2]:text-base [&_h2]:font-semibold [&_li]:mt-1.5 [&_p]:mt-2 [&_ul]:mt-2 [&_ul]:list-disc [&_ul]:pl-5">
          {children}
        </div>
      </main>
    </>
  );
}

export function ContactLine() {
  return <a className="font-medium underline underline-offset-4" href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>;
}
