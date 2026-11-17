"use client";

import { Users } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";
import { Avatar } from "@/components/avatar";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { useAcceptInvite, useInvitePreview, useMe } from "@/lib/api/hooks";

const DEAD: Record<string, string> = {
  expired: "This invite link has expired.",
  used_up: "This invite link has already been used up.",
  revoked: "This invite link was revoked by the chat owner.",
};

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="grid min-h-dvh place-items-center px-4 py-10">
      <div className="w-full max-w-sm space-y-6">
        <Logo className="text-lg" />
        {children}
      </div>
    </main>
  );
}

export function acceptErrorMessage(e: unknown): string {
  if (!(e instanceof ApiError)) return "Something went wrong.";
  if (e.code === "chat_full") return e.message;
  if (e.code.startsWith("invite_")) return e.message;
  if (e.code === "rate_limited") return e.message;
  return e.message;
}

/**
 * /invite/{token}: public preview -> (sign in, then come back here) -> join -> chat.
 * The token never leaves this page except to the API (header-less path param).
 */
export function InviteLanding({ token }: { token: string }) {
  const router = useRouter();
  const preview = useInvitePreview(token);
  const me = useMe();
  const accept = useAcceptInvite();
  const autoTried = useRef(false);

  const join = () =>
    accept.mutate(token, {
      onSuccess: (r) => router.replace(`/c/${r.chat_id}`),
    });

  // Returning from sign-in with ?join=1 joins automatically (one click total).
  useEffect(() => {
    const wantsJoin = new URLSearchParams(window.location.search).get("join") === "1";
    if (wantsJoin && me.data && preview.data?.state === "active" && !autoTried.current) {
      autoTried.current = true;
      join();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me.data, preview.data?.state]);

  if (preview.isPending || me.isPending) {
    return (
      <Shell>
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-24 w-full" />
      </Shell>
    );
  }

  if (preview.isError) {
    const notFound = preview.error instanceof ApiError && preview.error.status === 404;
    return (
      <Shell>
        <h1 className="text-xl font-semibold">
          {notFound ? "Invite not found" : "Couldn't open this invite"}
        </h1>
        <p className="text-sm text-muted-foreground">
          {notFound
            ? "Check that you copied the whole link, or ask the chat owner for a new one."
            : preview.error.message}
        </p>
      </Shell>
    );
  }

  const p = preview.data;
  const dead = p.state !== "active";
  const next = `/invite/${token}?join=1`;

  return (
    <Shell>
      <div className="space-y-1">
        <p className="text-sm text-muted-foreground">You&apos;ve been invited to</p>
        <h1 className="text-2xl font-semibold tracking-tight">{p.chat_title}</h1>
      </div>
      <div className="space-y-3 rounded-xl border bg-card p-4 text-sm">
        <div className="flex items-center gap-3">
          <Avatar name={p.owner_name} color={p.owner_color} />
          <div>
            <div className="font-medium">{p.owner_name}</div>
            <div className="text-xs text-muted-foreground">Owner · pays for the AI</div>
          </div>
        </div>
        <div className="flex items-center gap-2 text-muted-foreground">
          <Users className="size-4" aria-hidden />
          {p.member_count} {p.member_count === 1 ? "member" : "members"} · you&apos;ll join as{" "}
          <span className="font-medium capitalize text-foreground">{p.role}</span>
        </div>
        <p className="text-xs text-muted-foreground">
          {p.role === "editor"
            ? "Editors chat with the AI together with everyone else, live."
            : "Viewers watch the conversation live but can't send messages."}
        </p>
      </div>

      {dead ? (
        <p role="alert" className="text-sm text-destructive">
          {DEAD[p.state]} Ask {p.owner_name} for a new link.
        </p>
      ) : me.data ? (
        <div className="space-y-2">
          <Button size="lg" className="w-full" disabled={accept.isPending} onClick={join}>
            {accept.isPending ? "Joining…" : `Join as ${me.data.user.name}`}
          </Button>
          {accept.isError && (
            <p role="alert" className="text-sm text-destructive">
              {acceptErrorMessage(accept.error)}
            </p>
          )}
        </div>
      ) : (
        <Button
          size="lg"
          className="w-full"
          onClick={() => router.push(`/login?next=${encodeURIComponent(next)}`)}
        >
          Sign in to join
        </Button>
      )}
    </Shell>
  );
}
