"use client";

import { useParams } from "next/navigation";
import { InviteLanding } from "@/features/invite/invite-landing";

export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  return <InviteLanding token={token} />;
}
