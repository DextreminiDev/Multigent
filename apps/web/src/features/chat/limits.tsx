"use client";

import { Gauge, Hourglass, Layers, Loader2, RotateCw } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { toast } from "sonner";
import { Avatar } from "@/components/avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Chat } from "@/lib/api/client";
import {
  type UsageView,
  useClearSummary,
  useCompactNow,
  useContextInfo,
  useSetMemberCap,
  useUsage,
} from "@/lib/api/hooks";
import type { ContextInfo, TurnStatus } from "@/lib/ws/reducer";
import { cn } from "@/lib/utils";

const fmt = (n: number) => n.toLocaleString();

export function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n % 1_000 ? 1 : 0)}k`;
  return String(n);
}

/** Re-render every second while there's a countdown to show. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

export function turnStatusText(st: TurnStatus, now: number): string {
  const secs = st.until != null ? Math.max(0, Math.ceil((st.until - now) / 1000)) : null;
  const inSecs = secs != null ? ` in ${secs}s` : "";
  const attempt = st.attempt && st.maxAttempts ? ` (attempt ${st.attempt} of ${st.maxAttempts})` : "";
  switch (st.status) {
    case "rate_limited":
      return `The provider's rate limit was hit. Retrying${inSecs}${attempt}…`;
    case "unavailable":
      return `The provider is busy right now. Retrying${inSecs}${attempt}…`;
    case "waiting_rate":
      return `Pacing requests to stay within the key's per-minute limit. Starting${inSecs}…`;
    case "compacting":
      return "Summarizing older messages so the conversation fits the model…";
    case "retrying":
      return "Retrying…";
  }
}

/** Shown inside the streaming bubble while the turn waits or compacts. */
export function TurnStatusLine({ status }: { status: TurnStatus }) {
  const now = useNow(status.until != null);
  const Icon = status.status === "compacting" ? Layers : status.status === "retrying" ? RotateCw : Hourglass;
  return (
    <p className="flex items-start gap-2 text-sm text-muted-foreground" role="status">
      <Icon className={cn("mt-0.5 size-4 shrink-0", status.status === "retrying" && "animate-spin")} />
      <span>
        {turnStatusText(status, now)}
        {status.detail && status.status !== "compacting" && (
          <span className="block text-xs opacity-80">Provider said: {status.detail}</span>
        )}
      </span>
    </p>
  );
}

// ------------------------------------------------------------------ context meter

const SOURCE: Record<string, string> = {
  provider: "from the provider",
  litellm: "from LiteLLM's model data",
  default: "unknown, assuming 32k",
};

export function ContextMeter({ chat, live }: { chat: Chat; live: ContextInfo | null }) {
  const info = useContextInfo(chat.id);
  const compact = useCompactNow(chat.id);
  const clear = useClearSummary(chat.id);
  const owner = chat.role === "owner";
  const d = info.data;
  const percent = live?.percent ?? d?.percent ?? null;
  const window = live?.window ?? d?.window ?? chat.context_window ?? null;
  const source = live?.source ?? d?.source ?? chat.limits_source ?? null;
  const pct = percent == null ? null : Math.min(100, Math.round(percent));

  return (
    <section className="space-y-2.5" aria-labelledby="ctx-title">
      <h3 id="ctx-title" className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
        <Gauge className="size-3.5" aria-hidden /> Context
      </h3>
      {pct == null ? (
        <p className="text-sm text-muted-foreground">Measured after the first reply.</p>
      ) : (
        <div className="space-y-1">
          <div
            className="h-2 overflow-hidden rounded-full bg-muted"
            role="meter"
            aria-label="Context used"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
          >
            <div
              className={cn(
                "h-full rounded-full transition-[width]",
                pct >= 90 ? "bg-destructive" : pct >= 70 ? "bg-warn" : "bg-primary",
              )}
              style={{ width: `${pct}%` }}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            {pct}% of the working budget used
            {window ? ` · window ${compactNumber(window)} tokens` : ""}
            {source ? ` (${SOURCE[source] ?? source})` : ""}
          </p>
        </div>
      )}
      {d?.summary ? (
        <p className="text-xs text-muted-foreground">
          Older history is summarized: the summary covers the first {fmt(d.summary.covered_messages)}{" "}
          messages.
        </p>
      ) : null}
      {owner && d?.summary?.content && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            Show summary
          </summary>
          <p className="mt-1 whitespace-pre-wrap rounded-md bg-muted p-2">{d.summary.content}</p>
        </details>
      )}
      {owner && (
        <div className="flex flex-wrap gap-2">
          <Button
            size="xs"
            variant="outline"
            disabled={compact.isPending || d?.compact_requested}
            onClick={() =>
              compact.mutate(undefined, {
                onSuccess: () => toast("Older messages will be summarized before the next reply"),
                onError: (e) => toast.error(e.message),
              })
            }
          >
            {d?.compact_requested ? "Compaction queued" : "Compact now"}
          </Button>
          {d?.summary && (
            <Button
              size="xs"
              variant="ghost"
              disabled={clear.isPending}
              onClick={() =>
                clear.mutate(undefined, {
                  onSuccess: () => toast("Summary cleared"),
                  onError: (e) => toast.error(e.message),
                })
              }
            >
              Clear summary
            </Button>
          )}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ usage panel

function MemberCapInput({ chatId, userId, cap }: { chatId: string; userId: string; cap: number | null }) {
  const set = useSetMemberCap(chatId);
  const [value, setValue] = useState(cap ? String(cap) : "");
  useEffect(() => setValue(cap ? String(cap) : ""), [cap]);
  const save = () => {
    const n = value.trim() === "" ? null : Number(value);
    if (n !== null && (!Number.isInteger(n) || n < 1)) return;
    if (n === cap) return;
    set.mutate({ userId, cap: n }, { onError: (e) => toast.error(e.message) });
  };
  return (
    <Input
      aria-label="Daily cap for this member"
      className="h-7 w-24 text-xs"
      inputMode="numeric"
      placeholder="No cap"
      value={value}
      onChange={(e) => setValue(e.target.value.replace(/[^\d]/g, ""))}
      onBlur={save}
      onKeyDown={(e) => e.key === "Enter" && save()}
    />
  );
}

export function UsageBreakdown({ u, chatId }: { u: UsageView; chatId: string }) {
  const max = Math.max(1, ...u.days.map((d) => d.tokens_in + d.tokens_out));
  const capPct = u.daily_token_cap ? Math.min(100, Math.round((100 * u.used_today) / u.daily_token_cap)) : null;
  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <div className="flex items-baseline justify-between text-sm">
          <span className="font-medium">Today (UTC)</span>
          <span className="tabular-nums text-muted-foreground">
            {fmt(u.used_today)}
            {u.daily_token_cap ? ` / ${fmt(u.daily_token_cap)}` : ""} tokens
          </span>
        </div>
        {capPct != null && (
          <div className="h-2 overflow-hidden rounded-full bg-muted" role="meter" aria-label="Daily cap used" aria-valuenow={capPct} aria-valuemin={0} aria-valuemax={100}>
            <div className={cn("h-full rounded-full", capPct >= 100 ? "bg-destructive" : capPct >= 80 ? "bg-warn" : "bg-primary")} style={{ width: `${capPct}%` }} />
          </div>
        )}
      </div>

      <div>
        <div className="mb-1 text-xs text-muted-foreground">Last 7 days</div>
        <div className="flex h-16 items-end gap-1" aria-label="Tokens per day, last 7 days">
          {u.days.map((d) => {
            const total = d.tokens_in + d.tokens_out;
            return (
              <div key={d.day} className="flex flex-1 flex-col items-center gap-1" title={`${d.day}: ${fmt(total)} tokens`}>
                <div className="w-full rounded-sm bg-primary/70" style={{ height: `${Math.max(2, (56 * total) / max)}px` }} />
                <span className="text-[10px] text-muted-foreground">{new Date(`${d.day}T00:00:00Z`).toLocaleDateString([], { weekday: "narrow", timeZone: "UTC" })}</span>
              </div>
            );
          })}
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {u.pricing_known && u.est_cost_week_usd != null
            ? `Estimated cost this week: $${u.est_cost_week_usd.toFixed(4)}`
            : "No price data for this model (free tier or unknown), showing tokens only."}
        </p>
      </div>

      <div>
        <div className="mb-1 flex justify-between text-xs text-muted-foreground">
          <span>Who used what (today · 7 days)</span>
          <span>Daily cap</span>
        </div>
        <ul className="space-y-1.5">
          {u.members.map((m) => (
            <li key={m.user_id ?? "summaries"} className="flex items-center gap-2 text-sm">
              {m.avatar_color ? (
                <Avatar name={m.name} color={m.avatar_color} size="sm" />
              ) : (
                <span className="grid size-6 place-items-center rounded-full bg-muted"><Layers className="size-3" /></span>
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate">{m.name}{m.role === null && m.user_id ? " (former)" : ""}</span>
                <span className="block text-xs tabular-nums text-muted-foreground">
                  {fmt(m.tokens_today)} · {fmt(m.tokens_week)}
                </span>
              </span>
              {m.user_id && m.role && m.role !== "owner" && (
                <MemberCapInput chatId={chatId} userId={m.user_id} cap={m.member_token_cap ?? null} />
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function UsagePanel({ chat }: { chat: Chat }) {
  const usage = useUsage(chat.id, chat.role === "owner");
  if (usage.isPending) return <Loader2 className="size-4 animate-spin text-muted-foreground" aria-label="Loading usage" />;
  if (usage.isError) return <p className="text-sm text-destructive">Couldn&apos;t load usage.</p>;
  return <UsageBreakdown u={usage.data} chatId={chat.id} />;
}

export function RpmField({ chat, onSave }: { chat: Chat; onSave: (v: number | null) => void }) {
  const uid = useId();
  const [v, setV] = useState(chat.rpm_limit ? String(chat.rpm_limit) : "");
  const providerDefault = chat.provider === "gemini" ? "10 (Gemini free-tier default)" : "no limit";
  return (
    <form
      className="space-y-1.5"
      onSubmit={(e) => {
        e.preventDefault();
        const n = v.trim() === "" ? null : Number(v);
        if (n === null || (Number.isInteger(n) && n >= 1 && n <= 600)) onSave(n);
      }}
    >
      <label htmlFor={uid} className="text-sm font-medium">Requests per minute to the provider</label>
      <div className="flex gap-2">
        <Input id={uid} inputMode="numeric" placeholder={`Default: ${providerDefault}`} value={v} onChange={(e) => setV(e.target.value.replace(/[^\d]/g, ""))} />
        <Button type="submit" variant="outline">Save</Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Replies wait their turn instead of hitting the key&apos;s rate limit. Match your plan&apos;s RPM.
      </p>
    </form>
  );
}
