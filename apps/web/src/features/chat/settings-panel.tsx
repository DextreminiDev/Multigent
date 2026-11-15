"use client";

import { Info } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Avatar } from "@/components/avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import type { Chat } from "@/lib/api/client";
import { useDeleteChat, usePatchChat, type ChatPatch } from "@/lib/api/hooks";
import { KeySetup, ModelPicker } from "./key-setup";
import { isChatReady } from "./setup-checklist";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

function useSave(chat: Chat) {
  const patch = usePatchChat(chat.id);
  return {
    pending: patch.isPending,
    save: (body: ChatPatch, ok = "Saved") =>
      patch.mutate(body, {
        onSuccess: () => toast.success(ok),
        onError: (e) => toast.error(e.message),
      }),
  };
}

function OwnerSettings({ chat }: { chat: Chat }) {
  const { save, pending } = useSave(chat);
  const del = useDeleteChat();
  const router = useRouter();
  const [title, setTitle] = useState(chat.title);
  const [instructions, setInstructions] = useState(chat.custom_instructions ?? "");
  const [maxOut, setMaxOut] = useState(String(chat.max_output_tokens));
  const [cap, setCap] = useState(chat.daily_token_cap ? String(chat.daily_token_cap) : "");

  useEffect(() => setTitle(chat.title), [chat.title]);

  return (
    <div className="space-y-6">
      <Section title="General">
        <form
          className="space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (title.trim() && title !== chat.title) save({ title: title.trim() }, "Renamed");
          }}
        >
          <Label htmlFor="chat-title">Title</Label>
          <div className="flex gap-2">
            <Input
              id="chat-title"
              maxLength={120}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <Button type="submit" variant="outline" disabled={pending || title === chat.title}>
              Save
            </Button>
          </div>
        </form>
        <form
          className="space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            save({ custom_instructions: instructions }, "Instructions saved");
          }}
        >
          <Label htmlFor="instructions">Custom instructions</Label>
          <Textarea
            id="instructions"
            rows={5}
            maxLength={8000}
            placeholder="e.g. You are our trip-planning assistant. Keep answers short."
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              Only you can see these. {instructions.length}/8000
            </span>
            <Button
              type="submit"
              size="sm"
              variant="outline"
              disabled={pending || instructions === (chat.custom_instructions ?? "")}
            >
              Save
            </Button>
          </div>
        </form>
      </Section>

      <Separator />
      <Section title="Model & API key">
        {isChatReady(chat) ? (
          <>
            <KeySetup chat={chat} compact />
            <div className="space-y-1.5">
              <Label>Model</Label>
              <ModelPicker chat={chat} />
            </div>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            Add your key and pick a model using the setup steps in the chat.
          </p>
        )}
        <form
          className="space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            const n = Number(maxOut);
            if (Number.isInteger(n) && n >= 16) save({ max_output_tokens: n });
          }}
        >
          <Label htmlFor="max-out">Max reply length (tokens)</Label>
          <div className="flex gap-2">
            <Input
              id="max-out"
              type="number"
              inputMode="numeric"
              min={16}
              max={65536}
              value={maxOut}
              onChange={(e) => setMaxOut(e.target.value)}
            />
            <Button type="submit" variant="outline" disabled={pending}>
              Save
            </Button>
          </div>
        </form>
      </Section>

      <Separator />
      <Section title="Spending limits">
        <form
          className="space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            const n = cap.trim() === "" ? null : Number(cap);
            if (n === null || (Number.isInteger(n) && n > 0))
              save({ daily_token_cap: n }, n === null ? "Cap removed" : "Cap saved");
          }}
        >
          <Label htmlFor="cap">Daily token cap (whole chat)</Label>
          <div className="flex gap-2">
            <Input
              id="cap"
              type="number"
              inputMode="numeric"
              min={1}
              placeholder="No limit"
              value={cap}
              onChange={(e) => setCap(e.target.value)}
            />
            <Button type="submit" variant="outline" disabled={pending}>
              Save
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Collaborators spend your quota. When the cap is hit, the chat pauses until tomorrow
            (UTC).
          </p>
        </form>
      </Section>

      <Separator />
      <Section title="Danger zone">
        <Button
          variant="destructive"
          disabled={del.isPending}
          onClick={() => {
            if (
              !window.confirm(
                `Delete “${chat.title}” for everyone? Messages and the API key are erased permanently.`,
              )
            )
              return;
            del.mutate(chat.id, {
              onSuccess: () => {
                toast.success("Chat deleted");
                router.replace("/");
              },
              onError: (e) => toast.error(e.message),
            });
          }}
        >
          Delete chat
        </Button>
      </Section>
    </div>
  );
}

function CollaboratorInfo({ chat }: { chat: Chat }) {
  const keyOk = chat.key && chat.key.status !== "invalid";
  return (
    <div className="space-y-5 text-sm">
      <div className="flex items-center gap-3">
        <Avatar name={chat.owner.name} color={chat.owner.avatar_color} />
        <div>
          <div className="font-medium">{chat.owner.name}</div>
          <div className="text-xs text-muted-foreground">Owner</div>
        </div>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
        <dt className="text-muted-foreground">Your role</dt>
        <dd className="capitalize">{chat.role}</dd>
        <dt className="text-muted-foreground">Model</dt>
        <dd>{chat.model ?? "Not chosen yet"}</dd>
        <dt className="text-muted-foreground">Members</dt>
        <dd>{chat.member_count}</dd>
      </dl>
      <p className="flex gap-2 rounded-lg bg-muted p-3 text-muted-foreground">
        <Info className="mt-0.5 size-4 shrink-0" />
        {keyOk
          ? "This chat runs on the owner's API key, stored securely on the server. You can keep chatting even when the owner is offline."
          : "The owner's API key needs attention. Messages will wait until it's fixed."}
      </p>
    </div>
  );
}

export function SettingsPanel({ chat }: { chat: Chat }) {
  return chat.role === "owner" ? <OwnerSettings chat={chat} /> : <CollaboratorInfo chat={chat} />;
}
