"""Check the Llama 3.2 3B endpoint used by both assistants (AT-37 preparation).

The probes below use the English agent's prompt. For the Chinese assistant's query plans run
``python -m app.verify_assistant``, which also compares the answers with PostgreSQL.

Usage:
  python -m app.verify_llama                     # server check, then six real-prompt probes
  python -m app.verify_llama --server-only       # reachability and installed model only
  python -m app.verify_llama --url http://192.168.169.1:11434   # try an address before saving it
  python -m app.verify_llama "Please enroll me in CS101, section A."

Nothing is written to the database. Exit status 1 means the assistant cannot use the endpoint.
"""
import argparse
import asyncio
import json
import sys
import time

from . import english_agent, llama
from .config import settings

PROBES = (
    'Please enroll me in CS101, section A.',
    "I'd like to add CS101 A.",
    'Please drop CS101, section A.',
    'Remove CS101 A from my schedule.',
    'Can I take CS101?',
    "Don't drop CS101.",
)
# Operator hints for the Windows-hosted Ollama setup described in the README.
HINTS = {
    'LLAMA_NOT_CONFIGURED': '尚未設定 LLAMA_BASE_URL。在專案 .env 加入 LLAMA_BASE_URL=http://<Windows IP>:11434。',
    'LLAMA_CONNECT_TIMEOUT': '連線逾時：位址可能不是 Windows 主機，或 Windows 防火牆擋住 TCP 11434。'
                             'VMware NAT 的 Windows 位址通常是 VMnet8 介面卡的 x.x.x.1；'
                             '請在 Windows 執行 scripts/windows/setup-ollama.ps1。',
    'LLAMA_CONNECTION_REFUSED': '連線被拒：主機可達，但 11434 沒有服務在監聽。請確認 Windows 的 Ollama 已啟動，'
                                '且 OLLAMA_HOST=0.0.0.0:11434（設定後要完全結束並重新開啟 Ollama）。',
    'LLAMA_TIMEOUT': '已連上但回應逾時。第一次載入模型較慢，可稍後重試或調高 LLAMA_TIMEOUT_SECONDS。',
    'LLAMA_NOT_OLLAMA': '這個位址的回應不是 Ollama API。請確認連接埠是 11434，且 LLAMA_API_STYLE=ollama。',
    'LLAMA_MODEL_MISSING': '伺服器上沒有指定的模型。請在 Windows 執行：ollama pull llama3.2:3b',
    'LLAMA_AUTH_FAILED': '模型伺服器拒絕金鑰，請檢查 LLAMA_API_KEY。',
    'LLAMA_UNREACHABLE': '無法連到模型伺服器，請先用 --server-only 檢查位址與防火牆。',
    'LLAMA_INVALID_OUTPUT': '模型回傳的內容不是有效的意圖 JSON。請確認模型是 llama3.2:3b。',
    'CHAT_STUB_ENABLED': 'CHAT_STUB=true：英文助理使用固定測試規則，不會呼叫 Llama。驗收與展示時請在 .env 設 CHAT_STUB=false。',
    'OLLAMA_BEFORE_0_5_USES_JSON_MODE': 'Ollama 版本低於 0.5，無法使用 JSON schema；助理改用 JSON 模式。建議更新 Ollama。',
}


async def probe(messages):
    results = []
    for message in messages:
        begin = time.monotonic()
        evidence = {}
        try:
            raw = await llama.chat_json(english_agent.SYSTEM_PROMPT, message, english_agent.INTENT_SCHEMA,
                                        meta=evidence)
            intent = english_agent.Intent.model_validate(raw)
            outcome = {'intent': intent.intent, 'course': intent.course, 'section': intent.section,
                       'negated': intent.negated, 'other_person': intent.other_person,
                       'explicit_request': intent.explicit_request, 'server': evidence}
        except llama.LlamaError as error:
            outcome = {'error': error.reason_code}
        except ValueError:
            outcome = {'error': 'LLAMA_INVALID_OUTPUT'}
        results.append({'message': message, 'elapsed_ms': round((time.monotonic() - begin) * 1000), **outcome})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('messages', nargs='*', help='English messages to classify (default: built-in probes).')
    parser.add_argument('--url', help='Use this base URL instead of LLAMA_BASE_URL for this check only.')
    parser.add_argument('--server-only', action='store_true', help='Check reachability and the model; skip inference.')
    args = parser.parse_args()
    if args.url:
        settings.llama_base_url = type(settings).validate_llama_base_url(args.url)
    report = asyncio.run(llama.server_status())
    report['chat_stub'] = settings.chat_stub
    if report['ok'] and not args.server_only:
        report['results'] = asyncio.run(probe(args.messages or PROBES))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    problems = [code for code in (report.get('reason_code'), report.get('warning'),
                                  'CHAT_STUB_ENABLED' if settings.chat_stub else None,
                                  *(row.get('error') for row in report.get('results', []))) if code]
    if not report['model_is_llama_3_2_3b']:
        print(f'提醒：LLAMA_MODEL={settings.llama_model} 不是 Llama 3.2 3B，課程規格要求 llama3.2:3b。', file=sys.stderr)
    for code in dict.fromkeys(problems):
        print(f'{code}：{HINTS.get(code, "請查看 API 日誌。")}', file=sys.stderr)
    if report['ok'] and not problems:
        print('選課助理（英文加退選與中文查詢）可以使用此模型服務。中文查詢可再執行 python -m app.verify_assistant。', file=sys.stderr)
    failed = not report['ok'] or settings.chat_stub or not report['model_is_llama_3_2_3b'] or any(
        'error' in row for row in report.get('results', []))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
