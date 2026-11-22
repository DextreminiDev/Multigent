import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import {
  createInvite,
  createReadyChat,
  joinViaInvite,
  mention,
  RUN,
  send,
  signIn,
  type Person,
} from "./helpers";

/**
 * Mentions and off the record, three people in three browsers: the AI replies only
 * when mentioned; mentioning a person highlights the message for them (never for
 * the sender); @everyone highlights it for everyone else; off-the-record messages
 * are marked and can't mention the AI.
 */
test.describe.serial("mentions and off the record", () => {
  let tara: Person;
  let ezra: Person;
  let finn: Person;

  test.beforeAll(async ({ browser }) => {
    tara = await signIn(browser, "Tara");
    await createReadyChat(tara, `Mentions ${RUN}`);
    ezra = await joinViaInvite(browser, await createInvite(tara, "editor"), "Ezra");
    finn = await joinViaInvite(browser, await createInvite(tara, "editor"), "Finn");
  });

  test.afterAll(async () => {
    for (const p of [tara, ezra, finn]) await p?.ctx.close();
  });

  const msg = (page: Page, text: string) => page.locator("article[data-msg-id]", { hasText: text });
  const assistant = (p: Person) => p.page.getByRole("article", { name: "Assistant said" });
  const forMe = '[data-mentions-me="true"]';

  test("a plain message gets no reply", async () => {
    await send(tara, "hotel is booked");
    await expect(msg(ezra.page, "hotel is booked")).toBeVisible();
    await tara.page.waitForTimeout(1500); // longer than the debounce: nothing starts
    await expect(assistant(tara)).toHaveCount(0);
  });

  test("mentioning a person highlights it for them only, and gets no reply", async () => {
    await send(tara, "can you pay for it?", { to: ["Ezra"] });
    await expect(msg(ezra.page, "can you pay")).toContainText("@Ezra");
    await expect(ezra.page.locator(forMe, { hasText: "can you pay" })).toBeVisible();
    await expect(msg(finn.page, "can you pay")).toBeVisible();
    await expect(finn.page.locator(forMe, { hasText: "can you pay" })).toHaveCount(0);
    await expect(tara.page.locator(forMe, { hasText: "can you pay" })).toHaveCount(0); // sender
    await expect(assistant(tara)).toHaveCount(0);
  });

  test("@everyone highlights it for everyone except the sender", async () => {
    await send(ezra, "dinner at 8", { to: ["everyone"] });
    for (const p of [tara, finn]) {
      await expect(p.page.locator(forMe, { hasText: "dinner at 8" })).toBeVisible();
    }
    await expect(msg(ezra.page, "dinner at 8")).toBeVisible();
    await expect(ezra.page.locator(forMe, { hasText: "dinner at 8" })).toHaveCount(0);
  });

  test("off the record is marked, and can't mention the AI", async () => {
    await send(finn, "the door code is 4321", { off: true });
    for (const p of [tara, ezra]) {
      await expect(
        msg(p.page, "door code").getByRole("img", { name: "Off the record: hidden from the AI" }),
      ).toBeVisible();
    }

    const box = finn.page.getByRole("textbox", { name: "Message", exact: true });
    await mention(finn, "AI");
    await finn.page.keyboard.insertText("secret");
    await finn.page.getByRole("button", { name: "Off the record" }).click();
    await expect(
      finn.page.getByText("The AI can't be mentioned in an off-the-record message."),
    ).toBeVisible();
    await expect(finn.page.getByRole("button", { name: "Send message" })).toBeDisabled();
    await finn.page.getByRole("button", { name: "Off the record" }).click(); // back on the record
    await box.fill("");
  });

  test("mentioning the AI gets one reply that addresses each person", async () => {
    await Promise.all([
      send(tara, "Tara asks: pick a restaurant", { ai: true }),
      send(ezra, "Ezra asks: what about dessert?", { ai: true }),
    ]);
    for (const p of [tara, ezra, finn]) {
      const reply = assistant(p).last();
      await expect(reply).toContainText("Tara, you said:");
      await expect(reply).toContainText("Ezra, you said:");
      await expect(reply).not.toContainText("4321"); // off the record never reached it
    }
    await expect(assistant(tara)).toHaveCount(1);
  });

  test("the chat with the mention menu open has no serious accessibility violations", async () => {
    const box = tara.page.getByRole("textbox", { name: "Message", exact: true });
    await box.fill("");
    await box.pressSequentially("@");
    await expect(tara.page.getByRole("listbox", { name: "Mention someone" })).toBeVisible();
    const result = await new AxeBuilder({ page: tara.page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const serious = result.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
    expect(serious.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
    await box.press("Escape");
    await box.fill("");
  });
});
