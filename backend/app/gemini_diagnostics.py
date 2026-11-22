"""Operator diagnostics containing only known codes, field names, and local hints."""
import re

from .gemini_rest import GeminiHTTPError


STATUSES = frozenset({
    'INVALID_ARGUMENT', 'FAILED_PRECONDITION', 'UNAUTHENTICATED',
    'PERMISSION_DENIED', 'NOT_FOUND', 'RESOURCE_EXHAUSTED',
    'DEADLINE_EXCEEDED', 'INTERNAL', 'UNAVAILABLE',
})
REASON_HINTS = {
    'API_KEY_INVALID': 'API 金鑰無效；請從 Google AI Studio 重新複製金鑰，更新 .env 並重新建立 api 容器。',
    'API_KEY_EXPIRED': 'API 金鑰已過期；請更換金鑰並重新建立 api 容器。',
    'API_KEY_LEAKED': 'API 金鑰已被停用；請在 Google AI Studio 建立新金鑰並重新建立 api 容器。',
    'API_KEY_SERVICE_BLOCKED': 'API 金鑰的服務限制拒絕此請求；請檢查 Generative Language API 存取限制。',
    'API_KEY_IP_ADDRESS_BLOCKED': 'API 金鑰的 IP 限制拒絕此伺服器；請檢查伺服器對外 IP 與金鑰限制。',
    'API_KEY_HTTP_REFERRER_BLOCKED': 'API 金鑰設有網站來源限制；請改用適合後端伺服器的金鑰限制。',
    'API_KEY_ANDROID_APP_BLOCKED': 'API 金鑰限制為 Android 應用程式；請檢查後端使用的金鑰。',
    'API_KEY_IOS_APP_BLOCKED': 'API 金鑰限制為 iOS 應用程式；請檢查後端使用的金鑰。',
    'SERVICE_DISABLED': '專案尚未啟用所需 API；請在 Google Cloud 檢查 Generative Language API。',
    'BILLING_DISABLED': '專案帳務未啟用；請檢查 Google AI Studio 的專案帳務設定。',
    'CONSUMER_SUSPENDED': 'Google 專案已被停權；請檢查專案狀態。',
    'USER_LOCATION_UNSUPPORTED': '目前伺服器所在地區不支援此 API；請檢查 Gemini API 地區與帳務條件。',
}
MESSAGE_REASONS = (
    ('API_KEY_INVALID', ('api key not valid', 'api key is invalid', 'invalid api key')),
    ('API_KEY_EXPIRED', ('api key expired', 'api key has expired')),
    ('API_KEY_LEAKED', ('reported as leaked',)),
    ('USER_LOCATION_UNSUPPORTED', ('user location is not supported',)),
)
FIELDS = {
    name: (name, camel) for name, camel in (
        ('candidate_count', 'candidateCount'), ('temperature', 'temperature'),
        ('top_p', 'topP'), ('top_k', 'topK'),
        ('max_output_tokens', 'maxOutputTokens'), ('thinking_config', 'thinkingConfig'),
        ('response_json_schema', 'responseJsonSchema'), ('response_schema', 'responseSchema'),
        ('response_mime_type', 'responseMimeType'), ('system_instruction', 'systemInstruction'),
        ('tool_config', 'toolConfig'),
    )
}


def safe_provider_diagnostic(error):
    """Read the exception chain without returning provider text or request data."""
    for _ in range(12):
        if isinstance(error, GeminiHTTPError):
            break
        error = error.__cause__ or error.__context__
        if error is None:
            return {}
    else:
        return {}

    result = {}
    if type(error.code) is int:
        result['http_status'] = error.code
    if isinstance(error.status, str) and error.status in STATUSES:
        result['provider_status'] = error.status
    details = error.details.get('details', [])
    if not isinstance(details, list):
        details = []
    reasons, field_texts = [], []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        reason = detail.get('reason')
        if isinstance(reason, str) and reason in REASON_HINTS and reason not in reasons:
            reasons.append(reason)
        violations = detail.get('fieldViolations', [])
        if isinstance(violations, list):
            field_texts.extend(v.get('field') for v in violations if isinstance(v, dict))
    # Inspect only for fixed signatures; never return or log any message fragment.
    message = error.message if isinstance(error.message, str) else ''
    for reason, signatures in MESSAGE_REASONS:
        if any(signature in message.lower() for signature in signatures) and reason not in reasons:
            reasons.append(reason)
    if reasons:
        result['provider_reasons'] = reasons
        result['hint'] = REASON_HINTS[reasons[0]]
    fields = sorted(name for name, aliases in FIELDS.items() if any(
        re.search(r'(?<![A-Za-z0-9_])' + re.escape(alias) + r'(?![A-Za-z0-9_])', value)
        for alias in aliases for value in [message, *field_texts] if isinstance(value, str)))
    if fields:
        result['request_fields'] = fields
    if not reasons:
        result['hint'] = ('Google 錯誤提及已列出的請求欄位，請檢查模型支援的參數。' if fields else
                          '請先執行 --connection-only；若最小請求也失敗，請檢查金鑰、專案限制、帳務及地區條件。')
    return result
