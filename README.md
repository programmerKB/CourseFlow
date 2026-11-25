# CourseFlow 大學選課與課程推薦系統

**版本 3.0.1**。CourseFlow 提供公開查課、初選登記、分發、週課表與教務管理。學生可在網頁或用英文對話加選、退選（LangGraph + Llama 3.2 3B），每學期最多 18 學分且不得衝堂，正式選課後以 Gmail 寄出確認信（預設用應用程式密碼，可選 Gmail API）。授課教師可查本班名單並登錄成績。學生也可用中文文字或語音查課，依 16 種研究方向試排並確認選課。所有語言模型與語音辨識都在本機執行：文字問答用 Llama 3.2 3B，語音用 faster-whisper，不使用雲端模型。115-1 課庫涵蓋臺大、臺師大與臺科大電資參考課程。

介面使用繁體中文，時間以臺北時間顯示。示範班別的時間、容量、學分及教師均為虛構資料。系統使用 React、FastAPI、PostgreSQL、LangGraph、Llama 3.2 3B、faster-whisper、Gmail（SMTP 或 Gmail API）與 Docker Compose。 [需求說明書 4.0](Course_Registration_SRS.md)

[快速部署](#docker-compose-啟動) · [教師規格](#教師規格srs-40) · [部署設定](#部署設定) · [區網與語音](#區網登入與語音) · [本機模型](#本機模型中文查詢與語音) · [選課與課表](#115-1-選課與課表) · [驗證與開發](#自動測試)

![115-1 待分發課表與寄送課表按鈕](docs/timetable.png)

### v3.0.1 主要變更

- **教師規格（SRS 4.0）**：英文自然語言加選、退選（LangGraph + Llama 3.2 3B）；網頁與助理共用衝堂與每學期 18 學分檢查；正式選課後以 Gmail 寄確認信（預設用 Gmail 應用程式密碼經 SMTP 寄出，課表、初選與分發通知相同；可選 Gmail API）；`INSTRUCTOR` 角色可查本班名單並登錄、修改成績。資料庫升級為 migration `0006`。
- **移除 Gemini，全部改用本機模型**：中文查詢計畫改由 Llama 3.2 3B 產生並逐欄核對；語音改在 API 容器內以 faster-whisper 轉寫。不再需要 `GEMINI_*` 設定或任何雲端金鑰。
- **可查證的模型呼叫**：每則助理回覆、API 回應與日誌都附模型伺服器回報的 token 數與耗時。
- **Gmail 寄信**：選課確認信、課表、初選登記確認與分發結果都用同一種方式寄出，預設為 Gmail 應用程式密碼（SMTP），可改用 Gmail API（`GMAIL_TRANSPORT=api`）。`verify-email` 檢查設定並寄測試信，`retry-failed` 重寄失敗的信。

## Docker Compose 啟動

### 安裝 Docker

在 Linux 主機或 VM 安裝 Docker Engine 與 Docker Compose plugin。

- [Docker Engine 官方下載與安裝](https://docs.docker.com/engine/install/)
- [Docker Engine 的 Ubuntu 安裝步驟](https://docs.docker.com/engine/install/ubuntu/) · [手動下載 Ubuntu 套件](https://docs.docker.com/engine/install/ubuntu/#install-from-a-package)
- [Linux Compose plugin 安裝方式](https://docs.docker.com/compose/install/linux/)

需要 Git、Bash 和 Docker daemon 使用權限。以下範例以需要 `sudo` 的 VM 為例。

```bash
sudo docker version
sudo docker compose version
```

### 首次部署

```bash
git clone --branch v3.0.1 https://github.com/programmerKB/CourseFlow.git
cd CourseFlow
sudo ./deploy.sh
```

[deploy.sh](deploy.sh) 會從範本建立缺少的 `.env`、建置映像檔、套用資料庫 migration、初始化示範資料，再啟動 API、前端及通知 Worker。健康檢查通過後會列出入口。預設公開 **115-1**，初選開放至初始化後第 7 天。

| 服務 | 預設入口 |
|---|---|
| 選課系統 | [localhost:8080](http://localhost:8080) |
| 選課助理 | [localhost:8080/chat](http://localhost:8080/chat) |
| API 文件 | [localhost:8000/docs](http://localhost:8000/docs) |
| Mailpit 預覽信箱（未設定 Gmail 帳號時） | [localhost:8025](http://localhost:8025) |
| API 健康檢查 | [存活](http://localhost:8000/health/live) · [資料庫就緒](http://localhost:8000/health/ready) |

### 更新既有部署

在既有專案目錄取得版本標籤，再使用原有的 `.env` 和資料庫 volume。

```bash
git fetch origin --tags
git switch --detach v3.0.1
sudo ./deploy.sh --skip-seed
```

從 v2.1.0 升級到 v3.0.1 時另需：

1. **編輯 `.env`**（可對照 [.env.example](.env.example)）：刪除 `GEMINI_MODEL`、`GEMINI_AUDIO_MODEL`、`GEMINI_API_KEY`；設定 `LLAMA_BASE_URL`（中文與英文助理都需要，Windows 主機的 Ollama 見[教師規格](#使用-windows-本機的-ollamavm-透過-vmware-nat-連線)）；設定 Gmail 寄信（`GMAIL_TRANSPORT=smtp`、`SMTP_*` 與應用程式密碼，見[寄信設定](#gmail-寄信選課確認信與所有通知)）；舊的 `GMAIL_TRANSPORT=api` 若不使用 Gmail API 請改成 `smtp`。
2. **重新建置映像**：不要加 `--no-build`。建置時會下載 Whisper 語音模型（`base` 約 145 MB），之後容器不需連外。
3. **資料庫**：migration `0006` 自動套用。個人學分上限大於 18 的學期會改為 18 並留稽核紀錄，原有選課保留；`sudo docker compose exec api python -m app.manage credit-overloads` 可列出已選超過 18 學分、需要輔導退選的學生。
4. **驗證模型**：`sudo docker compose exec api python -m app.verify_llama` 與 `python -m app.verify_assistant`。

更新時會暫停應用服務，套用 migration 後重新啟動。種子程式會補齊三校班別，將舊展示學期的班別補入 115-1，並保留原有班別、登記、選課、分發及先修紀錄。已完成初始化、只需套用程式或環境設定時，可用 `--skip-seed`。

| 參數 | 用途 |
|---|---|
| `--skip-seed` | 套用程式、migration 與環境設定 |
| `--https` | 啟用區網 HTTPS，先驗證憑證與代理設定 |
| `--no-build` | 沿用已建置的映像檔 |
| `--scenario initial\|awaiting\|add-drop\|short` | 首次初始化的選課情境 |
| `--env-file 路徑` | 指定環境檔，相對路徑以執行指令的目錄為準 |
| `--timeout 秒數` | 每個服務與 HTTP 檢查的等待上限，預設 180 秒 |

## 教師規格（SRS 4.0）

3.0.1 依教師提供的 `Database.pdf` 第 1–4 節補齊下列功能。Web 與英文助理呼叫同一個 FastAPI 交易服務，寫入同一個 PostgreSQL。

| 教師項目 | 實作 | 驗收測試 |
|---|---|---|
| 英文自然語言加選、退選（T-01、T-09–T-11） | LangGraph 流程 `english_registration`：Llama 3.2 3B 解析意圖，後端檢查明確性後，經指定工具 `add_course`／`drop_course` 寫入 | `test_at37_*`、`test_at38_*` |
| Web 加選、退選（T-02） | 課程查詢、我的課表 | `test_at39_*`、`e2e.spec.ts` |
| Gmail 選課確認信（T-03） | 正式加選交易內建立 `REGISTRATION_CONFIRMATION`，Worker 預設以 Gmail SMTP（應用程式密碼）寄出；`GMAIL_TRANSPORT=api` 時改呼叫 Gmail API `users.messages.send`。規格寫的是 Gmail API，見下節說明 | `test_at40_*`、`test_at41_*` |
| 衝堂、每學期 18 學分（T-04、T-05） | `registration.evaluate_admission` 的上限為 `min(個人上限, 18)`；`student_terms` 另有 `CHECK (credit_limit BETWEEN 0 AND 18)` | `test_at39_*` |
| 教師名單、成績（T-06、T-07、T-12） | `INSTRUCTOR` 角色、「我的授課」頁、`grades` 表與成績稽核 | `test_at42_*`、`test_at43_*` |
| PostgreSQL、資料一致（T-08、T-13） | migration `0006` 從 `0005` 升級並保留原 ID | `test_at44_*`、`test_at45_*` |

### 英文選課助理（LangGraph + Llama 3.2 3B）

在選課助理輸入英文即進入英文流程。只提到研究方向而沒有要加退選的英文訊息（例如 “My research topic is Edge AI, 2 courses only”）改走研究路徑流程，見[本機模型：中文查詢與語音](#本機模型中文查詢與語音)；兩者使用同一個 Llama 端點。例句：

```text
Please enroll me in CS101, section A.      I'd like to add CS101 A.
Please drop CS101, section A.              Remove CS101 A from my schedule.
Can I take CS101?                          Show my timetable.
```

只有「本人明確要求一門課」才會寫入。是否為明確要求由 Llama 判斷（`explicit_request`，口語如 “yo can u sign me up for CS101 A pls” 也算），常見的祈使句另由規則辨識；問句改做資格試查，否定句（Don't drop…）只回覆目前課表，條件句、引述、一次兩門、替他人操作或課名不明時請學生補充，這些情況即使模型判斷為要求也不寫入。模型輸出的課號必須出現在學生原句中；學生身分一律取自登入 Session。同名或多班時列出候選班別，學生點選後後端在鎖內重新核對。模型逾時或無法使用時回 `503 CHAT_UNAVAILABLE` 與網頁加退選入口，選課資料不變。

使用內附的 Ollama 服務（首次下載約 2 GB；CPU 推論每次約數秒到數十秒）：

```bash
sudo ./deploy.sh --llm --skip-seed
sudo docker compose exec api python -m app.verify_llama
```

也可改連遠端 OpenAI 相容端點，例如部署 `meta-llama/Llama-3.2-3B-Instruct` 的 vLLM：

```dotenv
LLAMA_BASE_URL=https://llm.example.edu
LLAMA_API_STYLE=openai
LLAMA_MODEL=meta-llama/Llama-3.2-3B-Instruct
LLAMA_API_KEY=<token>
```

#### 使用 Windows 本機的 Ollama（VM 透過 VMware NAT 連線）

VM 不跑模型，改連 Windows 上的 Ollama。VMware NAT 下，VM 的預設閘道是 `x.x.x.2`，Windows 本機在同一網段的位址是「VMware Network Adapter VMnet8」，通常為 `x.x.x.1`。例如 VM 為 `192.168.169.128` 時，網址是 `http://192.168.169.1:11434`。

1. **Windows**：安裝 [Ollama for Windows](https://ollama.com/download/windows)。把專案的 `scripts/windows/setup-ollama.ps1` 複製到 Windows，以「系統管理員身分」開啟 PowerShell 執行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup-ollama.ps1 -VmSubnet 192.168.169.0/24
   ```

   腳本會設定 `OLLAMA_HOST=0.0.0.0:11434` 並重新啟動 Ollama、新增只允許 VM 網段連入 TCP 11434 的防火牆規則、執行 `ollama pull llama3.2:3b`，最後印出要貼到 VM 的 `LLAMA_BASE_URL`。Ollama 本身沒有登入機制，所以防火牆只開放給 VM 網段。也可手動完成：在 Windows 環境變數新增 `OLLAMA_HOST=0.0.0.0:11434`、從系統匣結束並重開 Ollama、開放防火牆 TCP 11434，再執行 `ollama pull llama3.2:3b`。

2. **VM**：先測試連線與推論，不需 Docker：

   ```bash
   ./scripts/check-ollama.sh http://192.168.169.1:11434
   ```

   連線逾時表示位址錯誤或防火牆未開；連線被拒表示 Ollama 只監聽 `127.0.0.1` 或尚未啟動；缺少模型時在 Windows 執行 `ollama pull llama3.2:3b`。

3. **把網址貼進 `.env`**：編輯專案根目錄的 `.env`（由 `sudo ./deploy.sh` 建立、只有 root 可讀，需用 `sudo nano .env`），新增或修改：

   ```dotenv
   LLAMA_BASE_URL=http://192.168.169.1:11434
   LLAMA_API_STYLE=ollama
   LLAMA_MODEL=llama3.2:3b
   ```

   套用設定（不要加 `--llm`，以免在 VM 另外啟動 Ollama）：

   ```bash
   sudo ./deploy.sh --skip-seed --no-build
   sudo docker compose exec api python -m app.verify_llama
   ```

   部署最後會檢查 Llama 服務；連不到時只顯示警告與修正提示，網頁加退選照常運作。不使用 Docker 的本機開發可直接帶環境變數：`LLAMA_BASE_URL=http://192.168.169.1:11434 ./scripts/run-local.sh`。

#### 確認助理真的呼叫 Llama，而非固定規則

- **畫面**：每則助理回覆（英文加退選、中文查課與研究路徑，含被拒絕的加選）下方顯示「LangGraph · llama3.2:3b 伺服器回報 798+41 tokens · 468 ms · 工具」。token 數與耗時由 Ollama 回傳，程式無法自行產生；若顯示「固定測試規則（CHAT_STUB），未呼叫 LLM」，代表 `.env` 的 `CHAT_STUB=true`；「完整句型由本機規則判讀，未呼叫 LLM」表示問題符合固定句型（例如「找星期二還有空位的電子系課」），直接查資料庫。
- **API 回應**：`agent.llm` 含 `server_model`、`prompt_tokens`、`output_tokens`、`total_ms` 與模型輸出；`agent.tools` 第一個工具為英文的 `llama.parse_intent` 或中文的 `llama.plan_query`（測試替身為 `stub.*`，固定句型為 `rules.plan_query`）。
- **API 日誌**：`sudo docker compose logs api | grep -E "english_registration|course_assistant"` 每筆都有 `"llm": {"server_model": ...}`，不含學生訊息內容。`CHAT_STUB=true` 時 API 啟動會先印出警告。
- **Windows 端**：`%LOCALAPPDATA%\Ollama\server.log` 會出現來自 VM（例如 `192.168.169.128`）的 `POST "/api/chat"`。
- **指令**：`sudo docker compose exec api python -m app.verify_llama` 會顯示 `chat_stub`、伺服器版本、已安裝模型，以及每個探測句由伺服器回報的 token 數與耗時。

模型無法使用時助理回 `503 CHAT_UNAVAILABLE`，不會改用固定規則回答；網頁查課與我的課表照常可用。已部署的服務需重新建置才會帶有上述證據欄位：`sudo ./deploy.sh --skip-seed`。

Windows 重新開機或 VMware 重新配置網路後，VMnet8 位址可能改變，可用 `ipconfig` 核對後更新 `.env`。CPU 推論時第一次請求要載入模型，可能接近 `LLAMA_TIMEOUT_SECONDS`（預設 45 秒）；Ollama 會保留模型 30 分鐘，之後的請求較快。

`verify_llama` 會用助理的實際提示詞送出 6 個探測句，列出模型名稱、是否為 Llama 3.2 3B、延遲與解析結果。每次英文請求都會在 API 日誌留下一筆 JSON，含 `graph`、`model`、`intent`、`tools` 與 `elapsed_ms`，不記錄訊息內容。回應的 `agent` 欄位同樣列出模型與呼叫的工具，畫面以「LangGraph · 模型 · 工具」顯示。

### Gmail 寄信（選課確認信與所有通知）

系統寄出的每一封信（選課確認信、「寄送課表」、初選登記確認與分發結果）都使用同一個寄信方式。預設是 **Gmail SMTP + 應用程式密碼**（`GMAIL_TRANSPORT=smtp`）。正式加選成功（網頁、英文或中文助理單筆加選、加退選草案確認、初選分發錄取）時，同一交易建立一筆確認信事件。退選、待分發登記、試查及失敗請求不寄確認信；同鍵重送沿用原事件。

> 老師規格（`Database.pdf`）的 Email Service 寫的是 **Gmail API**。本專案為了簡化設定，預設改用 Gmail 應用程式密碼（SMTP）寄送；若驗收要求 Gmail API，改設 `GMAIL_TRANSPORT=api`，步驟見下方「改用 Gmail API」。

#### 設定 Gmail 應用程式密碼

1. 用寄件的 Gmail 帳號開啟[兩步驟驗證](https://myaccount.google.com/signinoptions/twosv)。
2. 到[應用程式密碼](https://myaccount.google.com/apppasswords)建立一組密碼（名稱可填 CourseFlow），複製顯示的 16 碼密碼；中間的空白可留可去，系統會自動移除。
3. 編輯專案根目錄的 `.env`（`sudo nano .env`）：

   ```ini
   GMAIL_TRANSPORT=smtp
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_STARTTLS=true
   SMTP_USER=you@gmail.com
   SMTP_PASSWORD=abcdefghijklmnop
   SMTP_FROM=CourseFlow <you@gmail.com>
   ```

   `SMTP_FROM` 可省略（預設為 `SMTP_USER`）；Gmail 只允許以登入帳號本身或已設定的別名寄出。
4. 套用設定並寄一封測試信，再把示範學生的信箱改成可收信的地址（種子資料預設為 `example.test`）：

   ```bash
   sudo ./deploy.sh --skip-seed --no-build
   sudo docker compose exec api python -m app.manage verify-email --send-to you@example.com
   sudo docker compose exec api python -m app.manage set-email --login student05 --email you@example.com
   ```

   部署最後的「檢查 Gmail 寄信」也會登入一次 Gmail 並列出缺少的設定。

沒有 Gmail 帳號時，把 `SMTP_HOST=mailpit`、`SMTP_PORT=1025`、`SMTP_STARTTLS=false` 並清空 `SMTP_USER`，信會進內附的 Mailpit 預覽信箱（[localhost:8025](http://localhost:8025)），不會寄到學生信箱。

#### 寄送狀態與重寄

「教務管理 → Email 通知狀態」顯示每封信的狀態、嘗試次數、錯誤代碼、處理提示與訊息 ID（`smtp:` 開頭為 SMTP 寄出）。連線類錯誤最多重試 3 次；帳密或設定錯誤直接記為 `FAILED`。錯誤欄只存固定代碼，不存伺服器回應內容。修正設定後把失敗的信重新排入寄送：

```bash
sudo docker compose exec api python -m app.manage retry-failed                       # 全部 FAILED
sudo docker compose exec api python -m app.manage retry-failed --error SMTP_AUTH_FAILED
```

| 錯誤代碼 | 原因與處理 |
|---|---|
| `SMTP_NOT_CONFIGURED` | `SMTP_HOST` 是 `smtp.gmail.com`，但 `SMTP_USER` 或 `SMTP_PASSWORD` 未填 |
| `SMTP_AUTH_FAILED` | Gmail 拒絕登入：密碼須是 16 碼應用程式密碼（不是 Gmail 登入密碼），`SMTP_USER` 須是完整 Gmail 地址 |
| `SMTP_UNREACHABLE` | 連不到伺服器：核對 `SMTP_HOST`、`SMTP_PORT` 與主機對外網路，Worker 會自動重試 |
| `SMTP_STARTTLS_UNSUPPORTED` | 伺服器不支援 STARTTLS：Gmail 使用 `smtp.gmail.com:587` 並設 `SMTP_STARTTLS=true` |
| `SMTP_AUTH_UNSUPPORTED` | 連到不支援登入的伺服器（例如 Mailpit）卻填了帳密 |

#### 改用 Gmail API（選用）

設 `GMAIL_TRANSPORT=api` 後改用 Gmail API 的 `users.messages.send`，需要 Google Cloud 的 OAuth 用戶端：

1. 在 Google Cloud Console 啟用 Gmail API，「目標對象」加入寄件帳號為測試使用者，建立「電腦版應用程式」OAuth 用戶端。
2. 執行 `python3 scripts/gmail-oauth.py --client-id <用戶端ID> --manual` 取得 refresh token。
3. 在 `.env` 設定 `GMAIL_TRANSPORT=api`、`GMAIL_CLIENT_ID`、`GMAIL_CLIENT_SECRET`、`GMAIL_REFRESH_TOKEN`、`GMAIL_SENDER`，再執行 `verify-email`。測試狀態的 OAuth 應用程式，refresh token 約 7 天失效，失效後重跑第 2 步。錯誤代碼為 `GMAIL_*`，處理提示會顯示在管理頁與 `verify-email` 輸出。

### 授課教師名單與成績

教師帳號 `teacher01`～`teacher04`（密碼 `Demo123!`）連結既有教師主檔。登入後只看到「課程查詢」、「我的授課」與「選課規則」。

1. 「我的授課」列出本人在所選學期的授課班別與正式選課人數。
2. 點選班別後顯示 `ENROLLED` 學生（依學號排序），已退選與待分發學生不列入。
3. 輸入 0–100、最多兩位小數的成績後按「儲存」。同一筆修課紀錄只有一筆目前成績，每次新增或修改都寫入稽核（教師帳號、時間、課號、修改前後分數）。

非本人授課班別或錯配的修課紀錄回 `404`，學生或管理員呼叫教師 API 回 `403`。

### 課堂 Demo（SRS §10.2）

使用隔離環境：`.env` 設 `PUBLIC_INITIAL_TERM_ID=`，首次部署 `sudo ./deploy.sh --scenario add-drop --llm`。種子讓 `student05` 在 115-1 已有 15 學分（CS201、CS301、EE101、EE301、EE302）。

1. `student05` 登入，在網頁或英文助理查看 15 學分課表。
2. 英文助理輸入 `Please enroll me in CS101, section A.`。CS101 A 與 CS201 A 首尾相接，加選後為 18 學分，回覆顯示確認信已排入 Gmail 寄送。
3. `I'd like to add CS303 A.` 回 `CREDIT_LIMIT_EXCEEDED`（21 學分）；`Please enroll me in CS302, section A.` 回 `TIME_CONFLICT` 並列出衝突班別。
4. `Please drop CS101, section A.` 後到網頁「我的課表」確認回到 15 學分。
5. 以 `teacher01` 登入「我的授課」，開啟 CS301 A，為 `B11500005` 登錄 82 分再改成 90 分。改用學生帳號呼叫同一個 API 會得到 `403`。
6. 對照 `enrollments`、`grades`、`notification_outbox`（`provider_message_id`）與 `audit_events` 的紀錄。

從 2.1.0 升級的資料庫可用 `python -m app.manage credit-overloads` 列出既有選課已超過 18 學分的學生學期，再由學生退選處理。

## 部署設定

首次部署可先從 [.env.example](.env.example) 建立 `.env`；已部署的主機可直接編輯原環境檔。變更後執行 `sudo ./deploy.sh --skip-seed`。

### 調整 115-1 初選日期

```dotenv
INITIAL_START_AT=2026-11-24T09:00:00+08:00
INITIAL_END_AT=2026-11-30T23:59:00+08:00
```

兩項要同時設定，並附上時區。部署時會先檢查再更新資料庫，保留原來的登記並留下稽核紀錄。兩項留空會採用資料庫時段。公布分發結果後，該次初選時段即鎖定。

### 連接埠與資料庫

| 變數 | 預設值 | 用途 |
|---|---:|---|
| `FRONTEND_PORT` | 8080 | 選課網站 HTTP |
| `HTTPS_PORT` | 8443 | 啟用 HTTPS 時的網站入口 |
| `API_PORT` | 8000 | 主機內的 API 與文件 |
| `MAILPIT_PORT` | 8025 | 主機內的 Mailpit 收信畫面 |

主機連接埠已被占用時，可調整相應的變數。若變更使用者實際開啟的網址，也請更新 `APP_ORIGIN`，讓 Email 連結指向正確入口。

PostgreSQL 使用 `course_db` volume，`docker compose down` 後資料仍會保留。初始化時由 `MIGRATOR_DB_PASSWORD`、`APP_DB_PASSWORD` 與 `PUBLIC_DB_PASSWORD` 建立資料庫角色。這三項填原始密碼即可。既有資料庫角色的密碼需在 PostgreSQL 更新。若自行設定完整的 `DATABASE_URL`，需對 URL 中的密碼進行編碼。

## 區網登入與語音

### 使用區網登入

在 VM 上可開 `http://localhost:8080`。同區網的裝置用 `http://<VM 的區網 IP>:8080`。頁面與 API 由同一個網址提供時，可直接登入。若使用自訂網域，以 `APP_ORIGIN` 或逗號分隔的 `APP_ALLOWED_ORIGINS` 設定完整的來源網址。

### 在區網啟用麥克風

瀏覽器將 `localhost` 視為受信任的本機來源。區網 IP 需要受信任的 HTTPS 才能使用網頁麥克風。 [瀏覽器安全情境規範](https://www.w3.org/TR/secure-contexts/#is-origin-trustworthy)

1. 在開瀏覽器的電腦以 mkcert 產生憑證，將範例 IP 換成 VM 的實際區網位址。 [mkcert 安裝說明](https://github.com/FiloSottile/mkcert#installation)

   ```bash
   mkcert -install
   mkcert -cert-file server.crt -key-file server.key localhost 127.0.0.1 192.168.1.20
   ```

2. 在 VM 將伺服器憑證和私鑰放到 `.tls/server.crt`、`.tls/server.key`，私鑰權限設成 `600`。其他裝置須信任簽發憑證的 CA。 [跨裝置安裝 CA](https://github.com/FiloSottile/mkcert#installing-the-ca-on-other-systems)
3. 已部署的專案可啟動 `compose.yaml` 的 HTTPS 服務。若同時要更新程式或 `.env`，改用 `sudo ./deploy.sh --skip-seed --https`。

   ```bash
   sudo docker compose --profile https up -d --no-deps https
   ```

4. 從本機開啟 `https://localhost:8443/chat`，從區網開啟 `https://192.168.1.20:8443/chat`。登入後按「開始錄音」並授權麥克風。

可在 `.env` 設定 `COMPOSE_PROFILES=https` 讓後續部署啟用同一個 HTTPS 服務。若要以 HTTPS 作為主要入口，`APP_ORIGIN` 填對應的完整網址，並設 `COOKIE_SECURE=true`，讓登入 Cookie 只經 HTTPS 傳送。

### 語音輸入與朗讀

按「開始錄音」後說出問題，再按「停止並送出」。20 秒到期會停止收音並等待送出或取消。

從區網 HTTP 也可以按「選擇音檔」，使用裝置的錄音程式錄下問題後上傳。支援 WebM、M4A、MP4、Ogg、WAV 和 MP3，每段最多 20 秒、5 MiB。選檔後可確認送出或取消。助理會顯示轉寫文字，支援朗讀的瀏覽器可按「朗讀回覆」。

初選期間可說「我要登記初選電路學」或「我要加選電路學」，再點辨識結果中的「登記此班」並確認。若先以語音查詢研究方向，可接著說「請試排這個方向的本期課程」，再確認試排中的班別。辨識有誤時，可點「編輯辨識文字」修正後送出。

送出的錄音由後端用 FFmpeg 轉成 16 kHz 單聲道，在 API 容器內以 faster-whisper 轉寫，錄音不會傳到外部服務。設定與模型大小見[本機模型：中文查詢與語音](#本機模型中文查詢與語音)。Compose 映像已安裝 FFmpeg。本機啟動須另安裝 FFmpeg 並讓 `ffmpeg` 可從 PATH 執行。[faster-whisper](https://github.com/SYSTRAN/faster-whisper)

## 115-1 選課與課表

1. 用學生帳號登入。在「課程查詢」選擇班別，點「登記初選」。也可以在「選課助理」詢問「我是電子系學生，給我修課建議」，選擇試排班別後一次確認。
2. 到「我的選課」或「我的課表」查看已登記的班別。課表會立即標示「待分發」，錄取後才計入正式學分。
3. 在初選開放期間點「取消登記」，該班別會從「我的選課」和課表移除。
4. 在「我的課表」點「寄送課表」。系統將當下的課表寄到本人帳號信箱。登記、取消與加退選只更新選課資料，分發公布時另寄結果摘要。

## 測試帳號與資料

| 帳號 | 密碼 | 用途 |
|---|---|---|
| `admin` | `Demo123!` | 學期、時段、班別、容量、分發與通知管理 |
| `student01`～`student10` | `Demo123!` | 資工系學生，已於歷史學期通過程式設計、資料結構 |
| `student11`～`student20` | `Demo123!` | 電子系學生，包含先修未通過案例 |
| `teacher01`～`teacher04` | `Demo123!` | 授課教師；teacher01 教 CS101 A、CS301 A、CS303 A、CS399 A，teacher02 教 CS101 B、CS201 A、CS302 A、CS304 A |

在 115-1，`student20` 學分上限為 3.0，其他示範學生為 18.0（系統上限）。`student19` 已選上容量 1 的軟體工程。`add-drop` 情境首次建立時，`student05` 另有 15 學分供教師規格展示。

「我是電子系學生，給我修課建議」會依電子工程系的公開班別和登入者目前的選課紀錄試排。「我剛剛選了什麼？」會列出本學期的待分發登記及正式選課。這兩種明確問法可由資料庫直接回答。

| 課程 | 測試用途 |
|---|---|
| CS101 A / B 程式設計 | 同課不同班；A 班有週一、週三兩個時段 |
| CS201 A 資料結構 | 先修 CS101；時間與 CS101 A 首尾相接 |
| CS301 A 資料庫設計 | 先修 CS201，兩個上課時段 |
| CS302 A 作業系統 | 與 CS101 A 衝堂 |
| CS303 A 計算機網路 | 容量 1，初始剩餘最後一席 |
| CS304 A 軟體工程 | 初始額滿，可由 student19 退選釋出名額 |
| EE201 A 電子學 | 先修 EE101 |
| CS399 A 人工智慧專題 | 公開可查，但未開放選課 |

Seed 使用固定 UUID5。重跑會補上缺少的班別並保留現有班別、選課、分發與稽核。`--scenario` 用於第一次建立 115-1 的選課時段。

## 初始化情境與展示流程

下列情境都使用 115-1。若要展示即時加退選，先在示範環境的 `.env` 設定 `PUBLIC_INITIAL_TERM_ID=`，再選擇首次部署的啟動方式。

```bash
sudo ./deploy.sh --scenario initial
sudo ./deploy.sh --scenario awaiting
sudo ./deploy.sh --scenario add-drop
sudo ./deploy.sh --scenario short
```

四個指令是不同選項，只需選一個。`initial` 開放初選。`awaiting` 為初選已截止、等待分發，預先登記兩人競爭最後一席。`add-drop` 開放即時加退選。`short` 在初始化後約 3 分鐘截止初選，接著由管理員公布分發，再開放加退選。

建議展示：

1. 訪客查詢資料庫設計，開啟詳情查看兩個時段與先修條件。
2. 學生在初選學期登記，到「我的選課」與「我的課表」查看待分發班別。取消登記後，該班別會從兩處移除。
3. 在 `awaiting` 或 `short` 主學期，管理員進入「教務管理」執行「分發並公布」，查看錄取與未錄取數；重送返回同一批次。
4. 加退選學期以 student19 退選 CS304，再以 student01 加選，檢查名額、總學分與正式課表。
5. 在選課助理詢問「找這學期星期二還有空位的電子系課」，點開來源核對班別。這個問法直接查詢公開課程資料。
6. 展示研究推薦、試排與確認流程。在「我的課表」點「寄送課表」，信會經 Gmail 寄到學生信箱（先以 `set-email` 改成可收信的地址）。管理頁可查詢寄送狀態與 Gmail 訊息 ID。

### 研究方向推薦與批次選課

1. 以 `student01` 登入 **115-1**。
2. 打開選課助理，輸入「我的研究方向是大語言模型，請推薦並幫我選三門。」或點選同一個範例。`LLM` 與「大語言模型」使用同一組主題與排序規則。
3. 推薦可涵蓋三校課程和 `DEMO-501` 至 `DEMO-505`。展開推薦可核對大綱、先修、全部時段與研究延伸。
4. 在初選期間按「確認初選登記」，查看待分發結果。加退選期間按「確認加選」，更新正式課表。若名額或課表已變動，可按「更新試排」。

測試時可使用 `CHAT_STUB=true`，讓固定意圖走同一條推薦與選課流程。實機驗收需設定 `LLAMA_BASE_URL` 與 `CHAT_STUB=false`。

### 三校課庫與單門加選

執行 `sudo ./deploy.sh` 後，可直接在 **115-1** 查詢 40 門三校參考課程及 41 個三校模擬班別。校別篩選可分別查看臺大、臺師大與臺科大。

1. 在課程查詢選擇「國立臺灣科技大學」、「電子工程系」與「計算機結構與記憶體系統」。班別詳情附「校方課程來源」。上課時段、名額、學分及教師是專題資料。
2. 在即時加退選期間，以學生帳號開啟選課助理，輸入「我要加選臺科大電子系的高等計算機結構」或「我要加選 NTUST-ET-001」。找到唯一班別且資格符合時，當次送出就會更新「我的課表」。
3. 省略校系時會列出同名課的候選班別。初選期間可點「登記此班」並確認，課表會顯示待分發。加退選期間可點「加選此班」。
4. 輸入「我想做 RF，這學期先修什麼？」查看基礎、進階和研究專題。可接著按「試排本期課程」，再確認要提交的班別。「IC 設計」和「NVM」會先請你選方向。

## 本機模型：中文查詢與語音

本版不使用 Gemini 或其他雲端模型。中文問答與英文加退選共用同一個 Llama 3.2 3B 端點（`LLAMA_BASE_URL`，設定方式見 [Windows 本機 Ollama](#教師規格srs-40)），語音在 API 容器內以 faster-whisper 轉寫。

### 中文查詢如何使用 Llama

1. 完整固定句型（例如「找星期二還有空位的電子系課」、「我是電子系學生 給我修課建議」）與「我要加選…」指令直接由本機規則處理，不呼叫模型。
2. 其餘問題（例如「我想做晶片設計，該修什麼課？」、「My research topic is Edge AI, 2 courses only, Wednesday」）交給 Llama 產生結構化查詢計畫：要用的工具（查課、課表、資格、研究推薦等）、課名、系所、星期、門數與時段。
3. 程式逐欄核對計畫：學生沒說出的系所、學校、星期、門數或時段一律刪除（3B 模型常自行補上「臺大」或範例中的星期）。查他人資料與代為退選仍由本機防護攔下。
4. 核對後的計畫交給 PostgreSQL 唯讀工具執行。研究路徑的基礎、進階與研究專題來自審核過的路徑資料，「這學期可優先考慮」依修課紀錄、先修與開課班別計算，模型只負責理解問題，不產生課程內容。

### 驗證

以下指令用真實模型測試 115-1 查課與研究推薦，並比對 PostgreSQL 中的公開班別。回應必須帶有模型伺服器回報的 token 數，否則判定未呼叫模型。

```bash
sudo docker compose exec api python -m app.verify_assistant
sudo docker compose exec api python -m app.verify_assistant --connection-only   # 只檢查模型服務
```

通過時 API 容器的 `/tmp/assistant-verification.json` 會記錄公開課程結果、模型輸出的計畫與伺服器證據，不含學生課表或確認草案：

```bash
sudo docker compose cp api:/tmp/assistant-verification.json docs/assistant-verification.json
```

| reason_code | 處理方式 |
|---|---|
| `LLAMA_NOT_CONFIGURED` | 在 `.env` 設定 `LLAMA_BASE_URL`，重新建立 `api` 容器；只執行 `restart api` 不會更新 Compose 環境變數 |
| `LLAMA_UNREACHABLE` | 位址、防火牆或 Ollama 只監聽 127.0.0.1；執行 `python -m app.verify_llama --server-only` 查看提示 |
| `LLAMA_TIMEOUT` | 第一次載入模型較慢，稍後重試或調高 `LLAMA_TIMEOUT_SECONDS` |
| `LLAMA_MODEL_UNAVAILABLE` | 在模型主機執行 `ollama pull llama3.2:3b` |
| `LLAMA_AUTH_FAILED`、`LLAMA_RATE_LIMITED`、`LLAMA_UNAVAILABLE` | 檢查 OpenAI 相容端點的金鑰或服務狀態 |
| `LLAMA_INVALID_OUTPUT` | 模型回傳的計畫無法解析；換個說法再試，若持續發生請確認模型是 `llama3.2:3b` |
| `CHAT_FAILED` | 以 request_id 對照 `sudo docker compose logs --tail 100 api` 查閱錯誤類別 |

### 語音辨識（faster-whisper）

建置映像時會下載 `WHISPER_MODEL` 並放進映像，執行時設定 `HF_HUB_OFFLINE=1`，容器不需連外。預設 `base`：VM（4 核、2 執行緒）實測 4–13 秒的錄音約 0.7–1.8 秒轉寫完成，API 程序約多用 450 MB 記憶體。`small` 較準確，但約需 1 GB 記憶體、速度慢一倍，記憶體 4 GB 以上再考慮。

```ini
WHISPER_MODEL=base        # 改成 small 後需重新建置：sudo ./deploy.sh --skip-seed
WHISPER_CPU_THREADS=2     # 0 由 CTranslate2 自行決定
WHISPER_PRELOAD=true      # API 啟動後先在背景載入模型
```

轉寫提示詞包含選課常用語（加選、退選、試排、課號與班別），讓課號維持「CS101 A 班」的寫法；偶爾出現的簡體字以 OpenCC 轉成臺灣繁體。平均信心過低、文字重複或只是重述提示詞時回 `422 VOICE_UNCLEAR`，不會送出加選。模型載入失敗時回 `503 VOICE_UNAVAILABLE`，API 日誌只記錄錯誤類別（`whisper_preload_failed` 或 `voice_unavailable`）。

## 自動測試

### 後端與部署

後端測試使用獨立的 PostgreSQL 資料庫，名稱須以 `_test` 結尾。執行時會清空該測試資料庫的應用資料，並用虛構資料建立案例。

Compose 已啟動後，以下指令會使用 migration 角色建立並測試 `courses_test`。

```bash
sudo docker compose run --rm migrate python run_tests.py
```

本機 PostgreSQL 開發環境：

```bash
DATABASE_URL=postgresql+psycopg://course_migrator@127.0.0.1:55432/courses \
PUBLIC_DATABASE_URL=postgresql+psycopg://course_public:course_public@127.0.0.1:55432/courses \
.venv/bin/python backend/run_tests.py
```

教師規格驗收（AT-37–AT-45）在 `backend/tests/test_teacher_spec.py`。英文流程使用 `CHAT_STUB=true` 的固定意圖解析，經過與 Llama 相同的 LangGraph 防護、工具與交易；Llama 與 Gmail 的 HTTP 格式、錯誤與重試以模擬傳輸驗證。實機 Llama 與 Gmail 驗收請依[教師規格](#教師規格srs-40)的 `verify_llama`、`verify-email` 步驟另外執行。

部署腳本可用 `python3 -m unittest discover -s scripts/tests -v` 驗證。測試會使用模擬 Docker CLI。

### 瀏覽器

跨學期的瀏覽器回歸會進行加退選、初選登記、課表寄送和容量維護。請使用獨立測試資料庫，以 `seed('add-drop', recommendation_demo=True, university_demo=True, include_demo_terms=True, teacher_demo=True)` 建立測試情境，設定 `PUBLIC_INITIAL_TERM_ID=`、`CHAT_STUB=true` 並將 SMTP 指向 Mailpit，再啟動前後端。

```bash
cd frontend
npm ci
npx playwright install chromium
E2E_BASE_URL=http://localhost:5173 npm run test:e2e
```

測試 Compose 前端時，將 `E2E_BASE_URL` 改為測試服務的網址。截圖存於 `frontend/test-results/`。前端執行 `npm run build` 時會檢查 TypeScript 型別。

## 本機開發（Linux、不依賴 Docker daemon）

需要 Python 3.12、PostgreSQL 16（含 `pg_config`、`initdb`、`pg_ctl`）及 Node.js 22。初始化腳本會在專案內建立 `.venv` 與 `.runtime`。

```bash
./scripts/setup-local.sh add-drop
./scripts/run-local.sh
```

本機入口為 http://localhost:5173 與 http://localhost:8000/docs；資料庫使用 loopback 55432，資料存於 `.runtime/pgdata`。本機資料庫是展示用 trust 認證，僅監聽 `127.0.0.1`；Compose 則使用各用途的密碼角色。Ctrl+C 停止腳本啟動的應用程序，PostgreSQL 資料保留。

若有 Mailpit 可執行檔，可將它放在 `.runtime/mailpit`，腳本會啟動 SMTP 1025 與 Web 8025，本機腳本固定把信寄到這個 Mailpit。寄送失敗時依 Outbox 規則重試。

模型位址等設定可放入專案根目錄的 `.env`。測試用文字分類可用 `CHAT_STUB=true ./scripts/run-local.sh`。語音端點在本機以 faster-whisper 轉寫，第一次使用會把 `WHISPER_MODEL`（base 約 145 MB）下載到 Hugging Face 快取；整合測試以替身模型驗證轉寫狀態與繁體轉換。

若目前帳號無法讀取 `.env`，本機程序會改用環境變數及預設值。Compose 仍由執行部署的帳號讀取自己的環境檔。

## API 與專案結構

全部業務路由為 `/api/v1`，請參閱 `/docs` 與 `/openapi.json`。學生身分來自 HttpOnly Session。錯誤回傳 `code`、`message`、`request_id` 與必要的 `details`。加退選、草案確認及寄送課表需要 `Idempotency-Key`。前端會停用提交中的操作，遇到結果不明時保留原鍵與班別集合。

| API | 用途 |
|---|---|
| `GET /api/v1/schools`、`GET /api/v1/topics` | 提供公開查課的校系與主題選項。班別列表接受 `school_code` 和 `topic_code`。 |
| `POST /api/v1/me/terms/{term_id}/enrollments` | 網頁加選。`201` 回傳選課、剩餘名額、總學分、學分上限與確認信 `notification_id`。 |
| `POST /api/v1/chat` | 傳送 `message` 與 `term_id`。英文訊息走 Llama 3.2 3B 流程，成功時 `mode` 為 `add_course` 或 `drop_course`，`action.status` 為 `ENROLLED` 或 `DROPPED`。加退選帶 `Idempotency-Key`。從候選選班時附原指令與 `section_id`（`choice_action` 指出加選、退選或初選登記）。已選定研究方向後可用 `track_code` 接續試排。 |
| `GET /api/v1/instructor/terms/{term_id}/sections` | 教師本人在該學期的授課班別與正式選課人數。 |
| `GET /api/v1/instructor/terms/{term_id}/sections/{section_id}/roster` | 本班 `ENROLLED` 學生的 `enrollment_id`、學號、姓名、`score` 與 `graded_at`。 |
| `PUT /api/v1/instructor/terms/{term_id}/sections/{section_id}/roster/{enrollment_id}/grade` | 以 `{"score": 90.5}` 新增或更新成績（0–100，兩位小數）。相同分數重送回原成績。 |
| `POST /api/v1/chat/voice` | 以 `multipart/form-data` 傳送 `audio`、`term_id` 並附 `Idempotency-Key`。可用 `track_code` 接續研究方向試排。回傳轉寫及與文字助理相同的結果。 |
| `POST /api/v1/me/terms/{term_id}/selection-plans/{plan_id}/confirm` | 只接受本人草案中的 1–5 班。`section_ids` 是確定送出的班別集合。任一班失敗時整批回滾，回應以 `details.section_id` 指出班別。 |
| `POST /api/v1/me/terms/{term_id}/timetable/email` | 將當下課表排入本人信箱的寄送佇列。`201` 回傳 `notification_id`。 |

```text
frontend/src/              React 介面、型別與 API client（Teaching.tsx 為教師名單與成績）
frontend/tests/            Playwright 端到端測試
backend/app/models.py      資料模型與關聯約束
backend/app/registration.py 共用規則、鎖定、分發、冪等性
backend/app/auth.py         Argon2id、隨機 Session、撤銷
backend/app/admin.py        草稿設定、公開、班別與容量
backend/app/chat.py         LangGraph、Llama 查詢計畫與欄位核對、公開唯讀 SQL 工具；英文訊息轉交 english_agent
backend/app/english_agent.py 英文加退選 LangGraph 流程、防護規則與指定工具
backend/app/llama.py        Llama 3.2 3B 用戶端（Ollama 或 OpenAI 相容端點），兩個助理共用
backend/app/voice.py        錄音驗證、FFmpeg 轉檔與 faster-whisper 本機轉寫
backend/app/instructor.py   教師名單、成績與稽核
backend/app/gmail.py        通知信內容與選用的 Gmail API（OAuth、users.messages.send）
backend/app/manage.py       verify-email、set-email、retry-failed、credit-overloads 維運指令
backend/app/recommendation.py 主題排行、資格試排、草案與來源證據
backend/app/worker.py       Outbox Worker：通知信經 Gmail SMTP（預設）或 Gmail API 寄出
backend/app/seed.py         固定 ID 的可重複種子資料
backend/alembic/            migration、固定初版 DDL 與公開 View
backend/tests/             PostgreSQL 整合、併發、Gmail 寄信與效能測試
scripts/                   初始化、本機開發、gmail-oauth.py、check-ollama.sh 及 windows/setup-ollama.ps1
compose.yaml               PostgreSQL、migration、API、前端、通知、HTTPS 及選用 Ollama
```

[架構、核心 ERD 與交易設計](docs/architecture.md)、[SQL 與 EXPLAIN ANALYZE BUFFERS](docs/explain.txt)、[效能數據](docs/performance.json)、[課程查詢畫面](docs/desktop.png)、[手機畫面](docs/mobile.png)。

本版驗收紀錄見 [v3.0.1 驗證報告](docs/verification-v3.0.1.md)。歷史驗收紀錄可參閱 [v2.0.0 驗證報告](docs/verification-v2.md) 與 [v1.0.0 驗證紀錄](docs/verification.md)。
