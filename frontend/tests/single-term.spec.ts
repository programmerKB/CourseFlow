import { expect, test } from "@playwright/test";

const mainTerm = "8abc15dd-c1b8-53ed-a570-ae723e17a762";
const demoTerm = "f8ec99c3-fb0e-59ff-befc-85e4430f10d2";
const staleSection = "2f69b545-94e2-4b07-934a-125040037887";

test("單一公開學期會捨棄舊學期連結，且初選時段有日期與時間分隔", async ({ page }) => {
  const invalidRequests: string[] = [];
  await page.addInitScript((term) => localStorage.setItem("course-term", term), demoTerm);
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return route.fulfill({ status: 401, json: {} });
    if (path === "/api/v1/terms") {
      return route.fulfill({ json: [{
        id: mainTerm,
        code: "115-1 示範學期",
        catalog_published_at: "2026-11-22T11:27:00Z",
      }] });
    }
    if (["/api/v1/departments", "/api/v1/schools", "/api/v1/topics"].includes(path)) return route.fulfill({ json: [] });
    if (path === `/api/v1/terms/${mainTerm}/registration`) {
      return route.fulfill({ json: {
        phase: "INITIAL",
        periods: [{ kind: "INITIAL", starts_at: "2026-11-22T11:27:00Z", ends_at: "2026-11-29T16:00:00Z" }],
      } });
    }
    if (path === `/api/v1/terms/${mainTerm}/sections`) {
      return route.fulfill({ json: { items: [], page: 1, page_size: 20, total: 0 } });
    }
    invalidRequests.push(path);
    return route.fulfill({ status: 404, json: {} });
  });
  const staleUrl = `/courses?term=${demoTerm}&section=${staleSection}`;
  await page.goto(staleUrl);
  await expect(page.locator(".top-actions").getByText("115-1 示範學期")).toBeVisible();
  await expect(page.getByLabel("選擇學期")).toHaveCount(0);
  await expect(page).toHaveURL(/\/courses$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText("2026/11/22 19:27 ～ 2026/11/30 00:00")).toBeVisible();
  await page.evaluate((url) => {
    history.pushState({}, "", url);
    history.pushState({}, "", "/rules");
  }, staleUrl);
  await page.goBack();
  await expect(page).toHaveURL(/\/courses$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(invalidRequests).toEqual([]);
});

test("代理誤回 HTML 時顯示錯誤而不會把頁面當成學期資料", async ({ page }) => {
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 401, json: {} }));
  await page.route("**/api/v1/terms", (route) => route.fulfill({ contentType: "text/html", body: "<html>proxy</html>" }));
  await page.goto("/courses");
  await expect(page.getByRole("alert")).toContainText("伺服器回應格式錯誤");
});
