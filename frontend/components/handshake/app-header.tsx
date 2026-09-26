import Link from "next/link";
import { USE_MOCKS } from "@/lib/api";
import { Handshake } from "lucide-react";
import { UserMenu } from "./user-menu";

export function AppHeader() {
  return (
    <header className="sticky top-0 z-20 border-b bg-background/85 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-5xl items-center justify-between gap-4 px-4">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <span className="grid size-7 place-items-center rounded-md bg-brand text-brand-foreground"><Handshake className="size-4" /></span>
          Handshake
        </Link>
        <nav className="flex items-center gap-4 text-sm">
          <Link href="/contracts" className="text-muted-foreground hover:text-foreground">Contracts</Link>
          {USE_MOCKS && <span className="rounded bg-warn-soft px-1.5 py-0.5 font-mono text-[10px] font-semibold text-warn uppercase" title="Using mock data. Set NEXT_PUBLIC_USE_MOCKS=false to hit the backend.">Mock data</span>}
          <UserMenu />
        </nav>
      </div>
    </header>
  );
}
