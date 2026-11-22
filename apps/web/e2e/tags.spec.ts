import { expect, test } from "@playwright/test";
import { createInvite, createReadyChat, joinViaInvite, RUN, send, signIn, type Person } from "./helpers";

/**
 * Reply modes + tags, owner and editor in two browsers: @skip and @off never get a
 * reply, the owner switches the chat to "only when tagged @ai", and the composer's
 * @ suggestions add a tag that gets an answer. @off text never reaches the model.
 */
test.describe.serial("reply modes and tags", () => {
  let owner: Person;
  let editor: Person;

  test.beforeAll(async ({ browser }) => {
    owner = await signIn(browser, "Tara");
    await createReadyChat(owner, `Tags ${RUN}`);
    editor = await joinViaInvite(browser, await createInvite(owner, "editor"), "Ezra");
  });

  test.afterAll(async () => {
    for (const p of [owner, editor]) await p?.ctx.close();
  });

  const assistant = (p: Person) => p.page.getByRole("article", { name: "Assistant said" });

  test("@skip and @off messages get no reply, and everyone sees why", async () => {
    const box = editor.page.getByRole("textbox", { name: "Message", exact: true });
    await box.fill("@skip noting the hotel is booked");
    await expect(editor.page.getByTestId("intent-preview")).toHaveText("No reply · AI sees it");
    await box.press("Enter");
    await send(owner, "@off the door code is 4321");

    for (const p of [owner, editor]) {
      await expect(
        p.page.locator("article[data-msg-id]", { hasText: "hotel is booked" }),
      ).toContainText("hotel is booked"); // no clutter: just an icon by the time
      await expect(
        p.page
          .locator("article[data-msg-id]", { hasText: "hotel is booked" })
          .getByRole("img", { name: "No reply: the AI can still see this" }),
      ).toBeVisible();
      await expect(
        p.page.locator("article[data-msg-id]", { hasText: "door code" }),
      ).toContainText("door code");
      await expect(
        p.page
          .locator("article[data-msg-id]", { hasText: "door code" })
          .getByRole("img", { name: "Off the record: hidden from the AI" }),
      ).toBeVisible();
    }
    await owner.page.waitForTimeout(1500); // longer than the debounce: nothing should start
    await expect(assistant(owner)).toHaveCount(0);
  });

  test("in tags mode only @ai gets a reply; the AI sees @skip but never @off", async () => {
    await owner.page.getByRole("radio", { name: /Only when tagged @ai/ }).check();
    await expect(
      editor.page.getByRole("textbox", { name: "Message", exact: true }),
    ).toHaveAttribute("placeholder", /add @ai/);

    await send(editor, "anyone up for dinner?");
    const dinner = owner.page.locator("article[data-msg-id]", { hasText: "dinner?" });
    await expect(dinner).toBeVisible();
    await expect(dinner.getByRole("img", { name: /No reply/ })).toHaveCount(0); // the norm here

    // pick @ai from the suggestions with the keyboard
    const box = editor.page.getByRole("textbox", { name: "Message", exact: true });
    await box.pressSequentially("@");
    await expect(editor.page.getByRole("option")).toHaveCount(3);
    await box.press("Enter");
    await expect(box).toHaveValue("@ai ");
    await box.pressSequentially("pick a restaurant");
    await expect(editor.page.getByTestId("intent-preview")).toHaveText("AI will reply");
    await box.press("Enter");

    for (const p of [owner, editor]) {
      const reply = assistant(p).last();
      await expect(reply).toContainText("pick a restaurant");
      await expect(reply).not.toContainText("dinner?"); // context, not answered
    }
    await expect(assistant(owner)).toHaveCount(1);
    await expect(assistant(owner).last()).not.toContainText("4321");
  });
});
