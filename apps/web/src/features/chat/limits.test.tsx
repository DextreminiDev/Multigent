import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "@/lib/api/client";
import type { UsageView } from "@/lib/api/hooks";
import { applyEvent, initialState, type LiveState, type ServerEvent } from "@/lib/ws/reducer";
import { compactNumber, turnStatusText, UsageBreakdown } from "./limits";
import { MessageList } from "./message-list";

vi.mock("@/lib/api/hooks", () => ({
  useSetMemberCap: () => ({ mutate: vi.fn(), isPending: false }),
}));

const msg = (id: string, seq: number): ChatMessage => ({
  id, seq, anchor_seq: seq, kind: "human", author_id: "u1", author_name: "Alice",
  author_color: "#000", content: "hi", status: "pending", turn_id: null,
  created_at: "2026-11-22T10:00:00Z", client_msg_id: null,
});

function running(): LiveState {
  return [
    { type: "message.created", seq: 2, payload: { message: msg("a", 2) } },
    { type: "turn.started", seq: 3, payload: { turn_id: "t1", message_ids: ["a"], author_ids: ["u1"] } },
  ].reduce((s: LiveState, e: ServerEvent) => applyEvent(s, e), initialState(1, []));
}

describe("turn.status in the reducer", () => {
  it("tracks backoff with an absolute deadline, cleared once text flows", () => {
    let s = applyEvent(running(), {
      type: "turn.status",
      payload: { turn_id: "t1", status: "rate_limited", retry_in: 12, attempt: 1, max_attempts: 3 },
    }, 1_000);
    expect(s.turnStatus).toMatchObject({ status: "rate_limited", until: 13_000, attempt: 1 });
    expect(turnStatusText(s.turnStatus!, 4_000)).toBe(
      "The provider's rate limit was hit. Retrying in 9s (attempt 1 of 3)…",
    );
    s = applyEvent(s, { type: "turn.delta", payload: { turn_id: "t1", idx: 0, text: "Hi" } });
    expect(s.turnStatus).toBeNull();
  });

  it("is cleared when the turn ends, and ignores other turns", () => {
    let s = applyEvent(running(), { type: "turn.status", payload: { turn_id: "t1", status: "compacting" } });
    expect(applyEvent(s, { type: "turn.status", payload: { turn_id: "other", status: "retrying" } }).turnStatus?.status).toBe("compacting");
    s = applyEvent(s, {
      type: "turn.failed",
      seq: 4,
      payload: { turn_id: "t1", error_code: "rate_limit", error_message: "x", message_ids: ["a"], requeued: false },
    });
    expect(s.turnStatus).toBeNull();
  });

  it("stores the context snapshot", () => {
    const s = applyEvent(initialState(0, []), {
      type: "context.update",
      payload: { window: 1000, budget: 700, used: 350, percent: 50, source: "provider", summary_through: 12 },
    });
    expect(s.context).toEqual({ window: 1000, budget: 700, used: 350, percent: 50, source: "provider", summaryThrough: 12 });
  });

  it("the streaming bubble explains the wait instead of 'Thinking…'", () => {
    const s = applyEvent(running(), {
      type: "turn.status",
      payload: { turn_id: "t1", status: "unavailable", retry_in: 5, attempt: 2, max_attempts: 3, detail: "high demand" },
    });
    render(<MessageList state={s} meId="u1" canSend paused={false} onRetrySend={vi.fn()} onDiscardSend={vi.fn()} empty={null} />);
    expect(screen.getByRole("status")).toHaveTextContent(/provider is busy right now\. Retrying in [45]s \(attempt 2 of 3\)/);
    expect(screen.getByText("Provider said: high demand")).toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).toBeNull();
  });
});

describe("usage panel", () => {
  const u: UsageView = {
    day: "2026-11-22",
    daily_token_cap: 10_000,
    used_today: 8_500,
    members: [
      { user_id: "o", name: "Alice", avatar_color: "#e5484d", role: "owner", tokens_today: 6000, tokens_week: 9000, member_token_cap: null },
      { user_id: "b", name: "Bob", avatar_color: "#0090ff", role: "editor", tokens_today: 2000, tokens_week: 2000, member_token_cap: 5000 },
      { user_id: null, name: "Summaries & unattributed", avatar_color: null, role: null, tokens_today: 500, tokens_week: 500, member_token_cap: null },
    ],
    days: Array.from({ length: 7 }, (_, i) => ({ day: `2026-11-${16 + i}`, tokens_in: i * 100, tokens_out: i * 50, est_cost_usd: null })),
    est_cost_week_usd: null,
    pricing_known: false,
  };

  it("shows today's cap, per-member usage and caps", () => {
    render(<UsageBreakdown u={u} chatId="c1" />);
    expect(screen.getByText("8,500 / 10,000 tokens")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Daily cap used" })).toHaveAttribute("aria-valuenow", "85");
    expect(screen.getAllByLabelText("Daily cap for this member")).toHaveLength(1); // only Bob (not owner/summaries)
    expect(screen.getByLabelText("Daily cap for this member")).toHaveValue("5000");
    expect(screen.getByText(/No price data/)).toBeInTheDocument();
  });

  it("formats big numbers compactly", () => {
    expect(compactNumber(1_048_576)).toBe("1.0M");
    expect(compactNumber(200_000)).toBe("200k");
    expect(compactNumber(950)).toBe("950");
  });
});
