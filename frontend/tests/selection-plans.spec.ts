import { test, expect, type Page } from "@playwright/test";

async function signIn(page: Page, name = "student01") {
  await page.getByRole("button", { name: "登入帳號", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("帳號", { exact: true }).fill(name);
  await dialog.getByLabel("密碼", { exact: true }).fill("Demo123!");
  await dialog.getByRole("button", { name: "登入帳號", exact: true }).click();
  await expect(dialog).toBeHidden();
}

async function ask(page: Page, question: string) {
  await page.getByLabel("輸入選課問題").fill(question);
  await page.getByRole("button", { name: "送出", exact: true }).click();
}

const RESEARCH_QUESTION = "我的研究方向是大語言模型，請推薦並幫我選三門。";

for (const [phase, label, button, resultText] of [
  ["ADD_DROP", "115-4 研究方向推薦示範", "確認加選", "2 門已加選，正式課表已更新。"],
  ["INITIAL", "115-5 研究方向初選示範", "確認初選登記", "2 門已登記，等待分發。"],
] as const) {
  test(`研究推薦 ${phase} 可刪減並確認兩門`, async ({ page }) => {
    await page.goto("/courses");
    await signIn(page);
    const termPicker = page.getByLabel("選擇學期");
    await termPicker.selectOption({ label });
    const termId = await termPicker.inputValue();
    await page.getByRole("button", { name: "選課助理", exact: true }).click();
    const keys: string[] = [];
    let submitted: { section_id: string }[] = [];
    await page.route("**/selection-plans/*/confirm", async (route) => {
      keys.push((await route.request().headerValue("Idempotency-Key")) ?? "");
      const response = await route.fetch();
      if (response.ok()) submitted = (await response.json()).items;
      if (phase === "ADD_DROP" && keys.length === 1) await route.abort();
      else await route.fulfill({ response });
    });
    try {
      await ask(page, RESEARCH_QUESTION);
      const review = page.locator(".recommend-plan");
      await expect(review.getByRole("checkbox")).toHaveCount(3);
      await expect(review).toContainText(phase === "ADD_DROP" ? "預估 18.0 學分" : "預估 9.0 學分");
      await expect(page.getByRole("heading", { name: "研究延伸" })).toBeVisible();
      await review.getByRole("checkbox", { name: /DEMO-502/ }).uncheck();
      await expect(review).toContainText(phase === "ADD_DROP" ? "預估 15.0 學分" : "預估 6.0 學分");
      await review.getByRole("button", { name: button, exact: true }).click();
      if (phase === "ADD_DROP") {
        await review.getByRole("button", { name: "重試同一提交" }).click();
      }
      await expect(review.getByRole("status")).toContainText(resultText);
      expect(submitted).toHaveLength(2);
      expect(keys).toHaveLength(phase === "ADD_DROP" ? 2 : 1);
      expect(keys[0]).not.toBe("");
      expect(new Set(keys).size).toBe(1);
      await review.getByRole("button", { name: phase === "ADD_DROP" ? "查看我的課表" : "查看我的選課" }).click();
      await expect(page.getByRole("main").getByText(/大型語言模型專題/).first()).toBeVisible();
    } finally {
      const collection = phase === "ADD_DROP" ? "enrollments" : "applications";
      for (const row of submitted) {
        const response = await page.request.delete(`/api/v1/me/terms/${termId}/${collection}/${row.section_id}`, {
          headers: { "Idempotency-Key": `e2e-cleanup-${Date.now()}-${row.section_id}` },
        });
        expect(response.status()).toBe(204);
      }
    }
  });
}

test("更換學生時，上一個學生的助理請求不會寫回對話", async ({ page }) => {
  await page.goto("/courses");
  await signIn(page);
  await page.getByRole("button", { name: "選課助理", exact: true }).click();
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  let settle = () => {};
  const settled = new Promise<void>((resolve) => { settle = resolve; });
  await page.route("**/api/v1/chat", async (route) => {
    try {
      await held;
      await route.fulfill({ json: { mode: "student_query", answer: "上一個學生的虛構課表", sources: [] } });
    } catch {
      // The old fetch may have been cancelled by the new account scope.
    } finally {
      settle();
    }
  });
  const sent = page.waitForRequest("**/api/v1/chat");
  await ask(page, "查詢我的課表");
  await sent;
  await page.getByRole("button", { name: "登出", exact: true }).click();
  await signIn(page, "student02");
  await page.getByRole("button", { name: "選課助理", exact: true }).click();
  release();
  await settled;
  await expect(page.locator(".messages")).not.toContainText("上一個學生的虛構課表");
  await expect(page.getByRole("button", { name: RESEARCH_QUESTION, exact: true })).toBeEnabled();
});

test("切換班別後，舊班別的資格結果不會顯示在新詳情", async ({ page }) => {
  await page.goto("/courses");
  await signIn(page);
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  let settle = () => {};
  const settled = new Promise<void>((resolve) => { settle = resolve; });
  await page.route("**/eligibility", async (route) => {
    try {
      await held;
      await route.fulfill({ json: { eligible: true, message: "舊班別的資格結果" } });
    } catch {
      // Changing the selected section may cancel the request.
    } finally {
      settle();
    }
  });
  await page.getByRole("button", { name: "資料庫設計", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const sent = page.waitForRequest("**/eligibility");
  await dialog.getByRole("button", { name: "檢查目前選課條件" }).click();
  await sent;
  await dialog.getByRole("button", { name: "關閉對話框" }).click();
  await page.getByRole("button", { name: "程式設計", exact: true }).first().click();
  await expect(dialog.getByRole("heading", { name: /程式設計/ })).toBeVisible();
  release();
  await settled;
  await expect(dialog).not.toContainText("舊班別的資格結果");
  await expect(dialog.getByRole("button", { name: "檢查目前選課條件" })).toBeEnabled();
});
