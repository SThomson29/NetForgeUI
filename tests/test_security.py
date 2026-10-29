"""
App-wide hardening: the secret key, cross-site request checks, session
cookie flags and login throttling.
"""

import os

import pytest
import yaml

from app.security import (
    KNOWN_WEAK_KEYS, InsecureSecretKey, LoginThrottle,
    check_secret_key, install_origin_check, origin_allowed,
)


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------

class TestSecretKey:
    """Flask signs the session cookie with SECRET_KEY; knowing it means
    being able to forge a login as anyone."""

    @pytest.mark.parametrize('key', sorted(KNOWN_WEAK_KEYS))
    def test_published_keys_are_refused(self, key):
        with pytest.raises(InsecureSecretKey):
            check_secret_key(key)

    def test_the_key_once_committed_is_refused(self):
        with pytest.raises(InsecureSecretKey):
            check_secret_key('XdfLjM@Ug%x69q7l81c1')

    def test_short_key_is_refused(self):
        with pytest.raises(InsecureSecretKey):
            check_secret_key('abc123')

    def test_none_is_refused(self):
        with pytest.raises(InsecureSecretKey):
            check_secret_key(None)

    def test_a_real_key_is_accepted(self):
        import secrets
        check_secret_key(secrets.token_urlsafe(48))

    def test_app_will_not_start_with_the_default(self, monkeypatch):
        # Patch the class create_app actually reads — another test reloads
        # the config module, which leaves a different Config behind.
        import app as app_pkg
        monkeypatch.setattr(app_pkg.Config, 'SECRET_KEY', 'change-me-in-production')
        with pytest.raises(InsecureSecretKey):
            app_pkg.create_app()

    def test_app_starts_with_a_real_key(self, monkeypatch, tmp_path):
        """The counterpart, so the refusal above cannot pass vacuously."""
        import secrets
        import app as app_pkg
        monkeypatch.setattr(app_pkg.Config, 'SECRET_KEY', secrets.token_urlsafe(48))
        monkeypatch.setattr(app_pkg.Config, 'DATABASE', str(tmp_path / 'u.db'))
        monkeypatch.setattr(app_pkg.Config, 'DATA_DIR', str(tmp_path / 'users'))
        assert app_pkg.create_app() is not None

    def test_no_key_is_committed_to_the_compose_files(self):
        """The compose files take it from the environment instead."""
        for name in ('docker-compose.yml', 'docker-compose.ghcr.yml'):
            with open(name) as f:
                doc = yaml.safe_load(f)
            for svc in (doc.get('services') or {}).values():
                env = svc.get('environment') or {}
                if isinstance(env, list):
                    env = dict(e.split('=', 1) for e in env if '=' in e)
                value = str(env.get('SECRET_KEY', ''))
                if value:
                    assert value.startswith('${SECRET_KEY'), (name, value)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

class TestOriginAllowed:

    OWN = ['netforge.lan:5000']

    def test_same_origin_is_allowed(self):
        assert origin_allowed('http://netforge.lan:5000', None, self.OWN, [])

    def test_cross_site_origin_is_refused(self):
        assert not origin_allowed('https://evil.example', None, self.OWN, [])

    def test_null_origin_is_refused(self):
        assert not origin_allowed('null', None, self.OWN, [])

    def test_lookalike_host_is_refused(self):
        assert not origin_allowed('http://netforge.lan:5000.evil.example',
                                  None, self.OWN, [])

    def test_referer_is_used_when_origin_is_absent(self):
        assert origin_allowed(None, 'http://netforge.lan:5000/projects', self.OWN, [])
        assert not origin_allowed(None, 'https://evil.example/x', self.OWN, [])

    def test_non_browser_clients_are_left_to_authentication(self):
        """No Origin and no Referer means not a browser page; CSRF needs one."""
        assert origin_allowed(None, None, self.OWN, [])

    def test_trusted_origin_for_a_reverse_proxy(self):
        assert origin_allowed('https://netforge.example.com', None, self.OWN,
                              ['https://netforge.example.com'])


@pytest.fixture
def guarded_client(app, client):
    """The test app is built without create_app, so add the check here —
    before the first request, after which Flask locks setup."""
    from app.models import User
    from app.utils import ensure_workspace
    install_origin_check(app)
    with app.app_context():
        User.create('admin', 'testpass', app, is_admin=True)
        ensure_workspace(app, 'admin')
    client.post('/login', data={'username': 'admin', 'password': 'testpass'},
                headers={'Origin': 'http://localhost'})
    return client


class TestOriginCheckOnRequests:

    def _project(self, app):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'csrf')

    def test_cross_site_delete_is_refused(self, app, guarded_client):
        self._project(app)
        res = guarded_client.post('/projects/admin/csrf/delete',
                                  headers={'Origin': 'https://evil.example'})
        assert res.status_code == 403
        from app.project import project_dir
        with app.app_context():
            assert os.path.isdir(project_dir(app, 'admin', 'csrf'))

    def test_same_site_delete_still_works(self, app, guarded_client):
        self._project(app)
        res = guarded_client.post('/projects/admin/csrf/delete',
                                  headers={'Origin': 'http://localhost'})
        assert res.status_code in (200, 302)

    def test_cross_site_json_write_is_refused(self, app, guarded_client):
        self._project(app)
        res = guarded_client.post('/projects/admin/csrf/api/pools',
                                  json={'name': 'x', 'type': 'unique',
                                        'subnet': '10.9.0.0/24', 'prefix': '32'},
                                  headers={'Origin': 'https://evil.example'})
        assert res.status_code == 403

    def test_reads_are_not_affected(self, app, guarded_client):
        self._project(app)
        res = guarded_client.get('/projects/admin/csrf/hosts',
                                 headers={'Origin': 'https://evil.example'})
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

class TestCookieFlags:

    def test_samesite_and_httponly_are_set(self):
        from config import Config
        assert Config.SESSION_COOKIE_SAMESITE == 'Lax'
        assert Config.SESSION_COOKIE_HTTPONLY is True
        assert Config.REMEMBER_COOKIE_SAMESITE == 'Lax'
        assert Config.REMEMBER_COOKIE_HTTPONLY is True

    def test_secure_flag_is_driven_by_the_environment(self, monkeypatch):
        """Default off, so plain-HTTP internal installs can still log in."""
        import importlib
        import config
        monkeypatch.setenv('SESSION_COOKIE_SECURE', 'true')
        importlib.reload(config)
        try:
            assert config.Config.SESSION_COOKIE_SECURE is True
        finally:
            monkeypatch.delenv('SESSION_COOKIE_SECURE')
            importlib.reload(config)
        assert config.Config.SESSION_COOKIE_SECURE is False


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestLoginThrottle:

    def test_blocks_after_the_limit(self):
        t = LoginThrottle(max_failures=3, window_seconds=60, clock=FakeClock())
        for _ in range(3):
            assert not t.blocked('1.2.3.4', 'admin')
            t.record_failure('1.2.3.4', 'admin')
        assert t.blocked('1.2.3.4', 'admin')

    def test_window_expires(self):
        clock = FakeClock()
        t = LoginThrottle(max_failures=2, window_seconds=60, clock=clock)
        t.record_failure('a', 'u')
        t.record_failure('a', 'u')
        assert t.blocked('a', 'u')
        clock.now += 61
        assert not t.blocked('a', 'u')

    def test_success_clears_the_account_count(self):
        t = LoginThrottle(max_failures=2, window_seconds=60, clock=FakeClock())
        t.record_failure('a', 'u')
        t.record_success('a', 'u')
        t.record_failure('a', 'u')
        assert not t.blocked('a', 'u')

    def test_username_is_case_insensitive(self):
        t = LoginThrottle(max_failures=2, window_seconds=60, clock=FakeClock())
        t.record_failure('a', 'Admin')
        t.record_failure('a', 'ADMIN')
        assert t.blocked('a', 'admin')

    def test_spraying_many_usernames_from_one_address_is_limited(self):
        t = LoginThrottle(max_failures=2, window_seconds=60, clock=FakeClock())
        for i in range(8):
            t.record_failure('a', 'user%d' % i)
        assert t.blocked('a', 'someone-new')

    def test_other_addresses_are_unaffected(self):
        t = LoginThrottle(max_failures=2, window_seconds=60, clock=FakeClock())
        t.record_failure('a', 'u')
        t.record_failure('a', 'u')
        assert not t.blocked('b', 'u')


class TestLoginRoute:

    def _user(self, app):
        from app.models import User
        with app.app_context():
            User.create('sam', 'right-password', app)

    def test_repeated_failures_are_throttled(self, app, client):
        self._user(app)
        app.config['LOGIN_MAX_FAILURES'] = 3
        for _ in range(3):
            res = client.post('/login', data={'username': 'sam', 'password': 'wrong'})
            assert res.status_code == 200
        res = client.post('/login', data={'username': 'sam', 'password': 'wrong'})
        assert res.status_code == 429

    def test_correct_password_is_not_accepted_while_blocked(self, app, client):
        """Otherwise a block would still confirm a correct guess."""
        self._user(app)
        app.config['LOGIN_MAX_FAILURES'] = 2
        for _ in range(2):
            client.post('/login', data={'username': 'sam', 'password': 'wrong'})
        res = client.post('/login', data={'username': 'sam',
                                          'password': 'right-password'})
        assert res.status_code == 429

    def test_normal_login_still_works(self, app, client):
        self._user(app)
        res = client.post('/login', data={'username': 'sam',
                                          'password': 'right-password'})
        assert res.status_code == 302


# ---------------------------------------------------------------------------
# Behind a reverse proxy
# ---------------------------------------------------------------------------

def _real_app(monkeypatch, tmp_path, proxies):
    import secrets
    import app as app_pkg
    cfg = app_pkg.Config           # the class create_app actually reads
    monkeypatch.setattr(cfg, 'DATABASE', str(tmp_path / 'users.db'))
    monkeypatch.setattr(cfg, 'DATA_DIR', str(tmp_path / 'users'))
    monkeypatch.setattr(cfg, 'SECRET_KEY', secrets.token_urlsafe(48))
    monkeypatch.setattr(cfg, 'TRUSTED_PROXIES', proxies)
    return app_pkg.create_app()


# What Nginx sends with the recommended proxy_set_header lines, where the
# app itself is reached on an internal address.
_NGINX = {
    'Host': '127.0.0.1:5000',
    'X-Forwarded-Host': 'netforge.example.com',
    'X-Forwarded-Proto': 'https',
    'X-Forwarded-For': '203.0.113.7',
    'Origin': 'https://netforge.example.com',
}


class TestBehindNginx:

    def test_login_works_through_the_proxy(self, monkeypatch, tmp_path):
        """The public Origin must match the forwarded host, not the upstream."""
        app = _real_app(monkeypatch, tmp_path, proxies=1)
        res = app.test_client().post('/login', headers=_NGINX,
                                     data={'username': 'x', 'password': 'y'})
        assert res.status_code == 200

    def test_cross_site_is_still_refused_through_the_proxy(self, monkeypatch, tmp_path):
        app = _real_app(monkeypatch, tmp_path, proxies=1)
        headers = dict(_NGINX, Origin='https://evil.example')
        res = app.test_client().post('/login', headers=headers,
                                     data={'username': 'x', 'password': 'y'})
        assert res.status_code == 403

    def test_throttle_sees_the_real_client(self, monkeypatch, tmp_path):
        """Otherwise every user shares Nginx's address and one client's
        failures lock out everyone."""
        app = _real_app(monkeypatch, tmp_path, proxies=1)
        app.config['LOGIN_MAX_FAILURES'] = 2
        c = app.test_client()
        for _ in range(2):
            c.post('/login', headers=_NGINX, data={'username': 'a', 'password': 'bad'})
        blocked = c.post('/login', headers=_NGINX,
                         data={'username': 'a', 'password': 'bad'})
        assert blocked.status_code == 429

        other = dict(_NGINX, **{'X-Forwarded-For': '198.51.100.9'})
        res = c.post('/login', headers=other, data={'username': 'a', 'password': 'bad'})
        assert res.status_code == 200, 'a different client was caught by the block'

    def test_forwarded_headers_are_ignored_without_a_proxy(self, monkeypatch, tmp_path):
        """Served directly, a client must not be able to choose its address."""
        app = _real_app(monkeypatch, tmp_path, proxies=0)
        app.config['LOGIN_MAX_FAILURES'] = 2
        c = app.test_client()
        for spoof in ('1.1.1.1', '2.2.2.2', '3.3.3.3'):
            res = c.post('/login', headers={'X-Forwarded-For': spoof},
                         data={'username': 'a', 'password': 'bad'})
        assert res.status_code == 429, 'rotating X-Forwarded-For escaped the throttle'


# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------

def _directives(csp):
    return {d.split()[0]: d.split()[1:] for d in csp.split('; ')}


class TestResponseHeaders:

    def test_every_response_carries_them(self, monkeypatch, tmp_path):
        app = _real_app(monkeypatch, tmp_path, proxies=0)
        for url in ('/login', '/healthz', '/static/css/design.css', '/no-such-page'):
            h = app.test_client().get(url).headers
            assert 'Content-Security-Policy' in h, url
            assert h['X-Content-Type-Options'] == 'nosniff', url
            assert h['X-Frame-Options'] == 'DENY', url
            assert h['Referrer-Policy'] == 'same-origin', url

    def test_policy_blocks_framing_plugins_and_base_tags(self):
        from app.security import content_security_policy
        d = _directives(content_security_policy({}))
        assert d['frame-ancestors'] == ["'none'"]
        assert d['object-src'] == ["'none'"]
        assert d['base-uri'] == ["'self'"]
        assert d['form-action'] == ["'self'"]
        assert d['default-src'] == ["'self'"]

    def test_google_fonts_are_allowed(self):
        """design.css imports them; without these the UI falls back to system fonts."""
        from app.security import content_security_policy
        d = _directives(content_security_policy({}))
        assert 'https://fonts.googleapis.com' in d['style-src']
        assert 'https://fonts.gstatic.com' in d['font-src']

    def test_provider_logout_may_leave_the_site(self):
        """Sign-out POSTs here and redirects to the provider; browsers check
        that redirect against form-action."""
        from app.security import content_security_policy
        d = _directives(content_security_policy({
            'OIDC_ISSUER': 'https://Auth.Example.com/application/o/netforge/',
            'OIDC_LOGOUT': 'provider'}))
        assert d['form-action'] == ["'self'", 'https://auth.example.com']

    def test_local_logout_keeps_forms_on_the_site(self):
        from app.security import content_security_policy
        d = _directives(content_security_policy({
            'OIDC_ISSUER': 'https://auth.example.com/', 'OIDC_LOGOUT': 'local'}))
        assert d['form-action'] == ["'self'"]
