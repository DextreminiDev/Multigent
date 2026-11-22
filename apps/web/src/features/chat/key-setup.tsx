"use client";

import { CheckCircle2, ExternalLink, KeyRound, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, type Chat, type KeyView } from "@/lib/api/client";
import {
  useDeleteKey,
  useModels,
  usePatchChat,
  useProviders,
  usePutKey,
  useValidateKey,
  type KeyInput,
} from "@/lib/api/hooks";
import { cn } from "@/lib/utils";

const STATUS: Record<KeyView["status"], { label: string; className: string }> = {
  ok: { label: "Verified", className: "bg-ok/15 text-ok" },
  unvalidated: { label: "Not verified yet", className: "bg-warn/15 text-warn" },
  invalid: { label: "Rejected by provider", className: "bg-destructive/15 text-destructive" },
};

export function keyErrorMessage(e: unknown): string {
  if (!(e instanceof ApiError)) return "Something went wrong.";
  switch (e.code) {
    case "key_invalid":
      return "The provider rejected this key. Double-check it and try again.";
    case "validation_error":
      return "That doesn't look like an API key (check for spaces or a partial paste).";
    case "unsafe_base_url":
    case "base_url_required":
      return e.message;
    default:
      return e.message;
  }
}

export function KeySetup({ chat, compact = false }: { chat: Chat; compact?: boolean }) {
  const providers = useProviders();
  const put = usePutKey(chat.id);
  const del = useDeleteKey(chat.id);
  const validate = useValidateKey(chat.id);
  const [replacing, setReplacing] = useState(false);
  const editing = !chat.key || replacing;
  const setEditing = setReplacing;
  const uid = useId();
  const [provider, setProvider] = useState<KeyInput["provider"]>(
    (chat.provider as KeyInput["provider"] | null) ?? "gemini",
  );
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(chat.base_url ?? "");

  const meta = providers.data?.find((p) => p.id === provider);

  if (chat.key && !editing) {
    const s = STATUS[chat.key.status];
    const label = providers.data?.find((p) => p.id === chat.key?.provider)?.label;
    return (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3">
          <KeyRound className="size-4 text-muted-foreground" aria-hidden />
          <span className="text-sm font-medium">{label ?? chat.key.provider}</span>
          <span className="font-mono text-sm text-muted-foreground">
            <span className="sr-only">Key ending in </span>
            <span aria-hidden>•••• </span>
            {chat.key.last4}
          </span>
          <Badge className={cn("ml-auto", s.className)}>{s.label}</Badge>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
            Replace key
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={validate.isPending}
            onClick={() =>
              validate.mutate(undefined, {
                onError: (e) => toast.error(keyErrorMessage(e)),
                onSuccess: () => toast.success("Key re-checked"),
              })
            }
          >
            <RefreshCw className={cn(validate.isPending && "animate-spin")} /> Re-check
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-destructive"
            disabled={del.isPending}
            onClick={() => {
              if (!window.confirm("Remove this API key? The chat pauses until a new key is added."))
                return;
              del.mutate(undefined, { onSuccess: () => setEditing(false) });
            }}
          >
            <Trash2 /> Remove
          </Button>
        </div>
        {chat.key.status === "invalid" && (
          <p className="flex gap-2 text-sm text-destructive">
            <ShieldAlert className="mt-0.5 size-4 shrink-0" />
            The provider no longer accepts this key. Replace it to resume the chat.
          </p>
        )}
      </div>
    );
  }

  return (
    <form
      className="space-y-4"
      autoComplete="off"
      onSubmit={(e) => {
        e.preventDefault();
        put.mutate(
          {
            provider,
            api_key: apiKey.trim(),
            base_url: meta?.needs_base_url ? baseUrl.trim() : undefined,
          },
          {
            onSuccess: (k) => {
              setApiKey(""); // drop plaintext from memory as soon as it's stored
              setEditing(false);
              toast.success(k.status === "ok" ? "Key verified and saved" : "Key saved");
            },
          },
        );
      }}
    >
      <fieldset className="space-y-2">
        <legend className="mb-2 text-sm font-medium">Provider</legend>
        <div className={cn("grid gap-2", compact ? "grid-cols-2" : "grid-cols-2 sm:grid-cols-3")}>
          {providers.data?.map((p) => (
            <label
              key={p.id}
              className={cn(
                "cursor-pointer rounded-lg border p-2.5 text-sm transition-colors has-focus-visible:ring-3 has-focus-visible:ring-ring/50",
                provider === p.id ? "border-primary bg-accent text-accent-foreground" : "hover:bg-muted",
              )}
            >
              <input
                type="radio"
                name={`${uid}-provider`}
                value={p.id}
                className="sr-only"
                checked={provider === p.id}
                onChange={() => setProvider(p.id as KeyInput["provider"])}
              />
              {p.label}
              {p.id === "gemini" && (
                <span className="block text-xs text-muted-foreground">Free tier available</span>
              )}
            </label>
          ))}
        </div>
      </fieldset>

      {meta?.needs_base_url && (
        <div className="space-y-1.5">
          <Label htmlFor={`${uid}-base-url`}>Base URL</Label>
          <Input
            id={`${uid}-base-url`}
            type="url"
            required
            placeholder="https://api.example.com/v1"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </div>
      )}

      <div className="space-y-1.5">
        <Label htmlFor={`${uid}-api-key`}>API key</Label>
        <Input
          id={`${uid}-api-key`}
          type="password"
          required
          autoComplete="off"
          spellCheck={false}
          data-1p-ignore
          placeholder={provider === "fake" ? "any-text-works" : "Paste your key"}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <p className="text-xs text-muted-foreground">
          Encrypted at rest and never shown again, not even to you. Collaborators use it through the
          server and can&apos;t read it.
        </p>
        {meta?.key_help_url && (
          <a
            href={meta.key_help_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
          >
            {provider === "gemini" ? "Get a free Gemini key in Google AI Studio" : "Where do I get a key?"}
            <ExternalLink className="size-3" />
          </a>
        )}
      </div>

      {put.isError && (
        <p role="alert" className="text-sm text-destructive">
          {keyErrorMessage(put.error)}
        </p>
      )}

      <div className="flex gap-2">
        <Button type="submit" disabled={put.isPending || !apiKey.trim()}>
          {put.isPending ? "Checking key…" : "Save & verify"}
        </Button>
        {chat.key && (
          <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}

export function ModelPicker({ chat }: { chat: Chat }) {
  const enabled = !!chat.key && chat.key.status !== "invalid";
  const models = useModels(chat.id, enabled);
  const patch = usePatchChat(chat.id);
  const [manual, setManual] = useState(chat.model ?? "");

  if (!enabled) {
    return <p className="text-sm text-muted-foreground">Add a working key to choose a model.</p>;
  }
  const save = (model: string) =>
    model &&
    patch.mutate(
      { model },
      {
        onSuccess: () => toast.success("Model updated"),
        onError: (e) => toast.error(e.message),
      },
    );

  if (models.isError) {
    return (
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save(manual.trim());
        }}
      >
        <Input
          aria-label="Model id"
          placeholder="Model id, e.g. gemini-2.5-flash"
          value={manual}
          onChange={(e) => setManual(e.target.value)}
        />
        <Button type="submit" variant="outline" disabled={patch.isPending}>
          Save
        </Button>
      </form>
    );
  }

  return (
    <div className="space-y-1.5">
      <select
        aria-label="Model"
        className="h-8 w-full rounded-lg border border-input bg-transparent px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
        disabled={models.isPending || patch.isPending}
        value={chat.model ?? ""}
        onChange={(e) => save(e.target.value)}
      >
        <option value="" disabled>
          {models.isPending ? "Loading models…" : "Choose a model"}
        </option>
        {chat.model && !models.data?.models.some((m) => m.id === chat.model) && (
          <option value={chat.model}>{chat.model}</option>
        )}
        {models.data?.models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label ? `${m.label} (${m.id})` : m.id}
          </option>
        ))}
      </select>
      {chat.model && (
        <p className="flex items-center gap-1 text-xs text-muted-foreground">
          <CheckCircle2 className="size-3 text-ok" /> Everyone in this chat talks to {chat.model}
        </p>
      )}
      {chat.model && chat.context_window && (
        <p className="text-xs text-muted-foreground">
          Context window {chat.context_window.toLocaleString()} tokens
          {chat.max_output_limit ? ` · replies up to ${chat.max_output_limit.toLocaleString()}` : ""}
          {chat.limits_source === "default" ? " (unknown for this model, assumed)" : ""}
        </p>
      )}
    </div>
  );
}
