import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Composer, OFF_WITH_AI } from "./composer";
import { MessageText, OffMark } from "./message-list";

const PEOPLE = [
  { id: "me", name: "Alice" },
  { id: "u2", name: "Bob" },
];

function setup() {
  const onSend = vi.fn();
  render(<Composer onSend={onSend} people={PEOPLE} meId="me" />);
  const box = screen.getByLabelText("Message") as HTMLTextAreaElement;
  const type = (value: string) =>
    fireEvent.change(box, { target: { value, selectionStart: value.length } });
  return { onSend, box, type };
}

describe("Composer mentions", () => {
  it("@ lists AI, everyone and people (not me); picking inserts a chip", () => {
    const { onSend, box, type } = setup();
    type("@");
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(3); // not "Alice": you can't mention yourself
    expect(options[0]).toHaveTextContent("AIAsk the AI to reply");
    expect(options[1]).toHaveTextContent("everyoneAddress the whole chat");
    expect(options[2]).toHaveTextContent("Bob");
    fireEvent.keyDown(box, { key: "Enter" }); // picks AI, doesn't send
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("@AI ");
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "reply");
    type("@AI where to?");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith(
      "@AI where to?",
      [{ type: "ai", offset: 0, length: 3 }],
      false,
    );
  });

  it("typing @ai by hand is plain text (no reply)", () => {
    const { onSend, box, type } = setup();
    type("@ai");
    fireEvent.keyDown(box, { key: "Escape" }); // close the menu
    type("@ai hi");
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "context");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("@ai hi", [], false);
  });

  it("mentioning a person doesn't ask the AI", () => {
    const { onSend, box, type } = setup();
    type("@bo");
    fireEvent.keyDown(box, { key: "Enter" });
    type("@Bob can you pay?");
    expect(screen.getByTestId("intent-preview")).toHaveTextContent("No reply");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith(
      "@Bob can you pay?",
      [{ type: "user", offset: 0, length: 4, user_id: "u2" }],
      false,
    );
  });

  it("off the record: a toggle, reset after sending", () => {
    const { onSend, box, type } = setup();
    const toggle = screen.getByRole("button", { name: "Off the record" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    type("door code 4321");
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "off");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("door code 4321", [], true);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("won't send off the record with the AI mentioned, and says why", () => {
    const { onSend, box, type } = setup();
    type("@");
    fireEvent.keyDown(box, { key: "Enter" });
    type("@AI secret");
    fireEvent.click(screen.getByRole("button", { name: "Off the record" }));
    expect(screen.getByText(OFF_WITH_AI)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("editing inside a chip makes it plain text; Backspace after a chip removes it", () => {
    const { box, type } = setup();
    type("@");
    fireEvent.keyDown(box, { key: "Enter" });
    type("@AI hi");
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "reply");
    type("@Ax hi"); // edited inside the chip
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "context");

    type("");
    type("@");
    fireEvent.keyDown(box, { key: "Enter" });
    box.setSelectionRange(3, 3);
    fireEvent.keyDown(box, { key: "Backspace" });
    expect(box).toHaveValue(" ");
  });

  it("trims the message and keeps mention offsets in step", () => {
    const { onSend, box, type } = setup();
    type("  ");
    type("  @");
    fireEvent.keyDown(box, { key: "Enter" });
    type("  @AI go  ");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("@AI go", [{ type: "ai", offset: 0, length: 3 }], false);
  });
});

describe("Mentions in messages", () => {
  it("renders chips with the current name, falling back to the stored one", () => {
    const { container } = render(
      <MessageText
        text="@AI @Bob @Zed hi"
        mentions={[
          { type: "ai", offset: 0, length: 3 },
          { type: "user", offset: 4, length: 4, user_id: "u2", name: "Bob" },
          { type: "user", offset: 9, length: 4, user_id: "gone", name: "Zed" },
        ]}
        nameOf={(id) => (id === "u2" ? "Robert" : undefined)}
      />,
    );
    const chips = [...container.querySelectorAll("[data-mention]")].map((c) => c.textContent);
    expect(chips).toEqual(["@AI", "@Robert", "@Zed"]);
    expect(container).toHaveTextContent("@AI @Robert @Zed hi");
  });

  it("marks off-the-record messages with a labelled icon", () => {
    render(<OffMark />);
    expect(screen.getByRole("img", { name: /hidden from the AI/ })).toBeInTheDocument();
  });
});
