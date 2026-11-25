# CourseFlow v3.0.1 驗收

驗收日為 2026-11-25（臺北時間）。需求依專案根目錄的 [需求說明書 4.0](../Course_Registration_SRS.md) 與教師提供的 `Database.pdf` 第 1–4 節。驗收使用本機 PostgreSQL 16 的隔離資料庫（`courses_test`、`courses_e2e_test`）、FastAPI、Vite 與 Chromium，資料由專題種子建立。英文助理與中文查詢已連到 Windows 本機 Ollama 的 Llama 3.2 3B 實機驗證，語音以 VM 上的 faster-whisper 實機驗證；本版已移除 Gemini，不使用雲端模型。Gmail API 尚未實寄，步驟列於文末。

## 教師規格對照

| 教師項目 | 實作與檢查結果 |
|---|---|
| T-01、T-09–T-11 英文加退選 | LangGraph `english_registration`：`understand`（Llama 3.2 3B 結構化意圖）→ `decide`（明確性防護）→ `registration_tool` / `read_tool` / `reply`。兩種問法的加選與退選各自完成，稽核記錄 `agent`、`model` 與 `channel`，回應及日誌列出呼叫的工具。問句、否定、條件、引述、兩門課、替他人操作、課名不明與模型編造課號皆不寫入。 |
| T-02 Web 加退選 | 原有課程查詢與我的課表加退選通過回歸。加選成功提示「選課成功，確認信已排入寄送」。 |
| T-03 Gmail 確認信 | 單筆加選（網頁、英文、中文）、加退選草案確認與分發錄取在同一交易建立一筆 `REGISTRATION_CONFIRMATION`。退選、待分發、失敗請求與同鍵重送不增加事件。Worker 以 OAuth refresh token 呼叫 `users.messages.send`，保存 Gmail 訊息 ID；以模擬 HTTP 驗證訊息格式、503 重試後成功、未設定及授權失敗直接 `FAILED`。所有通知信預設以 Gmail SMTP（應用程式密碼）寄出，`GMAIL_TRANSPORT=api` 時改用 Gmail API；規格要求 Gmail API，差異見下方「Email 寄送方式」。 |
| T-04、T-05 衝堂與 18 學分 | 網頁與英文助理同一組案例：15 + 3 = 18 受理，21 回 `CREDIT_LIMIT_EXCEEDED`，部分時段重疊回 `TIME_CONFLICT`，首尾相接可選；兩入口錯誤碼與最終資料列相同。資料庫拒絕大於 18 的個人上限，較低上限照常生效。 |
| T-06、T-07、T-12 名單與成績 | 教師只看到本人授課班的 `ENROLLED` 學生；退選、待分發學生不列入。成績 82 → 90 寫入同一列與兩筆稽核，101、-1、三位小數及非數字回 `422`，他班或錯配修課回 `404`，學生與管理員回 `403`。退選後不能評分，重新加選沿用原修課 ID 與成績。 |
| T-08、T-13 PostgreSQL 與一致性 | 從 `0005` 建立舊資料（個人上限 25、已選 21 學分）後升級：ID 保留、上限改為 18 並留稽核、`v_credit_limit_overloads` 列出超限學期，學生仍可退選。網頁加選、英文退選、英文加選、網頁退選使用同一修課 ID，教師名單同步。 |

## 測試結果

- **PostgreSQL 與 API** 158 項全部通過，無略過（FFmpeg 音訊整合測試以本機 FFmpeg 9.0 執行）。移除 Gemini 時刪除兩個 Gemini 專用測試檔，`test_chat_diagnostics.py` 改為驗證本機模型的錯誤代碼、請求格式、欄位核對、研究路徑分流與 `verify_assistant`，`test_v2.py` 以替身 Whisper 模型驗證取樣格式、信心判斷與繁體轉換。新增 `test_teacher_spec.py` 22 項（AT-37–AT-45、Llama 與 Gmail 介面、Windows Ollama 連線診斷、經真實 HTTP 呼叫模型並依其輸出決策、種子）。英文流程以 `CHAT_STUB=true` 的固定意圖解析代替模型，其後的 LangGraph 防護、工具與交易與實機相同；另以模擬傳輸驗證 Ollama 與 OpenAI 相容格式、逾時、404 與非 JSON 輸出。
- **既有測試調整** 依 FR-24，四項舊測試原本斷言正式加選不建立通知，現改為各成功操作一筆確認信；`test_single_term_seed` 的示範上限由 25 改為 18；SMTP 測試的 `finish` 替身改為接受訊息 ID 參數。
- **瀏覽器與前端** 16 項 Playwright 測試通過（原 15 項及新增 `teacher-spec.spec.ts`；移除 Gemini 後重跑仍全數通過），TypeScript 與 Vite 產出成功。新測試以 `student05`（15 學分）用英文加選到 18 學分、觸發超修與衝堂、英文退選後回到 15 學分，再以 `teacher01` 登錄 82、改為 90.5 並驗證 101 的表單錯誤。
- **部署檢查** 15 項腳本測試（含 Gmail API 未設定時只警告）、（新增 `--llm` 順序與 Llama 連不到時只警告）、Shell 語法與 Compose 設定（含 `llm` profile）通過。
- **SQL 與執行計畫** 教師班別、名單含成績、單班有效名額與學生學期正式學分的 SQL 及 `EXPLAIN (ANALYZE, BUFFERS)` 見 [explain-v3.0.1.txt](explain-v3.0.1.txt)。示範資料量小時規劃器選擇循序掃描；關閉循序掃描的計畫顯示 `ix_sections_instructor_term`、`ix_enrolled_section` 與 `grades_pkey` 可用。

## AT-37 實機紀錄（2026-11-25）

VM（VMware NAT，`192.168.169.128`）經 `LLAMA_BASE_URL=http://192.168.169.1:11434` 連到 Windows 本機 Ollama 0.40.18，模型 `llama3.2:3b`（3.2B，Q4_K_M）。`CHAT_STUB=false`，使用隔離資料庫 `courses_e2e_test` 與 `student05`（15 學分）。

| 學生輸入 | Llama 意圖 / explicit_request | 結果 |
|---|---|---|
| Show my timetable. | my_timetable / false | 列出 15 學分課表 |
| yo can u sign me up for CS101 section A pls | add_course / true | 加選成功，18 學分，建立一筆確認信事件 |
| I'd like to add CS303 A. | add_course / true | `409 CREDIT_LIMIT_EXCEEDED`（21 學分） |
| Please enroll me in CS302, section A. | add_course / true | `409 TIME_CONFLICT`，列出 CS101 A、CS201 A |
| I dropped CS301 yesterday. | drop_course / false | 不寫入，請學生明確提出 |
| Put my roommate into CS201. | add_course / true（other_person） | 拒絕替他人操作 |
| Get me out of CS101 A asap. | drop_course / true | 退選成功，回到 15 學分 |

每次呼叫由 Ollama 回報約 800 個提示 token、41 個輸出 token，耗時約 0.43–0.75 秒。資料庫最後為 CS101 `DROPPED`、確認信事件 1 筆。`teacher-spec.spec.ts` 也改連實機模型通過（10.2 秒，替身為 2.2 秒），畫面顯示伺服器回報的 token 與耗時。另以未出現在提示詞中的 8 種說法探測，意圖與 explicit_request 皆正確（例如 “I took CS101 last year.” 為 unsupported / false）。

## 移除 Gemini：中文查詢與語音改用本機模型（2026-11-25）

`chat.py` 的中文查詢計畫改由 Llama 3.2 3B 產生（`llama.plan_query`），語音轉寫改用 API 程序內的 faster-whisper（`base`、int8、2 執行緒）。刪除 `gemini_rest.py`、`gemini_diagnostics.py`、`verify_gemini.py`、`scripts/diagnose-gemini.py` 與 `langchain-google-genai`、`google-genai` 套件；以 `verify_assistant` 取代 `verify_gemini`。映像建置時下載 Whisper 模型並設 `HF_HUB_OFFLINE=1`。

**中文查詢實機紀錄**（`courses_live_test`，`student01`，115-1 加退選）

| 學生輸入 | Llama 計畫（核對後） | 結果 |
|---|---|---|
| My research topic is Edge AI | recommend，track `edge_ai` | 邊緣 AI 路徑，優先考慮 機器學習、嵌入式系統、訊號與系統 |
| My research topic is Edge AI, 2 courses only, Wednesday | recommend，2 門，星期三整天 | 原本誤送英文加退選助理，改走研究路徑 |
| 我想做晶片設計，該修什麼課？ | recommend，track `digital_ic`（模型自行對應） | 數位 IC 與 VLSI 路徑 |
| 推薦兩門週二下午的機器學習相關課程 | recommend，2 門，週二 13:00–18:00 | 機器學習與電腦視覺路徑 |
| 我能修高等作業系統嗎 | eligibility，課名 高等作業系統 | 符合加選條件 |
| 有哪些台大電機所的課 | courses，NTU、電機所 | 模型把系所放進課名欄並另編「電子系」，核對後改正 |
| 加退選什麼時候截止？／我星期三有課嗎？ | status／timetable | 學期階段／本人課表 |
| 幫我看 student03 的課表 | 模型選 timetable，`guard.privacy` 攔下 | 拒絕查詢他人 |

每次呼叫由 Ollama 回報約 1,200 個提示 token、47–188 個輸出 token，0.6–2.1 秒。`verify_assistant` 對同一資料庫通過：查課結果與 SQL 預期相同，研究推薦為 `llm` 路徑。瀏覽器以真實模型詢問 “My research topic is Edge AI”，回覆下方顯示「llama3.2:3b 伺服器回報 1203+74 tokens · 1230 ms · llama.plan_query → …」。核對前的提示詞探測中，3B 模型 22 題的工具選擇對 19 題，常自行補上學校、系所或照抄範例的星期，因此保留逐欄核對。

**語音實機紀錄**（`base`，VM 4 核取 2 執行緒，經 FFmpeg 解碼）

| 錄音 | 轉寫 | 結果 |
|---|---|---|
| 找星期二還有空位的電子系課（合成語音） | 找星期二還有空位的電子系課。 | 查到 4 班 |
| 我的研究方向是邊緣AI，請給我修課路徑，只要兩門課 | 我的研究方向是邊緣 AI,請給我修課路徑,只要兩門課。 | Llama 計畫 2 門，邊緣 AI 路徑 |
| 我想要加選，CS101 A班 | 我想要加選、CS101 A 班。 | 修正指令解析後加選成功 |
| 我要加選臺科大電子系的高等計算機結構 | 原句 | 加選成功並排入確認信 |
| 公開華語樣本 13 秒 | 全句正確；未轉換前末段出現簡體字 | OpenCC 轉為繁體 |

每段 0.7–1.8 秒，模型載入 2.3 秒，程序峰值約 441 MB。`small` 準確度相近但峰值約 1 GB，3 GB 的 VM 不建議。未加選課詞彙提示時，`base` 會把「試排」聽成「請示牌」、「CS101 A」聽成「CS10EA」，加入提示後皆正確。

**與 SRS 4.0 的差異** §7.6 寫明送至 `GEMINI_AUDIO_MODEL` 並在介面提示「錄音會送往 Gemini 辨識」，AT-24 要求以真實 Gemini 驗證一次；本版改為本機 Whisper，介面提示改為「在本系統伺服器以 Whisper 轉寫」。§8「Agent 與模型」規定需經模型處理的文字意圖及對話使用 Llama 3.2 3B，Gemini 僅「可」繼續用於語音與舊推薦，因此文字部分改用 Llama 符合該條。

## Email 寄送方式（2026-11-25）

曾將所有通知信改經 Gmail API；因 OAuth 用戶端、測試使用者與 7 天失效的 refresh token 設定繁瑣，依使用者決定改回 **Gmail SMTP + 應用程式密碼** 作為預設（`GMAIL_TRANSPORT=smtp`），Gmail API 保留為選項（`GMAIL_TRANSPORT=api`）。**這與 `Database.pdf`「Email Service：Gmail API」不同**，驗收若要求 Gmail API，改設 `api` 並依 README 取得 OAuth 憑證即可，程式與測試兩條路徑都保留。

- 兩種方式寄出的信種相同：選課確認、課表、分發結果，皆保存訊息 ID（SMTP 以 `smtp:` 開頭）。
- SMTP 一律先 STARTTLS 再登入，不降級為明文；應用程式密碼的空白自動移除；帳密錯誤記為 `SMTP_AUTH_FAILED`、未填帳號記為 `SMTP_NOT_CONFIGURED`，都不重試，修正後以 `retry-failed` 重寄；連線失敗記為 `SMTP_UNREACHABLE` 並自動重試。錯誤欄只存固定代碼。
- `python -m app.manage verify-email [--send-to]` 依設定檢查 Gmail SMTP 登入或 Gmail API 授權，列出缺少的欄位名稱（不印出值）；部署最後的「檢查 Gmail 寄信」呼叫同一指令，失敗只警告。
- 以模擬 SMTP 伺服器驗證 TLS 與登入順序、登入失敗、未設定與連線失敗；瀏覽器回歸後以預設 SMTP 清空佇列，4 封確認信與 1 封課表信進入真實 Mailpit。Gmail API 路徑以模擬 Gmail 傳輸驗證。實際寄到 Gmail 仍待使用者填入應用程式密碼後執行 `verify-email --send-to`。

## 修正的問題

- 只輸入英文研究方向名稱（如 `RF`、`LLM`、`Edge AI`）時仍走既有修課路徑流程；含加退選、課表或名額等字詞的英文訊息才交給英文助理。
- FastAPI 會把 `Decimal` 轉成浮點數。成績與確認信中的學分改以字串輸出，保留兩位或一位小數。
- 「if possible」等禮貌用語不視為條件句。
- 寫入原本要求固定句型，Llama 已理解的口語要求（如 “Get me out of EE302 asap.”）會被擋下。改為由模型的 `explicit_request` 或句型規則判定明確性，否定、條件、引述、問句、兩門課與他人仍一律否決。
- 測試替身模式的工具名稱原本也寫成 `llama.parse_intent`，改為 `stub.parse_intent`，並在回應、畫面與日誌標示 `CHAT_STUB`。
- 只提到研究方向的英文訊息若含 courses、Wednesday 等字會被送進英文加退選助理，回傳星期三全部課程。現在沒有加退選動詞、且只有研究方向或含 research、topic、study 等字的英文訊息改走研究路徑。
- 語音轉寫常在動詞後加「、」或在「班」前留空格（「我想要加選、CS101 A 班」），單門指令解析器誤判為多門。現在接受這兩種寫法，兩門課之間的「、」仍要求一次一門。
- `test_at26` 過去因本機無 FFmpeg 而略過，仍預期不建立確認信；依 FR-24 改為勝出者建立一筆。
- 發布前檢查：OpenAI 相容端點不強制 JSON schema，模型若在星期、門數、研究方向或時段欄位回傳清單或物件，查詢計畫核對會拋出 `TypeError` 並回 `CHAT_FAILED`。現在型別不符的欄位一律捨棄（`test_wrongly_typed_model_fields_are_ignored_not_fatal`）。
- 確認信的提交時間原本直接印出 UTC ISO 字串（`2026-11-25T02:05:09+00:00`），改為臺北時間 `2026/11/25 10:05`。
- 教師登錄成績時若學生剛退選（`404`），畫面重新載入名單會同時清掉錯誤訊息，教師看不到原因；改以提示訊息顯示。
- 教務管理的 Email 通知狀態在錯誤代碼旁加上處理提示。
- `.env` 值前後的空白會讓 SMTP 主機查不到或登入失敗，現在讀入時去除；`SMTP_PORT=465` 改用 SSL 連線（Gmail 另一種連線方式），不再卡住後誤報連不到。
- `verify-email` 原本只回 `GMAIL_NOT_CONFIGURED`，看不出缺哪一欄；現在列出缺少或格式錯誤的欄位名稱與取得方式。`scripts/gmail-oauth.py` 先檢查用戶端 ID 格式，避免 Google 回 `401 invalid_client`。
- 發布前檢查：語音請求總時限 30 秒。轉寫成功後若本機模型首次載入超時，原本回「語音辨識暫時無法使用」且遺失辨識文字；現在回 `CHAT_UNAVAILABLE`／`LLAMA_TIMEOUT` 並保留 `transcript`，學生可直接重送（`test_model_timeout_after_speech_keeps_the_transcript`）。

## 部署環境待驗

- **AT-40（實機寄信）** 在 `.env` 填入 Gmail 帳號與應用程式密碼（或 `GMAIL_TRANSPORT=api` 與 OAuth 憑證），執行 `python -m app.manage verify-email --send-to <信箱>`，以 `set-email` 指定示範學生收件信箱，完成網頁加選、英文加選、草案確認與分發錄取各一次，核對收件內容與管理頁的訊息 ID。
- **AT-13** 需在可連 Docker daemon 的環境驗證映像建置、`--llm` 模型下載、容器冷啟與重啟後的 volume 保留。

## GitHub 交付

以 `release/v3.0.1` 分支提交，合併至 `main` 並標記 `v3.0.1`（主版號 3 因移除 Gemini 屬設定不相容的變更）。上節的 AT-40 實寄與 AT-13 Docker 冷啟仍待在部署主機補驗。
