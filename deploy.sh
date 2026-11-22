#!/usr/bin/env bash
# Deploy the existing Compose project. Database volumes and existing .env are retained.
set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
caller_dir="$PWD"
env_file="$project_dir/.env"
scenario='initial'
seed_enabled=true
build_enabled=true
timeout_seconds=180
stage='參數檢查'
compose_ready=false
compose=()

usage() {
  cat <<'EOF'
使用方式：./deploy.sh [選項]

建置並部署 CourseFlow，執行 migration、健康檢查及可重複的示範 seed。
需要 Bash、Docker 與 Docker Compose plugin，以及 Docker daemon 使用權限。

選項：
  --scenario NAME   建立主學期的情境：initial / awaiting / add-drop / short
                    預設 initial。單學期模式可將舊的 115-1 示範時段轉為初選。
  --skip-seed       略過示範帳號與課程初始化，適合已有資料的部署。
  --no-build        使用既有映像檔，略過建置。
  --env-file PATH   指定環境檔；相對路徑以執行指令時的目錄為準。
  --timeout N       每個服務與 HTTP 檢查最多等待 N 秒（1–3600，預設 180）。
  -h, --help        顯示說明。

範例：
  ./deploy.sh
  ./deploy.sh --scenario awaiting
  ./deploy.sh --skip-seed --env-file .env.production

缺少環境檔時會從 .env.example 建立，不會覆寫既有檔案。
更新期間暫停 frontend、api、worker，完成 migration 後重新啟動。
本腳本保留資料庫 volume；首次使用範本時採本機展示設定。
EOF
}

fail() {
  printf '\n錯誤：%s\n' "$*" >&2
  exit 1
}

require_value() {
  [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || fail "$1 需要一個參數。"
}

on_error() {
  local code=$?
  trap - ERR
  printf '\n部署失敗，階段：%s（結束碼 %s）。\n' "$stage" "$code" >&2
  if [[ "$compose_ready" == true ]]; then
    "${compose[@]}" ps --all >&2 || true
    printf '\n可使用下列指令查閱服務日誌：\n  ' >&2
    printf '%q ' "${compose[@]}" logs --tail 100 >&2
    printf '\n資料庫 volume 與既有環境檔均保留，修正原因後可重新執行。\n' >&2
  fi
  exit "$code"
}
trap on_error ERR

while (( $# )); do
  case "$1" in
    --scenario)
      require_value "$@"
      scenario="$2"
      case "$scenario" in initial|awaiting|add-drop|short) ;; *) fail '不支援的初始化情境。' ;; esac
      shift 2
      ;;
    --skip-seed) seed_enabled=false; shift ;;
    --no-build) build_enabled=false; shift ;;
    --env-file)
      require_value "$@"
      if [[ "$2" == /* ]]; then env_file="$2"; else env_file="$caller_dir/$2"; fi
      shift 2
      ;;
    --timeout)
      require_value "$@"
      [[ "$2" =~ ^[1-9][0-9]*$ && ${#2} -le 4 ]] || fail '--timeout 必須是 1–3600 的整數。'
      (( $2 <= 3600 )) || fail '--timeout 必須是 1–3600 的整數。'
      timeout_seconds="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) fail "不支援的選項：$1；請使用 --help 查看說明。" ;;
  esac
done

stage='Docker 環境檢查'
command -v docker >/dev/null 2>&1 || fail '找不到 Docker，請先安裝 Docker Engine 與 Compose plugin。'
docker compose version >/dev/null 2>&1 || fail '無法使用 docker compose，請先安裝 Compose plugin。'
docker info >/dev/null 2>&1 || fail '無法連接 Docker daemon。請確認 Docker 已啟動，且目前帳號有 Docker socket 使用權限。'
[[ -f "$project_dir/compose.yaml" && -f "$project_dir/.env.example" ]] || fail '缺少 compose.yaml 或 .env.example，請在完整專案中執行。'

stage='環境檔與 Compose 設定'
if [[ ! -e "$env_file" ]]; then
  [[ -d "$(dirname -- "$env_file")" ]] || fail '指定環境檔的目錄不存在。'
  # noclobber protects an env file created concurrently; no shell evaluation of its contents.
  (umask 077; set -o noclobber; cat "$project_dir/.env.example" > "$env_file")
  # A user invoking sudo should still be able to edit their newly created configuration.
  if [[ "$EUID" -eq 0 && "${SUDO_UID:-}" =~ ^[0-9]+$ && "${SUDO_GID:-}" =~ ^[0-9]+$ ]]; then
    chown -- "$SUDO_UID:$SUDO_GID" "$env_file"
  fi
  printf '已從範本建立環境檔：%s\n' "$env_file"
fi
[[ -f "$env_file" && -r "$env_file" ]] || fail '指定環境檔無法讀取；若先前使用 sudo 建立，請以原部署權限執行，或由檔案擁有者修正權限。'
compose=(docker compose --project-directory "$project_dir" --file "$project_dir/compose.yaml" --env-file "$env_file")
"${compose[@]}" config --quiet
compose_ready=true

wait_service() {
  local service="$1" health_required="$2" deadline=$((SECONDS + timeout_seconds))
  local container_id state status health
  printf '等待 %s 就緒（最多 %s 秒）…\n' "$service" "$timeout_seconds"
  while (( SECONDS < deadline )); do
    container_id="$("${compose[@]}" ps --all --quiet "$service")"
    if [[ -n "$container_id" ]]; then
      state="$(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
      status="${state%%|*}"
      health="${state#*|}"
      if [[ "$status" == running ]]; then
        if [[ "$health" == healthy || ( "$health_required" == false && "$health" == none ) ]]; then
          return 0
        fi
      fi
      if [[ "$status" == exited || "$status" == dead || "$health" == unhealthy ]]; then
        printf '%s 未就緒：狀態 %s / health %s。\n' "$service" "$status" "$health" >&2
        return 1
      fi
    fi
    sleep 1
  done
  printf '%s 等待逾時。\n' "$service" >&2
  return 1
}

wait_frontend() {
  local deadline=$((SECONDS + timeout_seconds))
  printf '檢查前端 HTTP 與同源 API 代理…\n'
  while (( SECONDS < deadline )); do
    if "${compose[@]}" exec -T api python -c '
import json
from urllib.request import urlopen
with urlopen("http://frontend/", timeout=3) as response:
    assert response.status == 200
with urlopen("http://frontend/api/v1/terms", timeout=3) as response:
    assert response.status == 200
    assert isinstance(json.load(response), list)
' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf '前端或同源 API 代理未能在期限內就緒。\n' >&2
  return 1
}

local_url() {
  local mapping host port
  mapping="$("${compose[@]}" port "$1" "$2")"
  mapping="${mapping%%$'\n'*}"
  host="${mapping%:*}"
  port="${mapping##*:}"
  [[ -n "$mapping" && "$port" =~ ^[0-9]+$ ]] || return 1
  case "$host" in '0.0.0.0'|'[::]'|'::'|'127.0.0.1'|'') host=localhost ;; esac
  printf 'http://%s:%s' "$host" "$port"
}

if [[ "$build_enabled" == true ]]; then
  stage='映像檔建置'
  printf '\n建置後端與前端映像檔…\n'
  "${compose[@]}" build migrate api worker frontend
fi

stage='啟動資料庫與 Mailpit'
"${compose[@]}" up --detach db mailpit
wait_service db true
wait_service mailpit false

stage='暫停應用服務'
printf '\n暫停應用服務，準備套用 migration…\n'
"${compose[@]}" stop frontend api worker

stage='資料庫 migration'
# Always run a new migration job, including when an older migrate container exited successfully.
"${compose[@]}" run --rm --no-deps -T migrate

stage='啟動 API'
# Dependencies were handled explicitly above; do not run the one-shot migrate service twice.
"${compose[@]}" up --detach --no-deps --force-recreate api
wait_service api true

if [[ "$seed_enabled" == true ]]; then
  stage='初始化示範資料'
  "${compose[@]}" exec -T api python -m app.seed --scenario "$scenario"
fi

stage='啟動前端與通知 Worker'
"${compose[@]}" up --detach --no-deps --force-recreate frontend worker
wait_service frontend false
wait_service worker false
wait_frontend
wait_service worker false

stage='部署結果'
frontend_url="$(local_url frontend 80)"
api_url="$(local_url api 8000)"
mailpit_url="$(local_url mailpit 8025)"
printf '\n部署完成。\n選課系統：%s\nAPI 文件：%s/docs\nMailpit：%s\n' "$frontend_url" "$api_url" "$mailpit_url"
printf '\n瀏覽器網址須在 APP_ORIGIN 或 APP_ALLOWED_ORIGINS 設定中。\n'
"${compose[@]}" ps --all
