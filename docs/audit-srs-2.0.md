# 選課系統 2.0 需求對照與程式審查

這份報告保留修正前的審查基準。新版實作與回歸測試結果見[驗證報告](verification.md)。

2026-11-22，Asia/Taipei。審查基準為提交 `b7f6f00`、[2.0 設計稿](design-srs-2.0.md) 與 [需求說明書 2.0](../Course_Registration_SRS.md)。審查時，核心選課流程通過例行測試，另查到帳號切換、區網登入及數個選課周邊問題。

## 新版需求對照

| 需求 | 2.0 設計稿 | 目前程式 |
|---|---|---|
| FR-01–08、§4、§7.2 登入、查課、初選分發、加退選、管理與通知 | 沿用現有資料、路由與交易規則，批次結果接回原流程。 | 核心流程已有實作。PostgreSQL、Chromium 與 Mailpit 驗證通過，仍有下列個別缺陷。 |
| FR-09、§7.1、AT-10–11 選課助理 | 保留既有查詢模式，使用 `langchain-google-genai` 與結構化輸出。 | LangGraph 與受限 SQL 已存在。Gemini 由 LangChain `RunnableLambda` 包裝 HTTPX 直接呼叫，與指定套件有差距。真實 Gemini 查課與推薦驗收待做。 |
| FR-12、§5、§7.3、AT-14 推薦 | 涵蓋同義詞、6／3／1 分數、穩定排序與前 20 筆、真實證據、學習延伸和 LLM 五班示範。 | `/chat` 只有規則、查課、個人查詢與澄清等原有路徑。缺研究方向推薦、`topics`、`course_topics`、公開主題 View、`DEMO-501`–`DEMO-505` 種子及推薦卡。 |
| FR-13、§6、§7.4、AT-15–18 試排與批次確認 | 涵蓋 1–5 班、15 分鐘或窗口截止、由本人確認、狀態與名額重查、整批回滾及冪等重播。 | 執行中的 FastAPI OpenAPI 無 `selection-plans` 確認路由。草案表、試排畫面、多班交易及新版 migration 仍待實作。現有冪等表只允許 201、204。 |
| FR-10、FR-11 收藏、候補等 | 依需求列為 P1。 | 可在 P0 之後實作。 |
| AT-13、AT-19 Compose 與交付 | 安排本機全新部署測試及後續遠端驗收。 | Compose 設定可解析，完整 Docker 冷啟與重啟待驗。GitHub 上傳及 AT-19 依本次範圍暫緩。 |

設計稿有一處需寫清楚。SRS §7.3 第 3 點要求可行門數不足時仍回傳組合與原因。目前設計已有各班 `reason_code`，但沒有明訂有草案時要如何呈現不足門數。可在回答或試排摘要顯示「目標 3 門，目前可排 2 門」，並列主要限制。現有欄位可支援這項顯示。

## 現行程式問題

優先修復會影響個人資料隔離、登入或部署的問題。下表區分實際重現、受控測試和程式路徑確認。

| 優先 | 問題及證據 | 修正方向 |
|---|---|---|
| 高 | **切換帳號時可能顯示前一位學生的助理回答。** Chromium 以虛構帳號及延遲回應重現。登出後切換學生，舊請求完成時仍寫入目前的對話，可能顯示前一位學生的課表。 [App.tsx 對話請求](../frontend/src/App.tsx#L572) · [帳號或學期切換](../frontend/src/App.tsx#L325) | 依登入身分及學期取消請求或忽略過期回應。驗收登出、切換學生、切換學期及請求完成順序。 |
| 高 | **以 VM 或區網 IP 的 HTTP 入口登入會卡住。** Chromium 以非 localhost 的 HTTP 位址重現。`crypto.randomUUID()` 不可用，登入在送出 API 之前就失敗，而且呼叫位於 `try/finally` 外，忙碌狀態留在畫面。 [App.tsx](../frontend/src/App.tsx#L428) · [瀏覽器 API 限制](https://developer.mozilla.org/en-US/docs/Web/API/Crypto/randomUUID) | 只為需要冪等的操作產生鍵。使用支援目標環境的安全隨機數，並保證錯誤時會清理操作狀態。 |
| 高 | **自訂 PostgreSQL 密碼可能阻止服務啟動。** 以合成密碼驗證 SQLAlchemy 解析，`@` 可使主機欄位解析錯誤。README 建議預編碼，但角色會使用編碼後的字面值，連線端會解碼成另一個密碼。 [compose.yaml](../compose.yaml#L5) · [init-db.sh](../scripts/init-db.sh#L3) · [README](../README.md#L56) · [SQLAlchemy URL 說明](https://docs.sqlalchemy.org/en/20/core/engines.html#escaping-special-characters-such-as-signs-in-passwords) | 保留原始角色密碼，在建立連線時用 `URL.create()` 等方式獨立組裝 URL，涵蓋應用、migration 與公開帳號。 |
| 中 | **通知可能開到另一學期。** 實際測試的加選 Outbox 連結只有 `/timetable`。分發通知也只帶 `/applications`，沒有學期。前端因此採用瀏覽器已選學期或預設學期。 [通知事件](../backend/app/registration.py#L295) · [分發事件](../backend/app/registration.py#L340) · [前端選擇學期](../frontend/src/App.tsx#L277) | 用事件的 `term_id` 產生連結，舊的 Outbox 事件寄送時也可補上學期。 |
| 中 | **助理的星期篩選可回傳錯誤課表。** 受控測試給模型計畫 `weekday=3`，詢問「我週三有課嗎」仍得到週一和週三課堂。執行端重新比對「星期」，沒有使用已解析的星期。 [chat.py](../backend/app/chat.py#L259) | 使用 `Plan.weekday`，並測「週三」「星期三」及不指定日期。 |
| 中 | **管理員點草稿班別會得到 404。** 在隔離資料庫用真實 API 驗證，管理清單回 200，前端使用的公開詳情回 `404 TERM_NOT_FOUND`。 [班別詳情請求](../frontend/src/App.tsx#L413) · [公開路由](../backend/app/main.py#L145) | 草稿班別用授權的管理詳情，或直接使用管理清單已回傳的班別資料。 |
| 中 | **SMTP 已接受後，連線結束錯誤可能導致重寄。** 用 `smtplib` 的實際退出邏輯和虛構 SMTP 回應重現。`send_message()` 已成功，`QUIT` 回 `421` 時仍排程重寄。 [worker.py](../backend/app/worker.py#L73) | 分開判定郵件接受與連線清理結果。保留接受結果，記錄清理錯誤。 |
| 中 | **Gemini 驗收工具可能誤報查課成功。** 受控測試讓查課例題回一般規則，工具仍寫入 `executed` 並印出成功。 [verify_gemini.py](../backend/app/verify_gemini.py#L34) | 驗證是課程查詢模式，班別和名額應可核對 PostgreSQL。2.0 另外驗證推薦模式和候選。 |
| 中 | **班別詳情可能套用前一班的資格結果。** 程式路徑確認。班別 A 的資格查詢尚未回來時打開班別 B，A 的回應仍可寫進共用 `check`。後端確認選課時會重新檢查。 [App.tsx](../frontend/src/App.tsx#L1413) | 請求與目前班別、學期和登入身分綁定，畫面切換時撤銷舊結果。 |
| 低 | **分發發布瞬間可回傳矛盾的階段。** 受控測試模擬兩次查分發紀錄中間發生提交，回應同時出現 `ADD_DROP` 與空的 `published_at`、`allocation`。 [registration.py](../backend/app/registration.py#L65) | 同一份分發紀錄用於階段、公布時間及摘要。 |
| 低 | **Alembic 離線 SQL 匯出失敗。** 實際執行 `alembic upgrade head --sql` 得到 `MockConnection` 缺少 `exec_driver_sql`。連線執行 migration 通過。 [0001_initial.py](../backend/alembic/versions/0001_initial.py#L10) | 讓離線支線用 Alembic 支援的 SQL 輸出方式，線上行為維持現有合約。 |

另有一項首次部署風險待容器驗證。Compose 的 `pg_isready` 使用 Unix socket，而 PostgreSQL 官方映像在初始化腳本期間會先啟動只聽 Unix socket 的暫時程序。若 `db` 此時被判為健康，使用 TCP 的 `migrate` 可能提早啟動。建議將健康檢查改為 TCP，並在新資料卷演練。[Compose 健康檢查](../compose.yaml#L33) · [PostgreSQL 官方映像初始化說明](https://hub.docker.com/_/postgres#initialization-scripts)

## 實測結果

| 檢查 | 結果 |
|---|---|
| 後端 pytest | 89 通過，2 則上游棄用提示。以全新 PostgreSQL 16.15 測試庫跑 migration、授權、選課與分發交易、競爭名額、冪等、助理與通知。 |
| 前端建置與 Chromium | TypeScript 和 Vite 建置通過，3 個端到端流程通過，含公開查課、學生選課、管理員容量與行動版。助理使用測試 stub。 |
| 部署 | 8 個模擬 Docker CLI 測試通過。Shell 語法與 `.env.example` 的 Compose 解析通過。本機 Docker socket 權限不足，容器部署未實測。 |
| SMTP | 隔離的 Mailpit 實測。第一次 TCP 連線失敗後，選課保留，第二次嘗試成功，Outbox 為 `SENT`。 |
| 查課效能 | 1,000 個班別、20 並行、25 筆暖機、200 筆請求。4 個邏輯 CPU、約 3.77 GiB RAM，p50 188.35 ms，p95 256.35 ms，達 ≤ 500 ms 目標。 |

真實 Gemini 查課與推薦、完整 Docker 冷啟和重啟、尚未實作的 AT-14–18 都仍待驗收。審查使用隔離的虛構資料，不涉及既有資料卷或讀取部署金鑰。這次交付審查報告，未變更應用程式或上傳 GitHub。
