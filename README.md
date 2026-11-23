# CourseFlow 大學選課與課程推薦系統

`v2.0.0` 對應 [需求說明書 3.2](Course_Registration_SRS.md)，加入三校電資課庫、文字與語音直接加選，以及 16 條研究修課路徑。

CourseFlow 提供公開查課、初選登記、分發、週課表與教務管理。學生可用自然語言查詢課程或依研究方向試排，檢查班別後一次確認。基本課程、研究推薦範例與三校課庫都在 **115-1**，首次採預設設定部署時開啟初選。

介面使用繁體中文，日期與時間採 `Asia/Taipei`，儲存庫內的種子資料為虛構資料。系統使用 React、TypeScript、FastAPI、PostgreSQL、LangGraph、Gemini 與 Docker Compose。

![115-1 待分發課表與寄送課表按鈕](docs/timetable.png)

## Docker Compose 啟動

需要 Bash、Docker Engine、Docker Compose plugin 及使用 Docker daemon 的權限。下列命令適用於需要 `sudo` 的 VM。已具備 Docker 權限時可省略 `sudo`。

### 首次部署

```bash
git clone --branch v2.0.0 https://github.com/programmerKB/CourseFlow.git
cd CourseFlow
sudo ./deploy.sh
```

[deploy.sh](deploy.sh) 會從範本建立缺少的 `.env`，再建置映像檔、執行 migration、初始化示範資料並啟動前端與通知 Worker。健康檢查通過後會列出服務網址。

### 更新既有部署

在既有專案目錄執行以下指令，使用原本的 `.env` 和資料庫 volume。

```bash
sudo ./deploy.sh
```

```bash
sudo ./deploy.sh --scenario awaiting   # 首次初始化為待分發情境
sudo ./deploy.sh --no-build            # 使用既有映像檔
sudo ./deploy.sh --env-file .env.production --timeout 300
sudo ./deploy.sh --help
```

升級會把舊展示學期的班別補入 115-1，包含時段、名額與先修條件。已在 115-1 開設的同課同班以原資料為準。舊展示學期會歸檔，原學期的登記、選課與先修紀錄留在資料庫。部署腳本會補齊尚未初始化的三校課程。

首次沿用預設設定部署時，115-1 的初選開放至初始化後第 7 天。種子程式可將舊版沒有實際分發的示範紀錄轉為初選。

腳本保留既有 `.env` 與資料庫 volume。更新時會暫停前端、API 與 Worker，migration 成功後再啟動，因此有短暫服務中斷。migration 失敗時停止後續部署並顯示失敗階段。環境檔按 Docker Compose 格式讀取。

首次部署如需設定網域、連接埠或 Gemini 金鑰，可先 `cp .env.example .env` 並編輯，再執行腳本。`--env-file` 的相對路徑以執行指令時的目錄為準。腳本需要已安裝且可使用的 Docker，不會安裝 Docker 或自動使用 sudo。若已啟動本機開發服務，請先釋出 Compose 需要的 8000、8025 連接埠；腳本不會終止其他程序。

若部署出現 `address already in use`，代表主機連接埠已被其他服務占用。可以先停止同專案的本機開發版（在執行 `scripts/run-local.sh` 的終端按 Ctrl+C；另行啟動的 Mailpit 需個別停止），或在 `.env` 調整主機連接埠後重跑部署：

```dotenv
FRONTEND_PORT=8080
APP_ORIGIN=http://localhost:8080
API_PORT=8001
MAILPIT_PORT=8026
```

以上設定改用 API 文件 `http://localhost:8001/docs` 與 Mailpit `http://localhost:8026`；容器內 API、SMTP 連接埠不變。重試可用 `./deploy.sh --no-build`，使用剛才已完成的映像檔。以 sudo 首次建立的環境檔會保留 `0600`，並交還原使用者擁有；既有環境檔的權限不會自動更改。

部署腳本的獨立流程測試可用 `python3 -m unittest discover -s scripts/tests -v` 執行。這些測試以模擬 Docker CLI 驗證流程順序、環境檔保留、故障中止及參數處理，不會修改正在執行的容器。

### 服務入口

| 入口 | 網址 |
|---|---|
| 選課系統 | http://localhost:8080 |
| FastAPI / OpenAPI 文件 | http://localhost:8000/docs |
| Mailpit 收信畫面 | http://localhost:8025 |
| 存活 / 資料庫就緒 | http://localhost:8000/health/live / http://localhost:8000/health/ready |

啟動順序為 `db healthy → migrate 成功 → api / worker → frontend`。`migrate` 是正常結束的一次性服務。`db` 使用獨立 `course_db` volume，`docker compose down` 與後續啟動會保留資料。既有 volume 的密碼由初始化時決定，修改 `.env` 不會自動修改 PostgreSQL 角色密碼。

`.env.example` 的密碼為本機展示值。Compose 會把 `MIGRATOR_DB_PASSWORD`、`APP_DB_PASSWORD` 和 `PUBLIC_DB_PASSWORD` 的原始值分別傳給 PostgreSQL 與連線程式。即使包含 `@`、`/` 或 `%`，這三項也直接填原始密碼。若自行撰寫完整 `DATABASE_URL`，URL 裡的密碼仍依 SQLAlchemy 格式編碼。

### 從區網登入

把瀏覽器使用的完整來源網址填到 `APP_ORIGIN` 或 `APP_ALLOWED_ORIGINS`。如果選課與課表信件都會從區網開啟，可以在 `.env` 使用以下設定，將範例 IP 換成 VM 的區網 IP。

```dotenv
APP_ORIGIN=http://192.168.1.20:8080
APP_ALLOWED_ORIGINS=http://localhost:8080
COOKIE_SECURE=false
```

`APP_ORIGIN` 也是 Email 連結的來源網址，額外允許的來源以逗號分隔。若畫面顯示「請由同源網站送出請求」，請核對網址的協定、IP 或網域及連接埠。HTTPS 部署請設定 `COOKIE_SECURE=true`。變更後執行 `sudo ./deploy.sh --skip-seed`。

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

在 115-1，`student20` 學分上限為 3.0，其他示範學生為 25.0。`student19` 已選上容量 1 的軟體工程。

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
5. 設定 Gemini 後詢問「找這學期星期二還有空位的電子系課」，點開來源核對班別。
6. 展示研究推薦、試排與確認流程。在「我的課表」點「寄送課表」，再開啟 Mailpit 查看郵件。管理頁可查詢寄送狀態。

### 研究方向推薦與批次選課

1. 以 `student01` 登入 **115-1**。
2. 打開選課助理，輸入「我的研究方向是大語言模型，請推薦並幫我選三門。」或點選同一個範例。`LLM` 與「大語言模型」使用同一組主題與排序規則。
3. 推薦可涵蓋三校課程和 `DEMO-501` 至 `DEMO-505`。展開推薦可核對大綱、先修、全部時段與研究延伸。
4. 在初選期間按「確認初選登記」，查看待分發結果。加退選期間按「確認加選」，更新正式課表。若名額或課表已變動，可按「更新試排」。

測試時可使用 `CHAT_STUB=true`，讓固定意圖走同一條推薦與選課流程。實際 Gemini 驗收需設定金鑰與 `CHAT_STUB=false`。

### V2 三校課庫與單門加選

執行 `sudo ./deploy.sh` 後，可直接在 **115-1** 查詢 40 門三校參考課程及 41 個三校模擬班別。校別篩選可分別查看臺大、臺師大與臺科大。

1. 在課程查詢選擇「國立臺灣科技大學」、「電子工程系」與「計算機結構與記憶體系統」。班別詳情附「校方課程來源」。上課時段、名額、學分及教師是專題資料。
2. 在即時加退選期間，以學生帳號開啟選課助理，輸入「我要加選臺科大電子系的高等計算機結構」或「我要加選 NTUST-ET-001」。找到唯一班別且資格符合時，當次送出就會更新「我的課表」。
3. 省略校系時會列出同名課的候選班別，可點「加選此班」。初選階段可由錯誤訊息開啟班別進行初選登記。
4. 輸入「我想做 RF，這學期先修什麼？」查看基礎、進階和研究專題。可接著按「試排本期課程」，再確認要提交的班別。「IC 設計」和「NVM」會先請你選方向。

### 語音輸入與朗讀

使用 HTTPS 或本機的 `http://localhost:8080`。Chrome 等支援錄音的瀏覽器會請求麥克風權限。點「開始錄音」，說出問題或明確加選指令，再按「停止並送出」。20 秒到期時停止收音，畫面會等待送出或取消。助理會顯示轉寫文字，支援朗讀的瀏覽器可按「朗讀回覆」。

區網語音可在前端服務前使用 HTTPS 反向代理，將 `APP_ORIGIN` 設為實際的 HTTPS 來源，並設定 `COOKIE_SECURE=true`。區網 HTTP 可使用文字助理。

送出的錄音由後端轉成單聲道 WAV 並傳給 `GEMINI_AUDIO_MODEL` 轉寫。Compose 映像已安裝 FFmpeg。本機啟動須另安裝 FFmpeg 並讓 `ffmpeg` 可從 PATH 執行。文字直接加選、已知研究路徑與本人選課查詢可在 Gemini 暫時無法使用時繼續操作。語音與文字模型分別依設定啟用。[Gemini 音訊輸入](https://ai.google.dev/gemini-api/docs/audio) · [預設模型支援格式](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)

## Email 設定與 SMTP 錯誤排查

課表信列出正式選課、待分發班別、上課時間、教室和學分。管理員可在「教務管理 → Email 通知狀態」查閱 Worker 處理結果。

Worker 的 SMTP 主機、連接埠、加密方式及帳密必須使用同一個服務的設定。預設 Mailpit 測試模式為：

```dotenv
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_STARTTLS=false
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=courses@example.test
```

透過 Gmail SMTP 寄到真實信箱時，請在專案 `.env` 設定以下項目，替換寄件地址及應用程式密碼。

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USER=yourname@gmail.com
SMTP_PASSWORD=Google產生的16碼應用程式密碼
SMTP_FROM=yourname@gmail.com
```

Google 帳戶需先開啟兩步驟驗證，再到[應用程式密碼](https://myaccount.google.com/apppasswords)建立 CourseFlow 專用密碼。依 [Google SMTP 文件](https://developers.google.com/workspace/gmail/imap/imap-smtp)，此設定使用 587 與 STARTTLS。學生的 `users.email` 也必須改為真實收件地址，種子資料預設為 `example.test`。

修改後重新建立 Worker；使用 `--build` 時也會載入本目錄最新的錯誤診斷程式：

```bash
sudo docker compose up -d --build --no-deps --force-recreate worker
sudo docker compose exec worker python -c 'import os; print({k: os.getenv(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_STARTTLS")})'
```

Gmail 應顯示 `smtp.gmail.com`、`587`、`true`。這個檢查不會輸出帳號、密碼；不要以完整環境或完整 Compose 設定作為公開的除錯輸出。

| 日誌錯誤碼 | 原因與處理 |
|---|---|
| `SMTP_AUTH_UNSUPPORTED` | 目前伺服器未提供帳密驗證。若顯示 `mailpit:1025` 卻填入 Gmail 帳密，請將主機、埠及 TLS 一起改成 Gmail；使用 Mailpit 則清空帳密。 |
| `SMTP_STARTTLS_UNSUPPORTED` | 所連伺服器沒有 STARTTLS。請核對主機與埠；程式不會降級為明文登入。 |
| `SMTPAuthenticationError` | SMTP 認證未通過，請確認寄件帳號與該帳號產生的應用程式密碼。 |

舊版 Worker 的 `SMTPNotSupportedError` 可能是前兩項原因；新版日誌會顯示明確錯誤碼及固定提示，不輸出憑證。Mailpit 預設不使用加密或驗證，詳見 [Mailpit SMTP 文件](https://mailpit.axllent.org/docs/configuration/smtp/)。

設定修正後，可在「我的課表」點「寄送課表」，並在「教務管理 → Email 通知狀態」查看寄送結果。未達三次的待寄通知依原排程重試。已達三次的 `FAILED` 通知會保留既有結果。

## Gemini 設定與驗收

選課助理使用 Gemini API，需另外取得 Gemini 金鑰。先開啟 [Google AI Studio 的 API Keys 頁面](https://aistudio.google.com/apikey)，登入 Google 帳號，建立或選擇專案後按「建立 API 金鑰」，將金鑰複製至本機的專案環境檔。建立及管理方式參考 [Google 官方說明](https://aistudio.google.com/docs/api-key)。

```bash
sudo nano .env
```

修改既有項目（不要重複新增同名項目），儲存後離開；金鑰不要貼到聊天、程式碼或日誌：

```ini
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_AUDIO_MODEL=gemini-3.5-flash-lite
GEMINI_API_KEY=貼上你在Google_AI_Studio建立的Gemini_API金鑰
CHAT_STUB=false
```

目前預設 `gemini-3.5-flash-lite`，可改為專案可用且支援結構化輸出的模型。選課助理透過 `langchain-google-genai` 連接 Gemini Developer API，用 Pydantic 驗證結構化計畫，再從 PostgreSQL 取得課程與資格。最小連線測試額外提供直接 REST 診斷。[模型說明](https://aistudio.google.com/docs/models/gemini-3.5-flash-lite) · [LangChain Gemini 整合](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai)

重建 API 容器以載入新的程式與環境變數；接著重啟前端代理，讓它重新解析 API 容器位址：

```bash
sudo docker compose up -d --build --no-deps --force-recreate api
sudo docker compose restart frontend
```

確認容器是否已載入設定（只顯示是否存在金鑰，不顯示內容），再以學生帳號登入測試，例如 `student01`：

```bash
sudo docker compose exec api python -c 'import os; print({"GEMINI_MODEL": os.getenv("GEMINI_MODEL"), "GEMINI_AUDIO_MODEL": os.getenv("GEMINI_AUDIO_MODEL"), "CHAT_STUB": os.getenv("CHAT_STUB"), "GEMINI_API_KEY_SET": bool(os.getenv("GEMINI_API_KEY", "").strip())})'
```

預期看到 `GEMINI_API_KEY_SET: True`、`CHAT_STUB: 'false'` 及指定的模型名稱。這只確認設定載入，實際呼叫仍取決於金鑰權限、模型可用性、網路與專案額度。

以下指令用真實 Gemini 測試 115-1 查課與研究推薦，並比對 PostgreSQL 中的公開班別。

```bash
sudo docker compose exec api python -m app.verify_gemini
```

若失敗，指令會以退出碼 1 輸出診斷 JSON，包含 `reason_code`，以及可辨識的 `http_status`、`provider_status`、`provider_reasons`、`request_fields` 與處理提示。診斷只顯示已知代碼及欄位名稱。例如 HTTP 400 搭配 `API_KEY_INVALID` 表示金鑰無效。

可先執行最小連線測試，使用同一組模型與金鑰傳送固定公開文字，不存取資料庫、不要求結構化輸出：

```bash
sudo docker compose exec api python -m app.verify_gemini --connection-only
```

若最小請求也失敗，先依診斷檢查金鑰、專案限制、帳務及地區條件；若最小請求成功而完整查詢失敗，再檢查結構化輸出與應用程式設定。最小連線測試不會產生完整驗收紀錄，仍需執行未加參數的驗證指令確認課程查詢。

若仍只有 `400 INVALID_ARGUMENT` 而沒有可辨識原因，可在主機專案目錄執行下列指令，不必再次建置映像檔：

```bash
sudo docker compose exec -T api python < scripts/diagnose-gemini.py
```

此腳本從容器讀取金鑰與模型，向 Google 的 `generateContent` 端點傳送固定公開文字。請求失敗時，查看遮蔽金鑰後的 `provider_message`，並依訊息檢查金鑰、專案或 API 條件。

兩個情境皆通過比對時，API 容器的 `/tmp/gemini-verification.json` 會記錄公開課程結果與推薦依據。紀錄不含學生課表、金鑰或確認草案。

```bash
sudo docker compose cp api:/tmp/gemini-verification.json docs/gemini-verification.json
```

未設定金鑰時，助理回 `503 CHAT_UNAVAILABLE` 並提供課程、課表入口；其他功能正常。`CHAT_STUB=true` 僅供測試：以固定意圖替代 Gemini，仍走同一個 LangGraph 與真實資料庫工具。測試 stub 不算真實 Gemini 驗收。

若仍失敗，畫面會顯示對應提示。API 日誌會記錄 `reason_code`，收到 Gemini HTTP 錯誤時也會記錄 `provider_http_status`。Gemini 回傳 500、502 或 503 時，助理會在原有逾時上限內等候片刻並重試一次。[Gemini 重試指南](https://aistudio.google.com/docs/troubleshooting#retry-strategy)

| reason_code | 處理方式 |
|---|---|
| `GEMINI_NOT_CONFIGURED` | 填入 Gemini 金鑰，重新建立 `api` 容器；只執行 `restart api` 不會更新 Compose 環境變數 |
| `GEMINI_AUTH_FAILED` | 檢查金鑰是否有效、是否有 Gemini API 存取權限 |
| `GEMINI_PAYMENT_REQUIRED` | 在 Google AI Studio 檢查專案帳務 |
| `GEMINI_MODEL_UNAVAILABLE` | 更新為該專案可使用且支援結構化輸出的模型 |
| `GEMINI_QUOTA_EXCEEDED` | 檢查 Google AI Studio 的專案額度與請求速率，稍後再試 |
| `GEMINI_REQUEST_REJECTED` | 檢查 API 金鑰、模型名稱與模型參數設定 |
| `GEMINI_TIMEOUT` | 檢查容器連外網路，稍後再試 |
| `GEMINI_PROVIDER_UNAVAILABLE` | 重試後 Gemini 仍回傳 500、502 或 503，請稍後再試 |
| `CHAT_FAILED` | 以 request_id 對照 `sudo docker compose logs --tail 100 api` 查閱錯誤類別 |

完整驗收結果以 `python -m app.verify_gemini` 產生的[真實 Gemini 紀錄](docs/gemini-verification.json)為準。

## 自動測試

API、交易與併發測試使用真實 PostgreSQL，絕不以 SQLite 替代。測試只接受名稱以 `_test` 結尾的資料庫，並清空該測試資料庫的應用資料。

Compose 已啟動後，使用 migration 角色建立獨立 `courses_test` 並執行：

```bash
make test
# 等同：docker compose run --rm migrate python run_tests.py
```

本機 PostgreSQL 開發環境：

```bash
DATABASE_URL=postgresql+psycopg://course_migrator@127.0.0.1:55432/courses \
PUBLIC_DATABASE_URL=postgresql+psycopg://course_public:course_public@127.0.0.1:55432/courses \
.venv/bin/python backend/run_tests.py
```

跨學期的瀏覽器回歸會進行加退選、初選登記、課表寄送和容量維護。請使用獨立測試資料庫，以 `seed('add-drop', recommendation_demo=True, university_demo=True, include_demo_terms=True)` 建立測試情境，設定 `PUBLIC_INITIAL_TERM_ID=`、`CHAT_STUB=true` 並將 SMTP 指向 Mailpit，再啟動前後端。

```bash
cd frontend
npm ci
npx playwright install chromium
E2E_BASE_URL=http://localhost:5173 npm run test:e2e
```

如測試 Compose 前端，改為 `E2E_BASE_URL=http://localhost:8080`。前端建置為 `npm run build`（含 TypeScript 型別檢查）。實測結果與限制請參閱 [V2 驗證報告](docs/verification-v2.md)。

## 本機開發（Linux、不依賴 Docker daemon）

需要 Python 3.12、PostgreSQL 16（含 `pg_config`、`initdb`、`pg_ctl`）及 Node.js 22。初始化腳本會在專案內建立 `.venv` 與 `.runtime`。

```bash
./scripts/setup-local.sh add-drop
./scripts/run-local.sh
```

本機入口為 http://localhost:5173 與 http://localhost:8000/docs；資料庫使用 loopback 55432，資料存於 `.runtime/pgdata`。本機資料庫是展示用 trust 認證，僅監聽 `127.0.0.1`；Compose 則使用各用途的密碼角色。Ctrl+C 停止腳本啟動的應用程序，PostgreSQL 資料保留。

若有 Mailpit 可執行檔，可將它放在 `.runtime/mailpit`，腳本會啟動 SMTP 1025 與 Web 8025。本機腳本使用 Mailpit 的無帳密設定。SMTP 暫時無法使用時，郵件依 Outbox 規則重試。

真實金鑰可放入專案根目錄的 `.env`。測試用文字分類可用 `CHAT_STUB=true ./scripts/run-local.sh`。語音端點使用 Gemini 轉寫，整合測試會模擬這個回應。

若目前帳號無法讀取 `.env`，本機程序會改用環境變數及預設值。Compose 仍由執行部署的帳號讀取自己的環境檔。

## API 與專案結構

全部業務路由為 `/api/v1`，請參閱 `/docs` 與 `/openapi.json`。學生身分來自 HttpOnly Session。錯誤回傳 `code`、`message`、`request_id` 與必要的 `details`。加退選、草案確認及寄送課表需要 `Idempotency-Key`。前端會停用提交中的操作，遇到結果不明時保留原鍵與班別集合。

| API | 用途 |
|---|---|
| `GET /api/v1/schools`、`GET /api/v1/topics` | 提供公開查課的校系與主題選項。班別列表接受 `school_code` 和 `topic_code`。 |
| `POST /api/v1/chat` | 傳送 `message` 與 `term_id`。直接加選帶 `Idempotency-Key`。從候選選班時附原指令與 `section_id`。已選定研究方向後可用 `track_code` 接續試排。 |
| `POST /api/v1/chat/voice` | 以 `multipart/form-data` 傳送 `audio`、`term_id` 並附 `Idempotency-Key`。回傳轉寫及與文字助理相同的結果。 |
| `POST /api/v1/me/terms/{term_id}/selection-plans/{plan_id}/confirm` | 只接受本人草案中的 1–5 班。`section_ids` 是確定送出的班別集合。任一班失敗時整批回滾，回應以 `details.section_id` 指出班別。 |
| `POST /api/v1/me/terms/{term_id}/timetable/email` | 將當下課表排入本人信箱的寄送佇列。`201` 回傳 `notification_id`。 |

```text
frontend/src/              React 介面、型別與 API client
frontend/tests/            Playwright 端到端測試
backend/app/models.py      資料模型與關聯約束
backend/app/registration.py 共用規則、鎖定、分發、冪等性
backend/app/auth.py         Argon2id、隨機 Session、撤銷
backend/app/admin.py        草稿設定、公開、班別與容量
backend/app/chat.py         LangGraph、Gemini、公開唯讀 SQL 工具
backend/app/recommendation.py 主題排行、資格試排、草案與來源證據
backend/app/worker.py       SMTP Outbox Worker
backend/app/seed.py         固定 ID 的可重複種子資料
backend/alembic/            migration、固定初版 DDL 與公開 View
backend/tests/             PostgreSQL 整合、併發、SMTP 與效能測試
scripts/                   初始化及本機開發腳本
compose.yaml               db / migrate / api / frontend / worker / mailpit
```

[架構、核心 ERD 與交易設計](docs/architecture.md)、[SQL 與 EXPLAIN ANALYZE BUFFERS](docs/explain.txt)、[效能數據](docs/performance.json)、[課程查詢畫面](docs/desktop.png)、[手機畫面](docs/mobile.png)。

本版的需求對照、測試結果及部署驗收項目見 [V2 驗證報告](docs/verification-v2.md)。[v1.0.0 驗證紀錄](docs/verification.md) 保留原交付結果。
