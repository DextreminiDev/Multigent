"use client";

import { MessagesSquare, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useCreateChat } from "@/lib/api/hooks";

export default function Home() {
  const create = useCreateChat();
  const router = useRouter();
  return (
    <div className="grid h-full place-items-center p-6">
      <div className="max-w-md space-y-4 text-center">
        <MessagesSquare className="mx-auto size-10 text-primary" aria-hidden />
        <h1 className="text-2xl font-semibold tracking-tight">One model, many people</h1>
        <p className="text-muted-foreground">
          Start a chat, plug in your API key (a free Gemini key works), and invite people. Everyone
          talks to the same model in the same live conversation, on your key.
        </p>
        <Button
          size="lg"
          disabled={create.isPending}
          onClick={() =>
            create.mutate(undefined, {
              onSuccess: (c) => router.push(`/c/${c.id}`),
              onError: (e) => toast.error(e.message),
            })
          }
        >
          <Plus /> New chat
        </Button>
      </div>
    </div>
  );
}
