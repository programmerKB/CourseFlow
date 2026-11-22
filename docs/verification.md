# 驗證報告

日期：2026-11-21（Asia/Taipei）。驗證對象為本目錄內的實作。

## 已實測

| 檢查 | 結果 |
|---|---|
| PostgreSQL 整合 / 併發、SMTP 協定及助理診斷測試 | **47 passed**，17.45 秒 |
| React / TypeScript 正式建置 | 通過，Vite 6.4.1 |
| Chromium 端到端 | **3 passed**；公開查課、登入、詳情多時段、加退選、課表、初選登記取消、助理來源、管理容量及手機版 |
| Compose 設定 | `docker compose config --quiet` 通過 |
| Alembic | 在獨立的全新 `courses` 與 `courses_test` PostgreSQL 資料庫成功套用初版 migration |
| SMTP 故障 / 恢復 | 實際 TCP 連線拒絕後保持選課成功，第二次嘗試經 SMTP 寄入 Mailpit 並更新 SENT |
| PostgreSQL 公開角色 | 可讀公開 View；讀學生資料與寫入班別皆被拒絕 |
| 效能 | 1,000 班、20 並行、25 次暖機、200 次量測，p95 **269.96 ms** |

整合測試固定業務時間為 `2026-11-21T12:00:00Z`，每個案例使用獨立重建的測試資料，交易與併發均連接 PostgreSQL 16.15。20 人搶最後一席得到恰一筆 201，其餘 19 筆 `SECTION_FULL`，資料庫有效人數等於容量。同學生競爭請求分別驗證衝堂、同課跨班、超學分及相鄰時段成功。

測試還包含半開期限邊界、初選重送與換班、固定種子的預期勝出者、分發重送、資料庫錯誤引發整批回滾、先修條件、容量下限、跨期限冪等回播、個資遮罩、缺金鑰與 provider timeout、租約過期與舊 claim token 防護、三次寄送上限、草稿公開流程、Decimal 學分合計，以及不合法複合外鍵的拒絕。

SMTP 診斷修正另增加六個協定測試，以可控的 SMTP 替身驗證：Mailpit 無帳密寄送、有帳密時的 AUTH 不支援、STARTTLS 不支援時不降級、TLS 後重新 EHLO 並確認 AUTH、TLS 後缺少 AUTH，以及日誌不包含帳號、收件者或密碼。這些測試沒有寄送外部郵件；真實 Gmail 寄送仍需在部署容器使用實際設定驗證。

助理診斷修正另增加十一個測試，涵蓋未設定或空白金鑰、LangChain 包裝的 Google 400 / 401 / 403 / 404 / 429 / 500 / 504 錯誤、HTTP 逾時，以及錯誤回應與日誌不洩漏供應商原文或金鑰。另攔截真正的 LangChain → Google SDK 呼叫，確認 `gemini-3.5-flash-lite` 的結構化輸出設定、已移除舊 SDK 的取樣及 `candidate_count` 預設值，並將模擬回應交給實際解析器及 LangGraph 完成規則查詢。驗收 CLI 也經過唯讀程式目錄測試，確認紀錄寫入可寫的暫存目錄。這些測試沒有呼叫外部 Gemini，真實金鑰、模型權限及額度仍需在部署環境驗證。

瀏覽器測試使用測試 stub 代替 Gemini 意圖判斷，後續課程與個人查詢仍走真實工具及資料庫。[桌面截圖](desktop.png)、[手機截圖](mobile.png)、[正式課表截圖](timetable.png)。

SMTP 使用 Mailpit v1.27.8 的獨立本機執行檔，非假 SMTP stub。以學生 API 先完成加選，再讓 Worker 實際連到拒絕連線的埠，檢查 PENDING 與 attempt=1，將排程提前進行驗收，恢復 SMTP 後 attempt=2、SENT，Mailpit 可查到郵件；之後退選還原展示課表。[SMTP 記錄](mailpit-verification.json)。

效能測試機器為 Linux x86_64、4 logical CPUs、約 3.77 GiB RAM。API 透過真實本機 HTTP，使用 20 執行緒及連線重用；同學期 1,000 班，回傳每頁 20 筆。p50 187.97 ms、p95 269.96 ms、最大 347.50 ms。[完整環境與數據](performance.json)、[兩筆 SQL 與 EXPLAIN (ANALYZE, BUFFERS)](explain.txt)。這是本機暖機結果，不代表其他部署環境的效能保證。

## 尚待外部環境驗證

新增的 `deploy.sh` 已通過 Bash 語法檢查、Compose 設定檢查及 **8 項模擬 Docker CLI 流程測試**，包含首次部署、重複 migration、既有環境檔保留、不執行環境檔內的 Shell 文字、自訂含空白路徑、資料庫故障、migration 故障、HTTP 檢查失敗及 daemon 權限不足。實際執行腳本會在 Docker 權限檢查階段停止，沒有改動現有服務；這些流程測試不等同實際容器部署成功。

- **真實 Gemini 呼叫**：本次沒有提供金鑰。`docs/gemini-verification.json` 明確記為未執行。可設定金鑰後執行 `python -m app.verify_gemini`，保存實際回應。
- **AT-13 完整 Compose 首次啟動與重啟持久性**：目前帳號連接 `/var/run/docker.sock` 被拒絕，無法在此環境執行容器建置與 Compose 啟動。已驗證 Compose 語法、本機正式建置、全新 PostgreSQL migration、可重複 seed 及核心服務流程；volume 設定與容器首次執行仍需在有 Docker 權限的主機驗收。

## 驗收對照

| SRS | 狀態與範圍 |
|---|---|
| AT-01～AT-03 | PostgreSQL API 測試通過；期限測試固定時間，開始時刻受理、截止時刻拒絕 |
| AT-04 | 預期 SHA-256 勝出者、額滿理由與既有批次回播通過；未另外啟動第二個相同環境 |
| AT-05～AT-09 | 故障回滾、20 人併發、學生序列化、先修、釋位、容量與冪等性通過 |
| AT-10 | 同一 LangGraph 的測試意圖、真實唯讀 SQL / 個人服務、操作入口及他人資料拒絕通過 |
| AT-11 | 無金鑰及模擬 provider timeout 通過；真實模型呼叫待驗證 |
| AT-12 | 自動租約 / 重試測試及真實 Mailpit 故障恢復煙霧測試通過 |
| AT-13 | 全新資料庫 migration 與 seed 通過；完整容器啟動與重啟待驗證 |

測試執行存在 Starlette / AnyIO 型別別名及 LangGraph 序列化預設值的上游棄用提示，不影響結果。P1 功能不在本次交付範圍。
