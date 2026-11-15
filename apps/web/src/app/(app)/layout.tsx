"use client";

import { AuthGate } from "@/features/auth/auth-gate";
import { AppShell } from "@/features/chat/app-shell";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AuthGate>{(me) => <AppShell me={me}>{children}</AppShell>}</AuthGate>;
}
