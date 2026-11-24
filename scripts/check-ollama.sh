#!/usr/bin/env bash
# 從 VM 檢查 Windows 本機的 Ollama（不需 Docker 或 Python）。
# 用法：scripts/check-ollama.sh [http://192.168.169.1:11434] [llama3.2:3b]
# 未指定時讀 LLAMA_BASE_URL、LLAMA_MODEL 環境變數，再退回 VMware NAT 常見的 Windows 位址。
set -uo pipefail

url="${1:-${LLAMA_BASE_URL:-http://192.168.169.1:11434}}"
url="${url%/}"
model="${2:-${LLAMA_MODEL:-llama3.2:3b}}"

printf '檢查 %s（模型 %s）\n' "$url" "$model"
version="$(curl -sS --connect-timeout 5 --max-time 10 "$url/api/version" 2>&1)"
case $? in
  0) ;;
  7)
    printf '連線被拒：主機可達，但 11434 沒有服務在監聽。\n'
    printf '請在 Windows 確認 Ollama 已啟動，且 OLLAMA_HOST=0.0.0.0:11434（設定後要從系統匣結束並重新開啟 Ollama）。\n'
    exit 1 ;;
  28)
    printf '連線逾時：位址可能不是 Windows 主機，或 Windows 防火牆擋住 TCP 11434。\n'
    printf '請在 Windows 以系統管理員執行 scripts/windows/setup-ollama.ps1，並用 ipconfig 核對 VMnet8 的 IPv4 位址。\n'
    exit 1 ;;
  *)
    printf '無法連線：%s\n' "$version"
    exit 1 ;;
esac
printf 'Ollama 回應：%s\n' "$version"

tags="$(curl -sS --max-time 10 "$url/api/tags")" || { printf '無法讀取模型清單。\n'; exit 1; }
if ! grep -q "\"name\":\"$model\"" <<<"$tags"; then
  printf '伺服器沒有 %s。請在 Windows 執行：ollama pull %s\n' "$model" "$model"
  exit 1
fi
printf '已安裝 %s。送出一次測試推論（第一次載入模型可能需要數十秒）…\n' "$model"

start=$SECONDS
body="{\"model\":\"$model\",\"stream\":false,\"format\":\"json\",\"options\":{\"temperature\":0,\"num_predict\":32},"
body+="\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the JSON object {\\\"ok\\\": true}.\"}]}"
reply="$(curl -sS --max-time 180 "$url/api/chat" -H 'Content-Type: application/json' -d "$body")" || {
  printf '推論請求失敗。\n'; exit 1; }
if ! grep -q '"message"' <<<"$reply"; then
  printf '推論回應不正確：%s\n' "${reply:0:300}"
  exit 1
fi
printf '推論成功，耗時 %s 秒。\n\n' "$((SECONDS - start))"
printf '將下列設定寫入專案的 .env：\n  LLAMA_BASE_URL=%s\n  LLAMA_MODEL=%s\n' "$url" "$model"
