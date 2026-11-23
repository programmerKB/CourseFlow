import { expect, test, type Page } from "@playwright/test";

const universityTerm = "115-6 三校電資研究所示範";
test.use({ launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] } });

async function openUniversity(page: Page) {
  await page.goto("/courses");
  await page.getByLabel("選擇學期").selectOption({ label: universityTerm });
}

async function login(page: Page, username: string) {
  await page.getByRole("button", { name: "登入帳號", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("帳號", { exact: true }).fill(username);
  await dialog.getByLabel("密碼", { exact: true }).fill("Demo123!");
  await dialog.getByRole("button", { name: "登入帳號", exact: true }).click();
  await expect(dialog).toBeHidden();
  await page.locator(".sidebar").getByRole("button", { name: "選課助理", exact: true }).click();
}

async function ask(page: Page, message: string) {
  await page.getByLabel("輸入選課問題").fill(message);
  await page.getByRole("button", { name: "送出", exact: true }).click();
}

test("三校篩選、班別來源、同名選班和網路重送", async ({ page }) => {
  await openUniversity(page);
  await page.getByLabel("開課學校").selectOption("NTUST");
  await page.getByLabel("開課系所").selectOption({ label: "電子工程系" });
  await page.getByLabel("課程主題").selectOption("memory_systems");
  await page.getByLabel("課程名稱或代碼").fill("高等計算機結構");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "高等計算機結構", exact: true }).click();
  const detail = page.getByRole("dialog");
  await expect(detail.getByText("模擬選課")).toBeVisible();
  await expect(detail.getByRole("link", { name: "校方課程來源" })).toHaveAttribute("href", /https:\/\/ece\.ntust\.edu\.tw/);
  await detail.getByRole("button", { name: "關閉對話框" }).click();

  await login(page, "student07");
  await ask(page, "我要加選高等計算機結構");
  const choices = page.getByRole("list", { name: "待選班別" });
  await expect(choices.getByRole("listitem")).toHaveCount(3);
  const keys: string[] = [];
  await page.route("**/api/v1/chat", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    if (keys.length === 1) await route.abort("failed");
    else await route.fulfill({ response });
  });
  await choices.getByRole("listitem").filter({ hasText: "NTNU-CS-002" })
    .getByRole("button", { name: "加選此班" }).click();
  await page.getByRole("button", { name: "重送同一請求" }).click();
  await expect(page.locator(".message.assistant").last()).toContainText("已加選 國立臺灣師範大學");
  expect(keys.length).toBe(2);
  expect(keys[0]).toBeTruthy();
  expect(keys[0]).toBe(keys[1]);
  await page.locator(".sidebar").getByRole("button", { name: "我的課表", exact: true }).click();
  await expect(page.locator(".class-block")).toHaveCount(1);
  await expect(page.locator(".class-block")).toContainText("高等計算機結構");
});

test("先瀏覽三階段研究路徑，再要求試排", async ({ page }) => {
  await openUniversity(page);
  await login(page, "student08");
  await ask(page, "IC 設計");
  await page.getByRole("button", { name: "類比與混合訊號 IC", exact: true }).click();
  const roadmap = page.getByRole("region", { name: "研究修課路徑", exact: true });
  await expect(roadmap.getByRole("heading", { name: "類比與混合訊號 IC修課路徑" })).toBeVisible();
  for (const title of ["基礎", "進階", "研究專題"]) {
    await expect(roadmap.getByRole("heading", { name: title, exact: true })).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "確認加選", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "試排本期課程", exact: true }).click();
  await expect(page.getByRole("button", { name: "確認加選", exact: true })).toBeVisible();
});

test.describe("瀏覽器麥克風", () => {
  test("取消錄音、錄音上傳與繁中朗讀", async ({ page }) => {
    await page.addInitScript(() => {
      window.speechSynthesis.speak = (utterance) => {
        document.documentElement.dataset.spokenLanguage = utterance.lang;
        document.documentElement.dataset.spokenText = utterance.text;
      };
    });
    const uploads: string[] = [];
    await page.route("**/api/v1/chat/voice", async (route) => {
      const headers = route.request().headers();
      expect(headers["content-type"]).toContain("multipart/form-data; boundary=");
      expect(headers["idempotency-key"]).toBeTruthy();
      expect(route.request().postData()).toContain('name="audio"');
      uploads.push(headers["idempotency-key"]);
      await route.fulfill({ json: {
        mode: "student_query", answer: "本學期目前沒有正式選課。", transcript: "我剛剛選了什麼？",
        transcript_status: "CLEAR", sources: [], as_of: new Date().toISOString(), request_id: "fixture",
      } });
    });
    await openUniversity(page);
    await login(page, "student09");
    await page.getByRole("button", { name: "開始錄音" }).click();
    await page.getByRole("button", { name: "停止並送出" }).waitFor();
    await page.getByRole("button", { name: "取消錄音" }).click();
    await expect(page.getByRole("button", { name: "開始錄音" })).toBeEnabled();
    expect(uploads).toEqual([]);
    await page.getByRole("button", { name: "開始錄音" }).click();
    await expect(page.getByLabel("錄音中，1 秒")).toBeVisible();
    await page.getByRole("button", { name: "停止並送出" }).click();
    await expect(page.getByText("語音辨識 我剛剛選了什麼？")).toBeVisible();
    await page.getByRole("button", { name: "朗讀回覆", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-spoken-language", "zh-TW");
    await expect(page.locator("html")).toHaveAttribute("data-spoken-text", "本學期目前沒有正式選課。");
    expect(uploads).toHaveLength(1);
  });

  test("拒絕權限後可繼續以文字查課", async ({ page }) => {
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException("Denied", "NotAllowedError"));
    });
    await openUniversity(page);
    await login(page, "student10");
    await page.getByRole("button", { name: "開始錄音" }).click();
    await expect(page.getByText("麥克風未獲授權。請開放權限或改用文字輸入。")).toBeVisible();
    await ask(page, "我剛剛選了什麼？");
    await expect(page.locator(".message.assistant")).toContainText("正式選課");
  });
});
