"use client";

import { KeyRound, Loader2, MessageSquareDashed, WifiOff } from "lucide-react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Skeleton } from "@/components/ui/skeleton";
import type { Chat } from "@/lib/api/client";
import { qk } from "@/lib/api/hooks";
import { queuedCount, typingLabel, typingNames, type LiveState } from "@/lib/ws/reducer";
import { Composer } from "./composer";
import { MessageList } from "./message-list";
import { isChatReady, SetupChecklist } from "./setup-checklist";
import type { LiveChat } from "./use-live-chat";

export function composerBlockReason(chat: Chat): string | null {
  if (chat.role === "viewer") return "You're a viewer here: you can read along live, but not send messages.";
  return null;
}

export function pauseNotice(chat: Chat): string | null {
  if (chat.status === "paused_key" || chat.key?.status === "invalid") {
    return chat.role === "owner"
      ? "Your API key was rejected. Replace it in chat settings; queued messages will then be answered."
      : "The owner's API key needs attention. Messages will wait until it's fixed.";
  }
  if (!isChatReady(chat)) {
    return chat.role === "owner"
      ? "Add an API key and pick a model to get replies."
      : "The owner hasn't finished setting up this chat yet. Messages will wait.";
  }
  return null;
}

function Banner({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div role="status" className="mx-auto mb-2 flex w-full max-w-3xl items-center gap-2 rounded-lg bg-muted px-3 py-2 text-sm text-muted-foreground">
      {icon}
      <span>{children}</span>
    </div>
  );
}

function TypingIndicator({ state, meId }: { state: LiveState; meId: string }) {
  // re-render every second while someone is typing so stale entries expire
  const [, tick] = useState(0);
  const active = typingNames(state, meId);
  useEffect(() => {
    if (active.length === 0) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [active.length]);
  const label = typingLabel(active);
  return (
    <div className="mx-auto h-5 w-full max-w-3xl px-1 text-xs text-muted-foreground" aria-live="polite">
      {label}
    </div>
  );
}

export function Conversation({
  chat,
  meId,
  live,
  memberIds,
  onInvite,
}: {
  chat: Chat;
  meId: string;
  live: LiveChat;
  memberIds: Set<string> | null;
  onInvite: () => void;
}) {
  const router = useRouter();
  const qc = useQueryClient();

  useEffect(() => {
    if (live.state?.deleted) {
      toast("This chat was deleted by its owner.");
      qc.invalidateQueries({ queryKey: qk.chats, exact: true });
      router.replace("/");
    } else if (live.state?.kicked) {
      toast("You're no longer a member of this chat.");
      qc.invalidateQueries({ queryKey: qk.chats, exact: true });
      qc.removeQueries({ queryKey: qk.chat(chat.id) });
      router.replace("/");
    }
  }, [live.state?.deleted, live.state?.kicked, router, qc, chat.id]);

  if (live.error) {
    return (
      <div className="grid h-full place-items-center p-6 text-sm text-destructive">{live.error}</div>
    );
  }
  if (!live.state) {
    return (
      <div className="mx-auto w-full max-w-3xl space-y-4 p-6" aria-busy="true">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="ml-auto h-10 w-1/2" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  const pause = pauseNotice(chat);
  const blocked = composerBlockReason(chat);
  const waiting = queuedCount(live.state);
  const ownerSetup = chat.role === "owner" && !isChatReady(chat);

  const header = ownerSetup ? (
    <div className="pb-4 sm:pt-4">
      <SetupChecklist chat={chat} onInvite={onInvite} />
    </div>
  ) : null;

  const empty = ownerSetup ? null : (
    <div className="grid place-items-center py-24 text-center">
      <div className="max-w-sm space-y-2">
        <MessageSquareDashed className="mx-auto size-8 text-muted-foreground" aria-hidden />
        <p className="font-medium">No messages yet</p>
        <p className="text-sm text-muted-foreground">
          Say hi. Everyone in the chat sees the conversation live, and the AI answers each person by
          name.
        </p>
      </div>
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <MessageList
        state={live.state}
        meId={meId}
        canSend={!blocked}
        paused={Boolean(pause)}
        onRetrySend={live.retrySend}
        onDiscardSend={live.discardSend}
        memberIds={memberIds}
        header={header}
        empty={empty}
      />
      <div className="shrink-0 px-4 pt-2 pb-3">
        {live.connection === "reconnecting" && (
          <Banner icon={<WifiOff className="size-4" />}>
            Reconnecting… messages you send will go out when the connection is back.
          </Banner>
        )}
        {pause && !ownerSetup && (
          <Banner icon={<KeyRound className="size-4" />}>{pause}</Banner>
        )}
        {waiting > 0 && live.state.turn && (
          <Banner icon={<Loader2 className="size-4 animate-spin" />}>
            {waiting} {waiting === 1 ? "message" : "messages"} waiting for the next reply
          </Banner>
        )}
        <TypingIndicator state={live.state} meId={meId} />
        <div className="mx-auto w-full max-w-3xl">
          <Composer
            onSend={live.send}
            onType={live.notifyTyping}
            disabledReason={blocked}
            hint={chat.role !== "owner" ? "Replies use the owner's key, even when they're offline." : null}
          />
        </div>
      </div>
    </div>
  );
}
