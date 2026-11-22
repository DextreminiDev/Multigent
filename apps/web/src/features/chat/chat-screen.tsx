"use client";

import { PanelRight, UserPlus, Users } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Chat } from "@/lib/api/client";
import { useChat, useMe, useMembers } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";
import { Conversation } from "./conversation";
import { InviteDialog } from "./invite-dialog";
import { ContextMeter } from "./limits";
import type { ContextInfo } from "@/lib/ws/reducer";
import { Participants, PresenceStack } from "./participants";
import { SettingsPanel } from "./settings-panel";
import { useLiveChat } from "./use-live-chat";

function PanelHeading({ chat }: { chat: Chat }) {
  return <>{chat.role === "owner" ? "Chat settings" : "About this chat"}</>;
}

function Panel({
  chat,
  meId,
  online,
  onInvite,
  context,
}: {
  chat: Chat;
  meId: string;
  online: string[] | null;
  onInvite: () => void;
  context: ContextInfo | null;
}) {
  return (
    <div className="space-y-6">
      <Participants chat={chat} meId={meId} online={online} onInvite={onInvite} />
      <Separator />
      <ContextMeter chat={chat} live={context} />
      <Separator />
      <SettingsPanel chat={chat} />
    </div>
  );
}

export function ChatScreen({ chatId }: { chatId: string }) {
  const chat = useChat(chatId);
  const me = useMe();
  const live = useLiveChat(chatId);
  const members = useMembers(chatId);
  const [panelOpen, setPanelOpen] = useState(true); // desktop
  const [sheetOpen, setSheetOpen] = useState(false); // mobile
  const [inviteOpen, setInviteOpen] = useState(false);

  const memberIds = useMemo(
    () => (members.data ? new Set(members.data.map((m) => m.user_id)) : null),
    [members.data],
  );
  // live presence once the socket has reported it, else the REST snapshot
  const online = live.state && live.state.online.length > 0 ? live.state.online : null;

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
  const meId = me.data?.user.id ?? "";
  const openInvite = () => setInviteOpen(true);

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
          <div className="ml-auto flex items-center gap-2">
            {live.state && <PresenceStack chatId={c.id} online={live.state.online} />}
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <Users className="size-3.5" aria-hidden />
              <span>
                {members.data?.length ?? c.member_count}
                <span className="sr-only"> members</span>
              </span>
            </span>
            {c.role === "owner" && (
              <Button variant="ghost" size="icon" aria-label="Invite people" onClick={openInvite}>
                <UserPlus />
              </Button>
            )}
          </div>
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
            aria-label="Open people and settings"
            onClick={() => setSheetOpen(true)}
          >
            <PanelRight />
          </Button>
        </header>
        <main className="min-h-0 flex-1">
          {me.data ? (
            <Conversation
              chat={c}
              meId={meId}
              live={live}
              memberIds={memberIds}
              onInvite={openInvite}
            />
          ) : null}
        </main>
      </div>

      <aside
        aria-label="Chat details"
        className={cn("hidden w-88 shrink-0 overflow-y-auto border-l p-5", panelOpen && "lg:block")}
      >
        <h2 className="mb-5 font-medium">
          <PanelHeading chat={c} />
        </h2>
        <Panel
          chat={c}
          meId={meId}
          online={online}
          onInvite={openInvite}
          context={live.state?.context ?? null}
        />
      </aside>

      {/* mobile: bottom sheet (people often join from invite links on phones) */}
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent side="bottom" className="max-h-[85dvh] overflow-y-auto rounded-t-2xl p-5">
          <SheetTitle>
            <PanelHeading chat={c} />
          </SheetTitle>
          <Panel
            chat={c}
            meId={meId}
            online={online}
            context={live.state?.context ?? null}
            onInvite={() => {
              setSheetOpen(false);
              openInvite();
            }}
          />
        </SheetContent>
      </Sheet>

      {c.role === "owner" && (
        <InviteDialog chatId={c.id} open={inviteOpen} onOpenChange={setInviteOpen} />
      )}
    </div>
  );
}
