import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Chat } from "@/lib/api/client";
import { applyEvent, initialState, typingLabel, typingNames } from "@/lib/ws/reducer";

// ------------------------------------------------------------------ reducer

describe("presence / typing / kicked", () => {
  it("tracks presence and typing with expiry; a sent message clears typing", () => {
    let s = initialState(1, []);
    s = applyEvent(s, { type: "presence.update", payload: { online: ["a", "b"] } });
    expect(s.online).toEqual(["a", "b"]);
    s = applyEvent(s, { type: "typing", payload: { user_id: "b", name: "Bob", ttl_s: 4 } }, 1000);
    s = applyEvent(s, { type: "typing", payload: { user_id: "me", name: "Me", ttl_s: 4 } }, 1000);
    expect(typingNames(s, "me", 2000)).toEqual(["Bob"]); // never show myself
    expect(typingNames(s, "me", 6000)).toEqual([]); // expired
    s = applyEvent(s, {
      type: "message.created",
      seq: 2,
      payload: {
        message: {
          id: "m", seq: 2, anchor_seq: 2, kind: "human", author_id: "b", author_name: "Bob",
          content: "hi", status: "pending", turn_id: null, created_at: "", client_msg_id: null,
        },
      },
    });
    expect(s.typing.b).toBeUndefined();
  });

  it("labels typing groups", () => {
    expect(typingLabel([])).toBeNull();
    expect(typingLabel(["Bob"])).toBe("Bob is typing…");
    expect(typingLabel(["Bob", "Cy"])).toBe("Bob and Cy are typing…");
    expect(typingLabel(["A", "B", "C"])).toBe("3 people are typing…");
  });

  it("kicked and member.updated", () => {
    let s = initialState(1, []);
    s = applyEvent(s, { type: "member.updated", seq: 2, payload: { member: {} } });
    expect(s.membersVersion).toBe(1);
    expect(applyEvent(s, { type: "kicked" }).kicked).toBe(true);
  });
});

// ------------------------------------------------------------------ components (hooks mocked)

const accept = vi.fn();
const push = vi.fn();
const replace = vi.fn();
const hookState: Record<string, unknown> = {};

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useParams: () => ({}),
}));

vi.mock("@/lib/api/hooks", () => ({
  useInvitePreview: () => hookState.preview,
  useMe: () => hookState.me,
  useAcceptInvite: () => ({ mutate: accept, isPending: false, isError: false }),
  useMembers: () => hookState.members,
  useChangeRole: () => ({ mutate: vi.fn(), isPending: false }),
  useRemoveMember: () => ({ mutate: vi.fn(), isPending: false }),
}));

const preview = (state = "active") => ({
  isPending: false,
  isError: false,
  data: {
    chat_title: "Trip planning",
    owner_name: "Alice",
    owner_color: "#e5484d",
    role: "editor",
    member_count: 2,
    state,
  },
});

beforeEach(() => {
  accept.mockReset();
  push.mockReset();
  replace.mockReset();
});

describe("InviteLanding", () => {
  it("sends signed-out visitors to login and back with join=1", async () => {
    const { InviteLanding } = await import("@/features/invite/invite-landing");
    Object.assign(hookState, { preview: preview(), me: { isPending: false, data: null } });
    render(<InviteLanding token="tok_ABCDEFGHIJKLMNOPQRSTUVWXYZ" />);
    expect(screen.getByText("Trip planning")).toBeInTheDocument();
    expect(screen.getByText(/you'll join as/)).toHaveTextContent("editor");
    fireEvent.click(screen.getByRole("button", { name: "Sign in to join" }));
    expect(push).toHaveBeenCalledWith(
      `/login?next=${encodeURIComponent("/invite/tok_ABCDEFGHIJKLMNOPQRSTUVWXYZ?join=1")}`,
    );
  });

  it("lets a signed-in user join", async () => {
    const { InviteLanding } = await import("@/features/invite/invite-landing");
    Object.assign(hookState, {
      preview: preview(),
      me: { isPending: false, data: { user: { name: "Bob" } } },
    });
    render(<InviteLanding token="t" />);
    fireEvent.click(screen.getByRole("button", { name: "Join as Bob" }));
    expect(accept).toHaveBeenCalledWith("t", expect.anything());
  });

  it("explains dead links instead of offering to join", async () => {
    const { InviteLanding } = await import("@/features/invite/invite-landing");
    Object.assign(hookState, {
      preview: preview("expired"),
      me: { isPending: false, data: { user: { name: "Bob" } } },
    });
    render(<InviteLanding token="t" />);
    expect(screen.getByRole("alert")).toHaveTextContent("expired");
    expect(screen.queryByRole("button", { name: /Join/ })).toBeNull();
  });
});

function chat(role: Chat["role"]): Chat {
  return {
    id: "c1", title: "T", role, owner: { id: "o", name: "Alice", avatar_color: "#000" },
    status: "active", provider: "fake", model: "m", max_output_tokens: 1, member_count: 2,
    key: null, last_seq: 1, created_at: "", updated_at: "",
  };
}

const members = {
  data: [
    { user_id: "o", name: "Alice", avatar_color: "#000", role: "owner", joined_at: "", online: true },
    { user_id: "b", name: "Bob", avatar_color: "#111", role: "editor", joined_at: "", online: false },
  ],
};

describe("Participants", () => {
  it("gives the owner role controls and an invite button, but none on themselves", async () => {
    const { Participants } = await import("./participants");
    hookState.members = members;
    const onInvite = vi.fn();
    render(<Participants chat={chat("owner")} meId="o" online={["o", "b"]} onInvite={onInvite} />);
    expect(screen.getByLabelText("Role for Bob")).toHaveValue("editor");
    expect(screen.getByRole("button", { name: "Remove Bob" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Role for Alice")).toBeNull();
    expect(screen.getAllByLabelText("online")).toHaveLength(2); // live presence wins
    fireEvent.click(screen.getByRole("button", { name: /Invite/ }));
    expect(onInvite).toHaveBeenCalled();
  });

  it("collaborators see roles and can leave, but not manage others", async () => {
    const { Participants } = await import("./participants");
    hookState.members = members;
    render(<Participants chat={chat("editor")} meId="b" online={null} onInvite={vi.fn()} />);
    expect(screen.queryByLabelText("Role for Bob")).toBeNull();
    expect(screen.queryByRole("button", { name: /Invite/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Leave chat/ })).toBeInTheDocument();
    expect(screen.getByLabelText("offline")).toBeInTheDocument(); // REST snapshot fallback
  });
});
