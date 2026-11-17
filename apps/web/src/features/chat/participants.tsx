"use client";

import { LogOut, UserPlus, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Avatar } from "@/components/avatar";
import { Button } from "@/components/ui/button";
import type { Chat } from "@/lib/api/client";
import { type Member, useChangeRole, useMembers, useRemoveMember } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";

function OnlineDot({ online }: { online: boolean }) {
  return (
    <span
      className={cn(
        "absolute -right-0.5 -bottom-0.5 size-2.5 rounded-full border-2 border-background",
        online ? "bg-ok" : "bg-muted-foreground/40",
      )}
      aria-label={online ? "online" : "offline"}
    />
  );
}

/** Online state comes from live presence when available, else the REST snapshot. */
export function isOnline(m: Member, live: string[] | null): boolean {
  return live ? live.includes(m.user_id) : m.online;
}

export function Participants({
  chat,
  meId,
  online,
  onInvite,
}: {
  chat: Chat;
  meId: string;
  online: string[] | null;
  onInvite: () => void;
}) {
  const members = useMembers(chat.id);
  const changeRole = useChangeRole(chat.id);
  const remove = useRemoveMember(chat.id);
  const router = useRouter();
  const owner = chat.role === "owner";

  return (
    <section className="space-y-3" aria-labelledby="participants-title">
      <div className="flex items-center justify-between">
        <h3
          id="participants-title"
          className="text-xs font-medium uppercase tracking-wider text-muted-foreground"
        >
          People · {members.data?.length ?? chat.member_count}
        </h3>
        {owner && (
          <Button size="sm" variant="outline" onClick={onInvite}>
            <UserPlus /> Invite
          </Button>
        )}
      </div>
      <ul className="space-y-1">
        {members.data?.map((m) => {
          const me = m.user_id === meId;
          return (
            <li key={m.user_id} className="flex items-center gap-2.5 rounded-md py-1">
              <span className="relative">
                <Avatar name={m.name} color={m.avatar_color} />
                <OnlineDot online={isOnline(m, online)} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">
                  {m.name}
                  {me && <span className="font-normal text-muted-foreground"> (you)</span>}
                </span>
                {!(owner && m.role !== "owner") && (
                  <span className="block text-xs capitalize text-muted-foreground">{m.role}</span>
                )}
              </span>
              {owner && m.role !== "owner" && (
                <>
                  <select
                    aria-label={`Role for ${m.name}`}
                    className="h-7 rounded-md border border-input bg-transparent px-1.5 text-xs dark:bg-input/30"
                    value={m.role}
                    disabled={changeRole.isPending}
                    onChange={(e) =>
                      changeRole.mutate(
                        { userId: m.user_id, role: e.target.value as "editor" | "viewer" },
                        { onError: (err) => toast.error(err.message) },
                      )
                    }
                  >
                    <option value="editor">Editor</option>
                    <option value="viewer">Viewer</option>
                  </select>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={`Remove ${m.name}`}
                    disabled={remove.isPending}
                    onClick={() => {
                      if (!window.confirm(`Remove ${m.name} from this chat?`)) return;
                      remove.mutate(m.user_id, { onError: (err) => toast.error(err.message) });
                    }}
                  >
                    <X />
                  </Button>
                </>
              )}
            </li>
          );
        })}
      </ul>
      {!owner && (
        <Button
          variant="ghost"
          size="sm"
          className="text-destructive"
          disabled={remove.isPending}
          onClick={() => {
            if (!window.confirm("Leave this chat? You'll need a new invite to come back.")) return;
            remove.mutate(meId, {
              onSuccess: () => {
                toast("You left the chat");
                router.replace("/");
              },
              onError: (err) => toast.error(err.message),
            });
          }}
        >
          <LogOut /> Leave chat
        </Button>
      )}
    </section>
  );
}

/** Stacked avatars of who's online, for the chat header. */
export function PresenceStack({ chatId, online }: { chatId: string; online: string[] }) {
  const members = useMembers(chatId);
  const here = (members.data ?? []).filter((m) => online.includes(m.user_id));
  if (here.length === 0) return null;
  const shown = here.slice(0, 4);
  return (
    <div className="flex items-center" aria-label={`${here.length} online`}>
      {shown.map((m) => (
        <span key={m.user_id} title={`${m.name} is online`} className="-ml-1.5 first:ml-0">
          <Avatar name={m.name} color={m.avatar_color} size="sm" className="ring-2 ring-background" />
        </span>
      ))}
      {here.length > shown.length && (
        <span className="-ml-1.5 grid size-6 place-items-center rounded-full bg-muted text-[10px] ring-2 ring-background">
          +{here.length - shown.length}
        </span>
      )}
    </div>
  );
}
