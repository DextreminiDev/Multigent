"use client";

import { PanelRight, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Chat } from "@/lib/api/client";
import { useChat, useMe } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";
import { SettingsPanel } from "./settings-panel";
import { Conversation } from "./conversation";

function PanelHeading({ chat }: { chat: Chat }) {
  return <>{chat.role === "owner" ? "Chat settings" : "About this chat"}</>;
}

export function ChatScreen({ chatId }: { chatId: string }) {
  const chat = useChat(chatId);
  const me = useMe();
  const [panelOpen, setPanelOpen] = useState(true); // desktop
  const [sheetOpen, setSheetOpen] = useState(false); // mobile

  if (chat.isPending) {
    return (
      <div className="space-y-3 p-6">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-40 w-full max-w-xl" />
      </div>
    );
  }
  if (chat.isError) {
    const missing = chat.error instanceof ApiError && chat.error.status === 404;
    return (
      <div className="grid h-full place-items-center p-6 text-center">
        <div className="space-y-3">
          <p className="font-medium">{missing ? "Chat not found" : "Couldn't load this chat"}</p>
          <p className="text-sm text-muted-foreground">
            {missing
              ? "It may have been deleted, or you're no longer a member."
              : chat.error.message}
          </p>
          <Button variant="outline" render={<Link href="/" />}>
            Back to chats
          </Button>
        </div>
      </div>
    );
  }

  const c = chat.data;

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
          <h1 className="min-w-0 truncate font-medium">{c.title}</h1>
          {c.role !== "owner" && (
            <Badge variant="secondary" className="capitalize">
              {c.role}
            </Badge>
          )}
          <span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
            <Users className="size-3.5" aria-hidden />
            <span>
              {c.member_count}
              <span className="sr-only"> members</span>
            </span>
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="hidden lg:inline-flex"
            aria-label={panelOpen ? "Hide panel" : "Show panel"}
            aria-pressed={panelOpen}
            onClick={() => setPanelOpen((v) => !v)}
          >
            <PanelRight />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="lg:hidden"
            aria-label="Open chat settings"
            onClick={() => setSheetOpen(true)}
          >
            <PanelRight />
          </Button>
        </header>
        <main className="min-h-0 flex-1">
          {me.data ? <Conversation chat={c} meId={me.data.user.id} /> : null}
        </main>
      </div>

      <aside
        aria-label="Chat details"
        className={cn(
          "hidden w-88 shrink-0 overflow-y-auto border-l p-5",
          panelOpen && "lg:block",
        )}
      >
        <h2 className="mb-5 font-medium">
          <PanelHeading chat={c} />
        </h2>
        <SettingsPanel chat={c} />
      </aside>

      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent side="right" className="w-[92vw] max-w-md overflow-y-auto p-5">
          <SheetTitle>
            <PanelHeading chat={c} />
          </SheetTitle>
          <SettingsPanel chat={c} />
        </SheetContent>
      </Sheet>
    </div>
  );
}
