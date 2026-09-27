import Link from "next/link";

/**
 * The small footer on every page. Privacy and Terms must be reachable without logging in: agent directories
 * (Muse's included) link to them before anyone has an account.
 */
export function SiteFooter() {
  return (
    <footer className="border-t text-xs text-muted-foreground">
      <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-x-4 gap-y-1 px-4 py-4">
        <p>Handshake · a prototype · test-mode payments only</p>
        <nav className="flex gap-4" aria-label="Legal">
          <Link href="/privacy" className="hover:text-foreground">Privacy</Link>
          <Link href="/terms" className="hover:text-foreground">Terms</Link>
        </nav>
      </div>
    </footer>
  );
}
