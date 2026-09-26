"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useSyncExternalStore } from "react";
import { clearSession, readSession } from "@/lib/session";

// localStorage has no change events within the same tab, so snapshot once per render pass.
const subscribe = () => () => {};
const getSnapshot = () => { try { return localStorage.getItem("handshake.session"); } catch { return null; } };
const getServerSnapshot = () => null;

export function useSession() {
  const raw = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return raw ? readSession() : null;
}

export function UserMenu() {
  const session = useSession();
  const router = useRouter();
  if (!session) {
    return <Link href="/login" className="rounded-md bg-brand px-3 py-1.5 text-sm font-medium text-brand-foreground hover:bg-brand/90">Sign in</Link>;
  }
  return (
    <div className="flex items-center gap-2">
      <span title={session.email} className="grid size-7 place-items-center rounded-full bg-brand text-xs font-semibold text-brand-foreground uppercase">
        {session.email[0]}
      </span>
      <button type="button" className="text-sm text-muted-foreground hover:text-foreground"
        onClick={() => { clearSession(); router.push("/login"); }}>
        Sign out
      </button>
    </div>
  );
}
