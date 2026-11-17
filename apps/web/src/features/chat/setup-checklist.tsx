"use client";

import { Check, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Chat } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import { KeySetup, ModelPicker } from "./key-setup";

export type SetupStep = { id: string; label: string; done: boolean; disabled?: boolean };

export function setupSteps(chat: Chat): SetupStep[] {
  const hasKey = !!chat.key;
  return [
    { id: "key", label: "Add your API key", done: hasKey },
    { id: "verified", label: "Key verified", done: chat.key?.status === "ok" },
    { id: "model", label: "Pick a model", done: !!chat.model },
    { id: "invite", label: "Invite people", done: chat.member_count > 1 },
  ];
}

/** Ready to chat once there's a usable key and a model; inviting is optional. */
export function isChatReady(chat: Chat): boolean {
  return !!chat.key && chat.key.status !== "invalid" && !!chat.model;
}

export function SetupChecklist({ chat, onInvite }: { chat: Chat; onInvite?: () => void }) {
  const steps = setupSteps(chat);
  const needKey = !chat.key || chat.key.status === "invalid";
  return (
    <section
      aria-labelledby="setup-title"
      className="mx-auto w-full max-w-xl space-y-6 rounded-xl border bg-card p-5 sm:p-6"
    >
      <div className="space-y-1">
        <h2 id="setup-title" className="text-lg font-semibold tracking-tight">
          Set up this chat
        </h2>
        <p className="text-sm text-muted-foreground">
          Your key powers the model for everyone you invite. They never see it.
        </p>
      </div>
      <ol className="flex flex-wrap gap-x-5 gap-y-2">
        {steps.map((s, i) => (
          <li
            key={s.id}
            className={cn(
              "flex items-center gap-2 whitespace-nowrap text-xs",
              s.done ? "text-foreground" : "text-muted-foreground",
              s.disabled && "opacity-60",
            )}
          >
            <span
              className={cn(
                "grid size-5 shrink-0 place-items-center rounded-full border text-[10px]",
                s.done && "border-primary bg-primary text-primary-foreground",
              )}
            >
              {s.done ? <Check className="size-3" /> : i + 1}
            </span>
            {s.label}
            <span className="sr-only">{s.done ? "(done)" : "(to do)"}</span>
          </li>
        ))}
      </ol>
      {needKey ? (
        <KeySetup chat={chat} />
      ) : (
        <div className="space-y-4">
          <KeySetup chat={chat} />
          <div className="space-y-1.5">
            <h3 className="text-sm font-medium">Model</h3>
            <ModelPicker chat={chat} />
          </div>
        </div>
      )}
      {onInvite && (
        <div className="flex items-center justify-between gap-3 border-t pt-4">
          <p className="text-xs text-muted-foreground">
            Invite people any time; they can chat as soon as the key and model are set.
          </p>
          <Button type="button" variant="outline" size="sm" onClick={onInvite}>
            <UserPlus /> Invite
          </Button>
        </div>
      )}
    </section>
  );
}
