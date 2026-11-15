"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Logo } from "@/components/logo";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api/client";
import { useDevLogin, useMe } from "@/lib/api/hooks";
import { safeNext } from "@/lib/safe-next";

function LoginForm() {
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const me = useMe();
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

  return (
    <form
      className="w-full max-w-sm space-y-5"
      onSubmit={(e) => {
        e.preventDefault();
        login.mutate({ name, email }, { onSuccess: () => router.replace(next) });
      }}
    >
      <div className="space-y-1.5">
        <Logo className="text-lg" />
        <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
        <p className="text-sm text-muted-foreground">
          Development sign-in. Google and GitHub sign-in arrive later.
        </p>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="name">Display name</Label>
        <Input
          id="name"
          autoComplete="name"
          required
          maxLength={40}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Alice"
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="alice@example.com"
        />
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <Button type="submit" size="lg" className="w-full" disabled={login.isPending}>
        {login.isPending ? "Signing in…" : "Continue"}
      </Button>
      <p className="text-center text-xs">
        <StatusBadge />
      </p>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main className="grid min-h-dvh place-items-center px-4">
      <Suspense>
        <LoginForm />
      </Suspense>
    </main>
  );
}
