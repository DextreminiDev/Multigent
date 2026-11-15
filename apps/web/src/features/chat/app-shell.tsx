"use client";

import { Menu } from "lucide-react";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import type { Me } from "@/lib/api/client";
import { Sidebar } from "./sidebar";

export function AppShell({ me, children }: { me: Me; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  useEffect(() => setOpen(false), [pathname]);

  return (
    <div className="flex h-dvh overflow-hidden">
      <aside className="hidden w-68 shrink-0 border-r border-sidebar-border md:block">
        <Sidebar me={me} />
      </aside>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="left" className="w-[85vw] max-w-xs p-0">
          <SheetTitle className="sr-only">Chats</SheetTitle>
          <Sidebar me={me} onNavigate={() => setOpen(false)} />
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-12 shrink-0 items-center gap-2 border-b px-2 md:hidden">
          <Button variant="ghost" size="icon" aria-label="Open chats" onClick={() => setOpen(true)}>
            <Menu />
          </Button>
          <Logo />
        </div>
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
