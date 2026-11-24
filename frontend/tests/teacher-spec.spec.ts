import { test, expect, type Page } from "@playwright/test";

// Requires the teacher demo seed (student05 starts with 15 credits) and an ADD_DROP 115-1 term.
async function login(page: Page, name: string) {
  await page.getByRole("button", { name: "登入帳號", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("帳號", { exact: true }).fill(name);
  await dialog.getByLabel("密碼", { exact: true }).fill("Demo123!");
  await dialog.getByRole("button", { name: "登入帳號", exact: true }).click();
  await expect(dialog).toBeHidden();
}

async function say(page: Page, text: string) {
  await page.getByLabel("輸入選課問題").fill(text);
  await page.getByRole("button", { name: "送出", exact: true }).click();
  await expect(page.locator(".message[role=status]")).toHaveCount(0);
  return page.locator(".message.assistant").last();
}

test("英文助理加退選、18 學分與衝堂限制，教師名單與成績", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/chat");
  await login(page, "student05");

  let reply = await say(page, "Please enroll me in CS101, section A.");
  await expect(reply).toContainText("Done. You are now enrolled in CS101");
  await expect(reply).toContainText("18.0 of 18.0 credits");
  await expect(reply).toContainText("選課確認信已排入寄送（Gmail API）");
  await expect(reply.locator(".agent-trace")).toContainText("add_course");
  reply = await say(page, "I'd like to add CS303 A.");
  await expect(reply).toContainText("exceed your credit limit");
  reply = await say(page, "Please enroll me in CS302, section A.");
  await expect(reply).toContainText("overlaps");
  await page.screenshot({ path: testInfo.outputPath("english-chat.png"), fullPage: true });

  await page.getByRole("button", { name: "我的課表" }).click();
  await expect(page.locator(".credit-summary strong")).toContainText("18");
  await page.getByRole("button", { name: "選課助理" }).click();
  reply = await say(page, "Please drop CS101, section A.");
  await expect(reply).toContainText("You have dropped CS101");
  await page.getByRole("button", { name: "我的課表" }).click();
  await expect(page.locator(".credit-summary strong")).toContainText("15");
  await page.getByRole("button", { name: "登出" }).click();

  await login(page, "teacher01");
  await expect(page.getByRole("button", { name: "我的選課" })).toHaveCount(0);
  await page.getByRole("button", { name: "我的授課" }).click();
  await page.getByRole("listitem").filter({ hasText: "CS301" }).click();
  const row = page.locator("table.roster tbody tr").filter({ hasText: "B11500005" });
  await expect(row).toContainText("未評分");
  const input = row.getByLabel("B11500005 成績");
  await input.fill("101");
  await row.getByRole("button", { name: "儲存" }).click();
  await expect(row.getByRole("alert")).toContainText("0–100");
  await input.fill("82");
  await row.getByRole("button", { name: "儲存" }).click();
  await expect(page.locator(".toast")).toContainText("成績已儲存：82.00 分");
  await input.fill("90.5");
  await row.getByRole("button", { name: "儲存" }).click();
  await expect(row).toContainText("90.50");
  await page.screenshot({ path: testInfo.outputPath("instructor-grades.png"), fullPage: true });
  expect(errors).toEqual([]);
});
