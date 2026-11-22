"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Logo } from "@/components/logo";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import { useAuthProviders, useDevLogin, useMe } from "@/lib/api/hooks";
import { oauthUrl } from "@/lib/oauth";
import { safeNext } from "@/lib/safe-next";

function GoogleIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-4" aria-hidden>
      <path fill="#EA4335" d="M12 10.2v3.9h5.5c-.2 1.3-1.6 3.9-5.5 3.9-3.3 0-6-2.7-6-6.1s2.7-6.1 6-6.1c1.9 0 3.1.8 3.8 1.5l2.6-2.5C16.8 3.3 14.6 2.4 12 2.4 6.7 2.4 2.4 6.7 2.4 12s4.3 9.6 9.6 9.6c5.5 0 9.2-3.9 9.2-9.4 0-.6-.1-1.1-.2-1.6H12z" />
    </svg>
  );
}

function GitHubIcon() {
  return (
    <svg viewBox="0 0 24 24" className="size-4" aria-hidden fill="currentColor">
      <path d="M12 .5a11.5 11.5 0 0 0-3.6 22.4c.6.1.8-.3.8-.6v-2c-3.2.7-3.9-1.5-3.9-1.5-.5-1.3-1.3-1.7-1.3-1.7-1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.7-1.6-2.6-.3-5.3-1.3-5.3-5.7 0-1.3.5-2.3 1.2-3.1-.1-.3-.5-1.5.1-3.1 0 0 1-.3 3.2 1.2a11 11 0 0 1 5.8 0C17.3 4.6 18.3 5 18.3 5c.6 1.6.2 2.8.1 3.1.7.8 1.2 1.8 1.2 3.1 0 4.4-2.7 5.4-5.3 5.7.4.4.8 1.1.8 2.2v3.2c0 .3.2.7.8.6A11.5 11.5 0 0 0 12 .5z" />
    </svg>
  );
}

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const oauthFailed = params.get("error") === "oauth_failed";
  const me = useMe();
  const providers = useAuthProviders();
  const login = useDevLogin();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");

  useEffect(() => {
    if (me.data) router.replace(next);
  }, [me.data, next, router]);

  const error =
    login.error instanceof ApiError
      ? login.error.code === "not_found"
        ? "Dev login is disabled on this server."
        : login.error.code === "validation_error"
          ? "Check your name and email."
          : login.error.message
      : null;

  const p = providers.data;
  const anyOAuth = Boolean(p?.google || p?.github);

  return (
    <div className="w-full max-w-sm space-y-6">
      <div className="space-y-1.5">
        <Logo className="text-lg" />
        <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          Chat with an AI together with other people, live.
        </p>
      </div>

      {oauthFailed && (
        <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
          Sign-in didn&apos;t complete. Try again, and make sure your account has a verified email.
        </p>
      )}

      {providers.isPending ? (
        <Skeleton className="h-10 w-full" />
      ) : (
        anyOAuth && (
          <div className="space-y-2">
            {p?.google && (
              <Button variant="outline" size="lg" className="w-full" render={<a href={oauthUrl("google", next)} />}>
                <GoogleIcon /> Continue with Google
              </Button>
            )}
            {p?.github && (
              <Button variant="outline" size="lg" className="w-full" render={<a href={oauthUrl("github", next)} />}>
                <GitHubIcon /> Continue with GitHub
              </Button>
            )}
          </div>
        )
      )}

      {p?.dev && (
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            login.mutate({ name, email }, { onSuccess: () => router.replace(next) });
          }}
        >
          {anyOAuth && (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span className="h-px flex-1 bg-border" /> or development sign-in{" "}
              <span className="h-px flex-1 bg-border" />
            </div>
          )}
          {!anyOAuth && (
            <p className="text-sm text-muted-foreground">
              Development sign-in (no password). Google and GitHub appear here once configured.
            </p>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="name">Display name</Label>
            <Input id="name" autoComplete="name" required maxLength={40} value={name}
              onChange={(e) => setName(e.target.value)} placeholder="Alice" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" autoComplete="email" required value={email}
              onChange={(e) => setEmail(e.target.value)} placeholder="alice@example.com" />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <Button type="submit" size="lg" className="w-full" disabled={login.isPending}>
            {login.isPending ? "Signing in…" : "Continue"}
          </Button>
        </form>
      )}

      {p && !anyOAuth && !p.dev && (
        <p role="alert" className="text-sm text-destructive">
          No sign-in method is configured on this server.
        </p>
      )}

      <p className="text-center text-xs">
        <StatusBadge />
      </p>
    </div>
  );
}

export default function LoginPage() {
  return (
    <main id="main" className="grid min-h-dvh place-items-center px-4">
      <Suspense>
        <LoginForm />
      </Suspense>
    </main>
  );
}
