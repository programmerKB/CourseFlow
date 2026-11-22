import { test, expect, type Page } from "@playwright/test";
async function login(page: Page, name = "student01") {
  await page.getByRole("button", { name: "登入帳號", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("帳號", { exact: true }).fill(name);
  await dialog.getByLabel("密碼", { exact: true }).fill("Demo123!");
  await dialog.getByRole("button", { name: "登入帳號", exact: true }).click();
  await expect(dialog).toBeHidden();
}
test("訪客查課、學生加退選、課表、初選登記與助理", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/courses");
  await expect(
    page.getByRole("heading", { name: "找到適合你的課程" }),
  ).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(12);
  await page.screenshot({ path: "../docs/desktop.png", fullPage: true });
  await page.getByLabel("課程名稱或代碼").fill("資料庫");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "資料庫設計", exact: true }).click();
  const detail = page.getByRole("dialog");
  await expect(detail.getByText("週二 13:00–15:00")).toBeVisible();
  await expect(detail.getByText("週四 13:00–14:00")).toBeVisible();
  await detail.getByRole("button", { name: "關閉對話框" }).click();
  await login(page);
  await page.getByLabel("課程名稱或代碼").fill("程式設計");
  await expect(page.locator("tbody tr")).toHaveCount(2);
  await page
    .locator("tbody tr")
    .filter({ hasText: "班別 A" })
    .getByRole("button", { name: "加選課程" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "確認提交" })
    .click();
  await expect(page.getByRole("alert")).toContainText("加選成功");
  await page.getByRole("button", { name: "關閉訊息" }).click();
  await page.getByRole("button", { name: "我的課表", exact: true }).click();
  await expect(page.locator(".class-block")).toHaveCount(2);
  await page.screenshot({ path: "../docs/timetable.png", fullPage: true });
  await page.getByRole("button", { name: "退選", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "確認提交" })
    .click();
  await expect(page.getByRole("alert")).toContainText("退選成功");
  await expect(page.locator(".class-block")).toHaveCount(0);
  await page.getByRole("button", { name: "關閉訊息" }).click();
  await page.getByRole("button", { name: "選課助理", exact: true }).click();
  await page.getByLabel("輸入選課問題").fill("找星期二還有空位的電子系課");
  await page.getByRole("button", { name: "送出", exact: true }).click();
  await expect(page.locator(".message.assistant")).toContainText("EE101");
  await page.getByRole("button", { name: "課程查詢", exact: true }).click();
  await page
    .getByLabel("選擇學期")
    .selectOption({ label: "115-2 初選登記示範" });
  await page.getByLabel("課程名稱或代碼").fill("CS101");
  await expect(page.locator("tbody tr")).toHaveCount(2);
  const initialRow = page.locator("tbody tr").filter({ hasText: "班別 A" });
  if (
    await initialRow
      .getByRole("button", { name: "取消登記", exact: true })
      .count()
  ) {
    await initialRow
      .getByRole("button", { name: "取消登記", exact: true })
      .click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "確認提交" })
      .click();
    await expect(
      initialRow.getByRole("button", { name: "登記初選" }),
    ).toBeVisible();
  }
  await initialRow.getByRole("button", { name: "登記初選" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "確認提交" })
    .click();
  await expect(page.getByRole("alert")).toContainText("目前待分發");
  await page.getByRole("button", { name: "關閉訊息" }).click();
  await page.getByRole("button", { name: "我的選課", exact: true }).click();
  await expect(page.locator("tbody")).toContainText("待分發");
  await page.getByRole("button", { name: "取消登記", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "確認提交" })
    .click();
  await expect(page.locator("tbody")).toContainText("已取消登記");
  expect(errors).toEqual([]);
});
test("管理員可瀏覽分發與通知並調整容量", async ({ page }) => {
  await page.goto("/courses");
  await login(page, "admin");
  await page.getByLabel("選擇學期").selectOption({ label: "115-1 示範學期" });
  await page.getByRole("button", { name: "教務管理", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Email 通知狀態" }),
  ).toBeVisible();
  const row = page.locator("tbody tr").filter({ hasText: "CS303 計算機網路" });
  await row.getByRole("button", { name: "編輯容量" }).click();
  await page.getByRole("dialog").getByLabel("班別容量").fill("1");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "儲存班別" })
    .click();
  await expect(page.getByRole("alert")).toContainText("班別已儲存");
});
test("手機介面可操作且主頁無水平溢出", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/courses");
  await expect(page.locator("tbody tr")).toHaveCount(12);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({ path: "../docs/mobile.png", fullPage: true });
  await page.getByRole("button", { name: "選課規則", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "從登記，到正式選課" }),
  ).toBeVisible();
});
