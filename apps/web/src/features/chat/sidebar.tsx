"use client";

import { LogOut, Moon, Plus, Sun } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { toast } from "sonner";
import { Avatar } from "@/components/avatar";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import type { Chat, Me } from "@/lib/api/client";
import { useChats, useCreateChat, useLogout } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";

function ChatLink({
  chat,
  active,
  onNavigate,
}: {
  chat: Chat;
  active: boolean;
  onNavigate?: () => void;
}) {
  const needsKey = chat.role === "owner" && (!chat.key || chat.key.status === "invalid");
  return (
    <Link
      href={`/c/${chat.id}`}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group flex items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
        active
          ? "bg-sidebar-accent text-sidebar-accent-foreground"
          : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60",
      )}
    >
      {chat.role !== "owner" && (
        <Avatar
          name={chat.owner.name}
          color={chat.owner.avatar_color}
          size="sm"
          className="size-5 text-[9px]"
        />
      )}
      <span className="min-w-0 flex-1 truncate">{chat.title}</span>
      {needsKey && (
        <span
          className="size-1.5 rounded-full bg-warn"
          title="Needs an API key"
          aria-label="Needs an API key"
        />
      )}
    </Link>
  );
}

function Section({
  title,
  chats,
  activeId,
  onNavigate,
}: {
  title: string;
  chats: Chat[];
  activeId?: string;
  onNavigate?: () => void;
}) {
  if (chats.length === 0) return null;
  return (
    <div className="space-y-0.5">
      <h2 className="px-2 pb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
        {title}
      </h2>
      {chats.map((c) => (
        <ChatLink key={c.id} chat={c} active={c.id === activeId} onNavigate={onNavigate} />
      ))}
    </div>
  );
}

export function Sidebar({ me, onNavigate }: { me: Me; onNavigate?: () => void }) {
  const chats = useChats();
  const create = useCreateChat();
  const logout = useLogout();
  const router = useRouter();
  const { chatId } = useParams<{ chatId?: string }>();
  const { resolvedTheme, setTheme } = useTheme();

  const owned = chats.data?.filter((c) => c.role === "owner") ?? [];
  const shared = chats.data?.filter((c) => c.role !== "owner") ?? [];

  return (
    <nav aria-label="Chats" className="flex h-full flex-col gap-3 bg-sidebar p-3">
      <div className="flex items-center justify-between px-1 pt-1">
        <Link href="/" onClick={onNavigate}>
          <Logo />
        </Link>
      </div>
      <Button
        variant="outline"
        className="justify-start"
        disabled={create.isPending}
        onClick={() =>
          create.mutate(undefined, {
            onSuccess: (c) => {
              onNavigate?.();
              router.push(`/c/${c.id}`);
            },
            onError: (e) => toast.error(e.message),
          })
        }
      >
        <Plus /> New chat
      </Button>

      <div className="-mx-1 flex-1 space-y-5 overflow-y-auto px-1">
        {chats.isPending && (
          <div className="space-y-2 px-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-6 w-full" />
            ))}
          </div>
        )}
        {chats.isError && (
          <p className="px-2 text-sm text-destructive">Couldn&apos;t load chats.</p>
        )}
        {chats.data?.length === 0 && (
          <p className="px-2 text-sm text-muted-foreground">
            No chats yet. Start one, or open an invite link.
          </p>
        )}
        <Section title="Owned by me" chats={owned} activeId={chatId} onNavigate={onNavigate} />
        <Section title="Shared with me" chats={shared} activeId={chatId} onNavigate={onNavigate} />
      </div>

      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <button className="flex w-full items-center gap-2 rounded-md p-1.5 text-left text-sm hover:bg-sidebar-accent/60" />
          }
        >
          <Avatar name={me.user.name} color={me.user.avatar_color} />
          <span className="min-w-0 flex-1">
            <span className="block truncate font-medium">{me.user.name}</span>
            <span className="block truncate text-xs text-muted-foreground">{me.user.email}</span>
          </span>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="w-56">
          <DropdownMenuItem onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}>
            {resolvedTheme === "dark" ? <Sun /> : <Moon />}
            {resolvedTheme === "dark" ? "Light mode" : "Dark mode"}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={() => logout.mutate(undefined, { onSettled: () => router.replace("/login") })}
          >
            <LogOut /> Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </nav>
  );
}
