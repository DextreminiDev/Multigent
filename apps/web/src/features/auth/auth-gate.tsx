"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useMe } from "@/lib/api/hooks";
import type { Me } from "@/lib/api/client";
import { Button } from "@/components/ui/button";

/** Renders children only for a signed-in user; otherwise sends them to /login
 *  with `next` set so they return to the same page (incl. invite links later). */
export function AuthGate({ children }: { children: (me: Me) => React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (me.data === null) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [me.data, pathname, router]);

  if (me.isError) {
    return (
      <div className="grid min-h-dvh place-items-center p-4 text-center">
        <div className="space-y-3">
          <p className="text-muted-foreground">Can&apos;t reach the server.</p>
          <Button variant="outline" onClick={() => me.refetch()}>
            Try again
          </Button>
        </div>
      </div>
    );
  }
  if (!me.data) {
    return (
      <div className="grid min-h-dvh place-items-center" aria-busy="true">
        <span className="size-2 animate-pulse rounded-full bg-primary" />
      </div>
    );
  }
  return <>{children(me.data)}</>;
}
