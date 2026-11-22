import pytest
from pydantic import ValidationError

from app.config import Settings, settings


LAN_ORIGIN = 'http://192.168.0.150:8080'
LOCAL_ORIGIN = 'http://localhost:8080'


def test_browser_origins_use_exact_normalized_urls_from_configuration():
    configured = Settings(_env_file=None, app_origin='https://COURSES.example:443/',
                          app_allowed_origins=f'{LAN_ORIGIN}/, {LOCAL_ORIGIN}, ')
    assert configured.browser_origins == {'https://courses.example', LAN_ORIGIN, LOCAL_ORIGIN}
    for invalid in ('*', 'http://*.example', 'http://192.168.0.150:8080/login', 'null'):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, app_allowed_origins=invalid)


def test_lan_login_uses_allowlist_and_keeps_origin_guard(client, monkeypatch):
    monkeypatch.setattr(settings, 'app_origin', LOCAL_ORIGIN)
    monkeypatch.setattr(settings, 'app_allowed_origins', LAN_ORIGIN)

    def login(origin, **headers):
        return client.post('/api/v1/auth/login', headers={'Origin': origin, **headers},
                           json={'login': 'student01', 'password': 'Demo123!'})

    for origin in (LAN_ORIGIN, LOCAL_ORIGIN):
        response = login(origin, **{'Sec-Fetch-Site': 'same-origin'})
        assert response.status_code == 200
        assert client.get('/api/v1/auth/me').json()['login'] == 'student01'
        assert client.post('/api/v1/auth/logout', headers={'Origin': origin}).status_code == 204
        assert client.get('/api/v1/auth/me').status_code == 401

    denied = [
        ('http://192.168.0.150:8081', {}),
        ('https://192.168.0.150:8080', {}),
        ('null', {}),
        ('http://other.example:8080', {'Host': 'other.example:8080', 'X-Forwarded-Host': '192.168.0.150:8080'}),
        (LAN_ORIGIN, {'Sec-Fetch-Site': 'cross-site'}),
    ]
    for origin, headers in denied:
        response = login(origin, **headers)
        assert response.status_code == 403
        assert response.json()['code'] == 'ORIGIN_FORBIDDEN'
    assert client.get('/api/v1/auth/me').status_code == 401
