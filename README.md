# CourseFlow 大學選課與課程推薦系統

**版本 2.1.0**。CourseFlow 提供公開查課、初選登記、分發、週課表與教務管理。學生可用文字或語音查課，依 16 種研究方向試排並確認選課。115-1 課庫涵蓋臺大、臺師大與臺科大電資參考課程。

介面使用繁體中文，時間以臺北時間顯示。示範班別的時間、容量、學分及教師均為虛構資料。系統使用 React、FastAPI、PostgreSQL、LangGraph、Gemini 與 Docker Compose。 [需求說明書 3.2](Course_Registration_SRS.md)

[快速部署](#docker-compose-啟動) · [部署設定](#部署設定) · [區網與語音](#區網登入與語音) · [選課與課表](#115-1-選課與課表) · [驗證與開發](#自動測試)

![115-1 待分發課表與寄送課表按鈕](docs/timetable.png)

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
git clone --branch v2.1.0 https://github.com/programmerKB/CourseFlow.git
cd CourseFlow
sudo ./deploy.sh
```

[deploy.sh](deploy.sh) 會從範本建立缺少的 `.env`、建置映像檔、套用資料庫 migration、初始化示範資料，再啟動 API、前端及通知 Worker。健康檢查通過後會列出入口。預設公開 **115-1**，初選開放至初始化後第 7 天。

| 服務 | 預設入口 |
|---|---|
| 選課系統 | [localhost:8080](http://localhost:8080) |
| 選課助理 | [localhost:8080/chat](http://localhost:8080/chat) |
| API 文件 | [localhost:8000/docs](http://localhost:8000/docs) |
| Mailpit 收信畫面 | [localhost:8025](http://localhost:8025) |
| API 健康檢查 | [存活](http://localhost:8000/health/live) · [資料庫就緒](http://localhost:8000/health/ready) |

### 更新既有部署

在既有專案目錄取得版本標籤，再使用原有的 `.env` 和資料庫 volume。

```bash
git fetch origin --tags
git switch --detach v2.1.0
sudo ./deploy.sh
```

更新時會暫停應用服務，套用 migration 後重新啟動。種子程式會補齊三校班別，將舊展示學期的班別補入 115-1，並保留原有班別、登記、選課、分發及先修紀錄。已完成初始化、只需套用程式或環境設定時，可用 `--skip-seed`。

| 參數 | 用途 |
|---|---|
| `--skip-seed` | 套用程式、migration 與環境設定 |
| `--https` | 啟用區網 HTTPS，先驗證憑證與代理設定 |
| `--no-build` | 沿用已建置的映像檔 |
| `--scenario initial\|awaiting\|add-drop\|short` | 首次初始化的選課情境 |
| `--env-file 路徑` | 指定環境檔，相對路徑以執行指令的目錄為準 |
| `--timeout 秒數` | 每個服務與 HTTP 檢查的等待上限，預設 180 秒 |

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

送出的錄音由後端轉成單聲道 WAV 並傳給 `GEMINI_AUDIO_MODEL` 轉寫。Compose 映像已安裝 FFmpeg。本機啟動須另安裝 FFmpeg 並讓 `ffmpeg` 可從 PATH 執行。文字直接加選、已知研究路徑與本人選課查詢可在 Gemini 暫時無法使用時繼續操作。語音與文字模型分別依設定啟用。[Gemini 音訊輸入](https://ai.google.dev/gemini-api/docs/audio) · [預設模型支援格式](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)

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
5. 在選課助理詢問「找這學期星期二還有空位的電子系課」，點開來源核對班別。這個問法直接查詢公開課程資料。
6. 展示研究推薦、試排與確認流程。在「我的課表」點「寄送課表」，再開啟 Mailpit 查看郵件。管理頁可查詢寄送狀態。

### 研究方向推薦與批次選課

1. 以 `student01` 登入 **115-1**。
2. 打開選課助理，輸入「我的研究方向是大語言模型，請推薦並幫我選三門。」或點選同一個範例。`LLM` 與「大語言模型」使用同一組主題與排序規則。
3. 推薦可涵蓋三校課程和 `DEMO-501` 至 `DEMO-505`。展開推薦可核對大綱、先修、全部時段與研究延伸。
4. 在初選期間按「確認初選登記」，查看待分發結果。加退選期間按「確認加選」，更新正式課表。若名額或課表已變動，可按「更新試排」。

測試時可使用 `CHAT_STUB=true`，讓固定意圖走同一條推薦與選課流程。實際 Gemini 驗收需設定金鑰與 `CHAT_STUB=false`。

### 三校課庫與單門加選

執行 `sudo ./deploy.sh` 後，可直接在 **115-1** 查詢 40 門三校參考課程及 41 個三校模擬班別。校別篩選可分別查看臺大、臺師大與臺科大。

1. 在課程查詢選擇「國立臺灣科技大學」、「電子工程系」與「計算機結構與記憶體系統」。班別詳情附「校方課程來源」。上課時段、名額、學分及教師是專題資料。
2. 在即時加退選期間，以學生帳號開啟選課助理，輸入「我要加選臺科大電子系的高等計算機結構」或「我要加選 NTUST-ET-001」。找到唯一班別且資格符合時，當次送出就會更新「我的課表」。
3. 省略校系時會列出同名課的候選班別。初選期間可點「登記此班」並確認，課表會顯示待分發。加退選期間可點「加選此班」。
4. 輸入「我想做 RF，這學期先修什麼？」查看基礎、進階和研究專題。可接著按「試排本期課程」，再確認要提交的班別。「IC 設計」和「NVM」會先請你選方向。

## Gemini 設定與驗收

### 設定模型與金鑰

在 Google AI Studio 選擇專案、建立 API 金鑰，再填入 VM 的 `.env`。 [建立 Gemini API 金鑰](https://aistudio.google.com/apikey) · [金鑰設定文件](https://aistudio.google.com/docs/api-key)

```bash
sudo nano .env
```

在環境檔修改這四項設定，將金鑰保存在 VM 本機。

```ini
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_AUDIO_MODEL=gemini-3.5-flash-lite
GEMINI_API_KEY=貼上你在Google_AI_Studio建立的Gemini_API金鑰
CHAT_STUB=false
```

目前預設 `gemini-3.5-flash-lite`，可改為專案可用且支援結構化輸出的模型。選課助理透過 `langchain-google-genai` 連接 Gemini Developer API，用 Pydantic 驗證結構化計畫，再從 PostgreSQL 取得課程與資格。最小連線測試額外提供直接 REST 診斷。[模型說明](https://aistudio.google.com/docs/models/gemini-3.5-flash-lite) · [LangChain Gemini 整合](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai)

重建 API 容器以載入新的程式與環境變數。等 API 就緒後重啟前端代理，讓它重新解析 API 容器位址。

```bash
sudo docker compose up --build --no-deps --force-recreate --wait api &&
  sudo docker compose restart frontend
```

確認容器是否已載入設定（只顯示是否存在金鑰，不顯示內容），再以學生帳號登入測試，例如 `student01`：

```bash
sudo docker compose exec api python -c 'import os; print({"GEMINI_MODEL": os.getenv("GEMINI_MODEL"), "GEMINI_AUDIO_MODEL": os.getenv("GEMINI_AUDIO_MODEL"), "CHAT_STUB": os.getenv("CHAT_STUB"), "GEMINI_API_KEY_SET": bool(os.getenv("GEMINI_API_KEY", "").strip())})'
```

預期看到 `GEMINI_API_KEY_SET: True`、`CHAT_STUB: 'false'` 及指定的模型名稱。這只確認設定載入，實際呼叫仍取決於金鑰權限、模型可用性、網路與專案額度。

### 驗證與診斷

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

選課、課表、寄信與明確指定星期、系所和空位的查課可直接使用資料庫。需由 Gemini 處理的問題會在服務無法使用時提供課程查詢及課表入口。`CHAT_STUB=true` 供測試使用，以固定意圖執行同一套 LangGraph 與資料庫工具。

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

## Email 設定與 SMTP 錯誤排查

課表信列出正式選課、待分發班別、上課時間、教室和學分。管理員可在「教務管理 → Email 通知狀態」查閱 Worker 處理結果。

### Mailpit 測試收信

預設將通知送到 Mailpit。用瀏覽器開啟收信畫面，即可檢查寄送內容。 [Mailpit 收信畫面](http://localhost:8025)

```dotenv
SMTP_HOST=mailpit
SMTP_PORT=1025
SMTP_STARTTLS=false
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=courses@example.test
```

### Gmail 寄送

透過 Gmail SMTP 寄信時，在專案 `.env` 設定寄件地址與應用程式密碼。

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

部署腳本可用 `python3 -m unittest discover -s scripts/tests -v` 驗證。測試會使用模擬 Docker CLI。

### 瀏覽器

跨學期的瀏覽器回歸會進行加退選、初選登記、課表寄送和容量維護。請使用獨立測試資料庫，以 `seed('add-drop', recommendation_demo=True, university_demo=True, include_demo_terms=True)` 建立測試情境，設定 `PUBLIC_INITIAL_TERM_ID=`、`CHAT_STUB=true` 並將 SMTP 指向 Mailpit，再啟動前後端。

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

若有 Mailpit 可執行檔，可將它放在 `.runtime/mailpit`，腳本會啟動 SMTP 1025 與 Web 8025。本機腳本使用 Mailpit 的無帳密設定。SMTP 暫時無法使用時，郵件依 Outbox 規則重試。

真實金鑰可放入專案根目錄的 `.env`。測試用文字分類可用 `CHAT_STUB=true ./scripts/run-local.sh`。語音端點使用 Gemini 轉寫，整合測試會模擬這個回應。

若目前帳號無法讀取 `.env`，本機程序會改用環境變數及預設值。Compose 仍由執行部署的帳號讀取自己的環境檔。

## API 與專案結構

全部業務路由為 `/api/v1`，請參閱 `/docs` 與 `/openapi.json`。學生身分來自 HttpOnly Session。錯誤回傳 `code`、`message`、`request_id` 與必要的 `details`。加退選、草案確認及寄送課表需要 `Idempotency-Key`。前端會停用提交中的操作，遇到結果不明時保留原鍵與班別集合。

| API | 用途 |
|---|---|
| `GET /api/v1/schools`、`GET /api/v1/topics` | 提供公開查課的校系與主題選項。班別列表接受 `school_code` 和 `topic_code`。 |
| `POST /api/v1/chat` | 傳送 `message` 與 `term_id`。直接加選帶 `Idempotency-Key`。從候選選班時附原指令與 `section_id`。已選定研究方向後可用 `track_code` 接續試排。 |
| `POST /api/v1/chat/voice` | 以 `multipart/form-data` 傳送 `audio`、`term_id` 並附 `Idempotency-Key`。可用 `track_code` 接續研究方向試排。回傳轉寫及與文字助理相同的結果。 |
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
compose.yaml               PostgreSQL、migration、API、前端、通知及 HTTPS
```

[架構、核心 ERD 與交易設計](docs/architecture.md)、[SQL 與 EXPLAIN ANALYZE BUFFERS](docs/explain.txt)、[效能數據](docs/performance.json)、[課程查詢畫面](docs/desktop.png)、[手機畫面](docs/mobile.png)。

歷史驗收紀錄可參閱 [v2.0.0 驗證報告](docs/verification-v2.md) 與 [v1.0.0 驗證紀錄](docs/verification.md)。
