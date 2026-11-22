import { type Browser, type BrowserContext, expect, type Page } from "@playwright/test";

/** Unique per run so tests never collide with existing data. */
export const RUN = Date.now().toString(36);

export type Person = { ctx: BrowserContext; page: Page; name: string };

/** A fresh browser context (own cookies) signed in through the dev login form. */
export async function signIn(browser: Browser, name: string, path = "/"): Promise<Person> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(path);
  await page.getByRole("textbox", { name: "Display name" }).fill(name);
  await page.getByRole("textbox", { name: "Email" }).fill(`${name.toLowerCase()}-${RUN}@example.com`);
  await page.getByRole("button", { name: "Continue" }).click();
  return { ctx, page, name };
}

/** Owner creates a chat and completes setup with the keyless demo model. */
export async function createReadyChat(owner: Person, title: string): Promise<string> {
  const { page } = owner;
  await page.getByRole("button", { name: "New chat" }).last().click();
  await expect(page).toHaveURL(/\/c\//);
  await page.getByText("Demo (no key)").first().click();
  await page.getByRole("textbox", { name: "API key" }).first().fill("demo-key-e2e");
  await page.getByRole("button", { name: "Save & verify" }).first().click();
  await expect(page.getByText("Verified").first()).toBeVisible();
  await page.getByRole("combobox", { name: "Model" }).first().selectOption("fake-small");
  await expect(page.getByText("Say hi.")).toBeVisible(); // setup done: empty chat
  await page.getByRole("textbox", { name: "Title" }).fill(title);
  await page.getByRole("textbox", { name: "Title" }).press("Enter");
  return page.url();
}

export async function createInvite(owner: Person, role: "editor" | "viewer"): Promise<string> {
  const { page } = owner;
  await page.getByRole("button", { name: "Invite people" }).click();
  await page.getByRole("combobox", { name: "Role" }).selectOption(role);
  await page.getByRole("button", { name: "Create invite link" }).click();
  const link = await page.getByRole("textbox", { name: "Invite link" }).inputValue();
  await page.keyboard.press("Escape");
  return link;
}

/** Visitor opens the link signed out, signs in, and is joined automatically. */
export async function joinViaInvite(browser: Browser, link: string, name: string): Promise<Person> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.goto(link);
  await expect(page.getByText("You've been invited to")).toBeVisible();
  await page.getByRole("button", { name: "Sign in to join" }).click();
  await page.getByRole("textbox", { name: "Display name" }).fill(name);
  await page.getByRole("textbox", { name: "Email" }).fill(`${name.toLowerCase()}-${RUN}@example.com`);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page).toHaveURL(/\/c\//);
  return { ctx, page, name };
}

/** Pick someone from the composer's @ menu (inserts a mention chip). */
export async function mention(p: Person, who: "AI" | "everyone" | string): Promise<void> {
  const box = p.page.getByRole("textbox", { name: "Message", exact: true });
  await box.pressSequentially("@");
  await p.page.getByRole("option").filter({ hasText: who }).first().click();
}

/** Type and send a message. `ai` mentions the AI (the only way to get a reply),
 *  `to` mentions people, `off` sends it off the record. */
export async function send(
  p: Person,
  text: string,
  opts: { ai?: boolean; to?: string[]; off?: boolean } = {},
): Promise<void> {
  const box = p.page.getByRole("textbox", { name: "Message", exact: true });
  await box.fill("");
  if (opts.off) await p.page.getByRole("button", { name: "Off the record" }).click();
  if (opts.ai) await mention(p, "AI");
  for (const who of opts.to ?? []) await mention(p, who);
  await box.focus();
  await p.page.keyboard.insertText(text);
  await box.press("Enter");
}

/** Ids of every human message, in on-screen order (same for every viewer). */
export async function humanMessages(page: Page): Promise<string[]> {
  return page
    .locator("article[data-msg-id]")
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-msg-id") ?? ""));
}
