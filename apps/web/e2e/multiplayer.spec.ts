import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import {
  createInvite,
  createReadyChat,
  humanMessages,
  joinViaInvite,
  RUN,
  send,
  signIn,
  type Person,
} from "./helpers";

type Violation = { id: string; help: string; nodes: { target: unknown[]; html: string }[] };
const describe = (v: Violation) =>
  `${v.id}: ${v.help} -> ${v.nodes.map((n) => `${n.target.join(" ")} ${n.html.slice(0, 120)}`).join(" | ")}`;

/**
 * Three real browsers (owner, editor, viewer) in one chat, against the running stack
 * with the demo model. Covers the definition of done: live chat for three people,
 * simultaneous sends batched into one reply that addresses each person by name,
 * reconnect mid-stream, and a viewer who can't send.
 */
test.describe.serial("three people in one chat", () => {
  let owner: Person;
  let editor: Person;
  let viewer: Person;

  test.beforeAll(async ({ browser }) => {
    owner = await signIn(browser, "Olivia");
    await createReadyChat(owner, `E2E ${RUN}`);
    const editorLink = await createInvite(owner, "editor");
    const viewerLink = await createInvite(owner, "viewer");
    editor = await joinViaInvite(browser, editorLink, "Eddie");
    viewer = await joinViaInvite(browser, viewerLink, "Vera");
  });

  test.afterAll(async () => {
    for (const p of [owner, editor, viewer]) await p?.ctx.close();
  });

  test("everyone sees who joined and who is online", async () => {
    await expect(owner.page.getByText("Eddie joined as editor")).toBeVisible();
    await expect(owner.page.getByText("Vera joined as viewer")).toBeVisible();
    const people = owner.page.getByRole("region", { name: /People/ }).or(owner.page.locator("section[aria-labelledby='participants-title']"));
    await expect(people.getByText("Eddie")).toBeVisible();
    await expect(owner.page.getByLabel("3 online")).toBeVisible();
  });

  test("simultaneous sends are batched into one reply addressing each person", async () => {
    await Promise.all([send(owner, "Olivia asks: plan the trip?", { ai: true }), send(editor, "Eddie asks: budget?", { ai: true })]);

    for (const p of [owner, editor, viewer]) {
      const reply = p.page.getByRole("article", { name: "Assistant said" }).last();
      await expect(reply).toContainText("Olivia, you said:");
      await expect(reply).toContainText("Eddie, you said:");
      await expect(reply).toContainText(/Replying to\s*(Olivia\s*Eddie|Eddie\s*Olivia)/);
    }
    // identical order everywhere (seq-ordered, not arrival-ordered)
    const orders = await Promise.all([owner, editor, viewer].map((p) => humanMessages(p.page)));
    expect(orders[1]).toEqual(orders[0]);
    expect(orders[2]).toEqual(orders[0]);
    await expect(owner.page.getByRole("article", { name: "Assistant said" })).toHaveCount(1);
  });

  test("a viewer reads along but cannot send", async () => {
    await expect(viewer.page.getByText(/You're a viewer here/)).toBeVisible();
    await expect(viewer.page.getByLabel("Message", { exact: true })).toHaveCount(0);
  });

  test("reconnecting mid-stream shows the full reply without gaps or repeats", async () => {
    const long = "Tell me everything. " + "Lorem ipsum dolor sit amet consectetur. ".repeat(25);
    await send(editor, long, { ai: true });
    await expect(viewer.page.getByRole("article", { name: "Assistant is replying" })).toBeVisible();
    await viewer.ctx.setOffline(true);
    await viewer.page.waitForTimeout(2500);
    await viewer.ctx.setOffline(false);

    const ownerReply = owner.page.getByRole("article", { name: "Assistant said" }).last();
    await expect(ownerReply).toContainText("Eddie, you said:", { timeout: 60_000 });
    const viewerReply = viewer.page.getByRole("article", { name: "Assistant said" }).last();
    await expect(viewerReply).toContainText("Eddie, you said:", { timeout: 60_000 });
    // wait for the whole reply to finish everywhere before comparing
    for (const p of [owner, viewer]) {
      await expect(p.page.getByRole("article", { name: "Assistant is replying" })).toHaveCount(0, {
        timeout: 60_000,
      });
    }
    // compare the reply itself (the owner also has a Regenerate button)
    const body = (l: typeof ownerReply) => l.locator(".prose-chat").first().innerText();
    expect(await body(viewerReply)).toEqual(await body(ownerReply));
  });

  test("messages sent while the AI is busy are queued and answered next", async () => {
    await send(editor, "First: " + "words ".repeat(120), { ai: true });
    await expect(owner.page.getByRole("article", { name: "Assistant is replying" })).toBeVisible();
    await send(owner, "Second, while busy", { ai: true });
    await expect(
      owner.page.locator("article[data-msg-id]", { hasText: "Second, while busy" }),
    ).toContainText("Queued · will be answered next");
    await expect(
      owner.page.getByRole("article", { name: "Assistant said" }).last(),
    ).toContainText("Second, while busy", { timeout: 60_000 });
  });

  test("removing the key pauses the chat for everyone, with a clear reason", async () => {
    owner.page.once("dialog", (d) => d.accept());
    await owner.page.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(editor.page.getByText(/hasn't finished setting up this chat/)).toBeVisible();
  });

  test("chat and login pages have no serious accessibility violations", async ({ browser }) => {
    const chat = await new AxeBuilder({ page: owner.page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const serious = chat.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
    expect(serious.map(describe)).toEqual([]);

    const anonCtx = await browser.newContext();
    const anon = await anonCtx.newPage();
    await anon.goto("/login");
    await expect(anon.getByRole("heading", { name: "Sign in" })).toBeVisible();
    const login = await new AxeBuilder({ page: anon }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(
      login.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? "")).map(describe),
    ).toEqual([]);
    await anonCtx.close();
  });
});

test("a dead invite link explains itself", async ({ page }) => {
  await page.goto("/invite/" + "x".repeat(43));
  await expect(page.getByRole("heading", { name: "Invite not found" })).toBeVisible();
});

test("unknown pages show a designed 404", async ({ page }) => {
  await page.goto("/definitely/not/here");
  await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
});
