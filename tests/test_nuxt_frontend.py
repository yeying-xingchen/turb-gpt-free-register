"""Nuxt hosting and session API contract, without requiring a Node build."""
import pytest

from webui.app import create_app


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "frontend"
    (dist / "_nuxt" / "builds").mkdir(parents=True)
    (dist / "index.html").write_text('<!doctype html><div id="__nuxt">Nuxt shell</div>')
    (dist / "brand.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (dist / "_nuxt" / "app.abc123.js").write_text('console.log("nuxt")')
    (dist / "_nuxt" / "builds" / "latest.json").write_text('{"id":"test"}')
    app = create_app(auth_code="test-nuxt-auth")
    app.config.update(TESTING=True, NUXT_DIST_DIR=dist)
    return app.test_client()


def login(client):
    response = client.post('/api/auth/login', json={'auth_code': 'test-nuxt-auth', 'remember': True})
    assert response.status_code == 200
    assert response.json['authenticated'] is True
    return response


def test_public_shells_and_chunks_do_not_require_login(client):
    for path in ['/login', '/redeem']:
        response = client.get(path)
        assert response.status_code == 200
        assert b'__nuxt' in response.data
        assert response.headers['Cache-Control'] == 'no-store'
    response = client.get('/_nuxt/app.abc123.js')
    assert response.status_code == 200
    assert 'immutable' in response.headers['Cache-Control']
    assert response.mimetype in ('text/javascript', 'application/javascript')
    assert client.get('/brand.svg').status_code == 200
    assert client.get('/_nuxt/../auth.py').status_code == 404
    assert client.get('/_nuxt/missing.js').status_code == 404
    assert client.get('/_nuxt/app.js.map').status_code == 404
    assert 'immutable' not in client.get('/_nuxt/builds/latest.json').headers['Cache-Control']


def test_admin_deep_links_preserve_auth_boundary(client):
    for path in ['/', '/accounts', '/tasks', '/mailboxes', '/codex', '/redemptions', '/settings', '/providers']:
        response = client.get(path)
        assert response.status_code == 302
        assert '/login?next=' in response.headers['Location']
    assert client.get('/api/does-not-exist').status_code == 401
    login(client)
    for path in ['/', '/accounts', '/accounts/', '/tasks', '/mailboxes', '/codex', '/redemptions', '/settings', '/providers']:
        response = client.get(path)
        assert response.status_code == 200
        assert b'Nuxt shell' in response.data
    assert client.get('/api/does-not-exist').status_code == 404
    assert client.get('/this-is-not-a-page').status_code == 404


def test_json_login_validation_and_logout(client):
    assert client.get('/api/auth/session').json == {'ok': True, 'authenticated': False}
    assert client.post('/api/auth/login', json=[]).status_code == 400
    assert client.post('/api/auth/login', json={'auth_code': 42}).status_code == 400
    assert client.post('/api/auth/login', json={'auth_code': 'wrong'}).status_code == 401
    response = login(client)
    assert 'HttpOnly' in response.headers['Set-Cookie']
    assert 'SameSite=Lax' in response.headers['Set-Cookie']
    assert 'Expires=' in response.headers['Set-Cookie']
    assert client.get('/api/auth/session').json['authenticated'] is True
    assert 'no-store' in client.get('/api/auth/session').headers['Cache-Control']
    assert client.post('/api/auth/logout').json['ok'] is True
    assert client.get('/api/auth/session').json['authenticated'] is False
    assert client.get('/api/summary').status_code == 401


def test_missing_build_explains_how_to_generate(client, tmp_path):
    client.application.config['NUXT_DIST_DIR'] = tmp_path / 'not-built'
    response = client.get('/login')
    assert response.status_code == 503
    assert b'npm --prefix frontend run build' in response.data


def test_explicit_frontend_switches_are_available(client):
    login(client)
    nuxt = client.get("/?ui=nuxt")
    assert nuxt.status_code == 200
    assert b"Nuxt shell" in nuxt.data

    modern = client.get("/?ui=modern")
    assert modern.status_code == 200
    assert b"console.js" in modern.data
    assert b"Nuxt" in modern.data
    assert b"ui=legacy" in modern.data

    legacy = client.get("/?ui=legacy")
    assert legacy.status_code == 200
    assert b"Nuxt" in legacy.data
    assert b"ui=modern" in legacy.data
    client.set_cookie("ui_mode", "legacy")
    assert b"Nuxt shell" in client.get("/").data


