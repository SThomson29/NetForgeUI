"""
OIDC single sign-on.

Authlib's protocol handling is trusted and stubbed at the boundary; what is
tested here is NetForgeUI's part — who gets in, which account they land in,
admin by group, the password fallback for local admins, and linking.
"""

import os
import sqlite3
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs

import pytest

from app import sso
from app.models import User, init_db
from app.utils import ensure_workspace, user_dir

SSO_CONFIG = dict(
    OIDC_ISSUER='https://idp.example.com/realms/corp',
    OIDC_CLIENT_ID='netforge',
    OIDC_CLIENT_SECRET='s3cret',
    OIDC_ALLOWED_GROUP='netforge-users',
    OIDC_ADMIN_GROUP='netforge-admins',
    OIDC_GROUPS_CLAIM='groups',
    OIDC_SCOPES='openid email profile',
    OIDC_REDIRECT_URI='',
    OIDC_BUTTON_LABEL='Sign in with Corp SSO',
)


@pytest.fixture
def sso_app(app):
    app.config.update(SSO_CONFIG)
    sso.init_sso(app)
    app.register_blueprint(sso.sso_bp)
    client = app.extensions['netforge_oidc']
    # Discovery as if already fetched, so nothing goes to the network.
    client.server_metadata.update({
        '_loaded_at': 1,
        'issuer': SSO_CONFIG['OIDC_ISSUER'],
        'authorization_endpoint': 'https://idp.example.com/auth',
        'token_endpoint': 'https://idp.example.com/token',
        'jwks_uri': 'https://idp.example.com/certs',
    })
    return app


def claims(sub='sub-1', email='sam.thomson@corp.com', groups=('netforge-users',), **extra):
    c = {'sub': sub, 'email': email, 'groups': list(groups)}
    c.update(extra)
    return c


def callback(app, client, c):
    """The provider redirecting back with a token carrying claims c."""
    oidc = app.extensions['netforge_oidc']
    with patch.object(oidc, 'authorize_access_token', return_value={'userinfo': c}), \
         patch.object(oidc, 'userinfo', return_value={}):
        return client.get('/login/sso/callback')


def whoami(client):
    res = client.get('/account')
    return res.status_code, res.get_data(as_text=True)


class TestSettings:

    def test_off_without_an_issuer(self, app):
        sso.init_sso(app)
        assert not sso.enabled(app) and 'netforge_oidc' not in app.extensions

    @pytest.mark.parametrize('missing', ['OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET',
                                         'OIDC_ALLOWED_GROUP'])
    def test_half_configured_refuses_to_start(self, app, missing):
        app.config.update(SSO_CONFIG, **{missing: ''})
        with pytest.raises(RuntimeError, match=missing):
            sso.init_sso(app)

    def test_routes_404_when_off(self, app, client):
        app.register_blueprint(sso.sso_bp)
        assert client.get('/login/sso').status_code == 404

    def test_redirect_uses_pkce_state_and_nonce(self, sso_app, client):
        res = client.get('/login/sso')
        assert res.status_code == 302
        url = urlparse(res.headers['Location'])
        q = parse_qs(url.query)
        assert url.netloc == 'idp.example.com'
        assert q['client_id'] == ['netforge'] and q['response_type'] == ['code']
        assert q['code_challenge_method'] == ['S256'] and q['code_challenge']
        assert q['state'] and q['nonce']
        assert q['redirect_uri'] == ['http://localhost/login/sso/callback']
        assert 'openid' in q['scope'][0]

    @pytest.mark.parametrize('exc, says', [
        ('SSLError', "certificate isn't trusted"),
        ('ConnectionError', "can't be reached"),
        ('ReadTimeout', 'did not answer in time'),
    ])
    def test_provider_unavailable_is_not_a_500(self, sso_app, client, exc, says):
        import requests
        oidc = sso_app.extensions['netforge_oidc']
        with patch.object(oidc, 'authorize_redirect',
                          side_effect=getattr(requests.exceptions, exc)('boom')):
            res = client.get('/login/sso')
        assert res.status_code == 302 and res.headers['Location'].endswith('/login')
        body = client.get('/login').get_data(as_text=True)
        assert says.replace("'", '&#39;') in body
        [e] = sso.recent_events(sso_app)
        assert e['outcome'] == 'failed' and says in e['detail']

    def test_redirect_uri_override(self, sso_app, client):
        sso_app.config['OIDC_REDIRECT_URI'] = 'https://netforge.corp/login/sso/callback'
        q = parse_qs(urlparse(client.get('/login/sso').headers['Location']).query)
        assert q['redirect_uri'] == ['https://netforge.corp/login/sso/callback']


class TestWhoGetsIn:

    def test_first_sign_in_creates_an_account(self, sso_app, client):
        res = callback(sso_app, client, claims())
        assert res.status_code == 302 and res.headers['Location'].endswith('/projects')
        user = User.get_by_username('sam-thomson', sso_app)
        assert user.source == 'sso' and user.email == 'sam.thomson@corp.com'
        assert not user.is_admin
        assert os.path.isdir(user_dir(sso_app, 'sam-thomson'))
        status, body = whoami(client)
        assert status == 200 and 'sam.thomson@corp.com' in body
        assert 'Change password' not in body

    def test_not_in_the_allowed_group(self, sso_app, client):
        res = callback(sso_app, client, claims(groups=['someone-else']))
        assert res.headers['Location'].endswith('/login')
        assert User.get_by_username('sam-thomson', sso_app) is None
        assert 'not in the group' in client.get('/login').get_data(as_text=True)

    def test_no_groups_at_all(self, sso_app, client):
        c = claims()
        del c['groups']
        callback(sso_app, client, c)
        assert User.get_by_username('sam-thomson', sso_app) is None

    def test_keycloak_group_paths(self, sso_app, client):
        callback(sso_app, client, claims(groups=['/netforge-users']))
        assert User.get_by_username('sam-thomson', sso_app) is not None

    def test_entra_group_overage_is_explained(self, sso_app, client):
        c = claims()
        del c['groups']
        c['_claim_names'] = {'groups': 'src1'}
        callback(sso_app, client, c)
        assert 'too many groups' in client.get('/login').get_data(as_text=True)

    def test_groups_from_userinfo_when_not_in_the_id_token(self, sso_app, client):
        c = claims()
        del c['groups']
        oidc = sso_app.extensions['netforge_oidc']
        with patch.object(oidc, 'authorize_access_token', return_value={'userinfo': c}), \
             patch.object(oidc, 'userinfo',
                          return_value={'sub': 'sub-1', 'groups': ['netforge-users']}):
            client.get('/login/sso/callback')
        assert User.get_by_username('sam-thomson', sso_app) is not None

    def test_provider_error_goes_back_to_login(self, sso_app, client):
        from authlib.integrations.base_client import OAuthError
        oidc = sso_app.extensions['netforge_oidc']
        with patch.object(oidc, 'authorize_access_token',
                          side_effect=OAuthError('access_denied', 'User cancelled')):
            res = client.get('/login/sso/callback')
        assert res.headers['Location'].endswith('/login')
        assert 'User cancelled' in client.get('/login').get_data(as_text=True)

    def test_upn_when_there_is_no_email_claim(self, sso_app, client):
        c = claims(email=None, preferred_username='Jo.Bloggs@Corp.com')
        callback(sso_app, client, c)
        assert User.get_by_username('jo-bloggs', sso_app).email == 'jo.bloggs@corp.com'


    def test_username_when_there_is_no_email(self, sso_app, client):
        callback(sso_app, client, claims(email=None, preferred_username='Jo.Bloggs'))
        user = User.get_by_username('jo-bloggs', sso_app)
        assert user.source == 'sso' and user.email is None
        assert whoami(client)[0] == 200
        [e] = sso.recent_events(sso_app)
        assert e['outcome'] == 'created' and e['email'] == 'Jo.Bloggs'

    def test_username_clash_gets_a_suffix_not_the_account(self, sso_app, client):
        User.create('admin2', 'password1', sso_app, is_admin=True)
        callback(sso_app, client, claims(email=None, preferred_username='admin2'))
        assert User.get_by_sub('sub-1', sso_app).username == 'admin2-2'
        assert User.get_by_username('admin2', sso_app).oidc_sub is None

    def test_neither_email_nor_username(self, sso_app, client):
        callback(sso_app, client, claims(email=None))
        assert 'neither an email address nor a username' in \
            client.get('/login').get_data(as_text=True)
        assert User.get_by_sub('sub-1', sso_app) is None

    def test_refusal_records_the_username(self, sso_app, client):
        callback(sso_app, client, claims(email=None, preferred_username='jo', groups=[]))
        [e] = sso.recent_events(sso_app)
        assert e['outcome'] == 'refused' and e['email'] == 'jo'


class TestAccounts:

    def test_same_subject_same_account_even_if_email_changes(self, sso_app, client):
        callback(sso_app, client, claims())
        client.post('/logout')
        callback(sso_app, client, claims(email='sam.t@newcorp.com'))
        users = [u for u in User.all_users(sso_app) if u.source == 'sso']
        assert [u.username for u in users] == ['sam-thomson']
        assert users[0].email == 'sam.t@newcorp.com'

    def test_name_clash_gets_a_suffix(self, sso_app, client):
        User.create('sam-thomson', 'password1', sso_app)
        callback(sso_app, client, claims())
        assert User.get_by_sub('sub-1', sso_app).username == 'sam-thomson-2'

    @pytest.mark.parametrize('email, name', [
        ('sam.thomson@corp.com', 'sam-thomson'),
        ("o'brien+net@corp.com", 'o-brien-net'),
        ('..@corp.com', 'user'),
        ('UPPER_case@corp.com', 'upper_case'),
    ])
    def test_username_from_email(self, email, name):
        assert sso.username_for(email, lambda n: False) == name

    def test_admin_group_granted_and_revoked_each_sign_in(self, sso_app, client):
        callback(sso_app, client, claims(groups=['netforge-users', 'netforge-admins']))
        assert client.get('/admin').status_code == 200
        client.post('/logout')
        callback(sso_app, client, claims())
        assert client.get('/admin').status_code == 403

    def test_sso_never_removes_a_locally_granted_admin(self, sso_app, client):
        User.create('sam', 'password1', sso_app, is_admin=True)
        User.set_link_email(User.get_by_username('sam', sso_app).id, 'sam.thomson@corp.com', sso_app)
        callback(sso_app, client, claims())
        assert User.get_by_username('sam', sso_app).is_admin


class TestPasswordSignIn:

    def _login(self, client, user, pw='password1'):
        return client.post('/login', data={'username': user, 'password': pw})

    def test_local_admin_still_can(self, sso_app, client):
        User.create('breakglass', 'password1', sso_app, is_admin=True)
        assert self._login(client, 'breakglass').headers['Location'].endswith('/projects')
        # Admins aren't limited to the admin pages.
        assert client.post('/projects/new', data={'name': 'p1'}).status_code in (200, 302)
        assert os.path.isdir(os.path.join(user_dir(sso_app, 'breakglass'), 'projects', 'p1'))

    def test_local_non_admin_cannot(self, sso_app, client):
        User.create('bob', 'password1', sso_app)
        res = self._login(client, 'bob')
        assert res.status_code == 200
        assert 'Invalid username or password' in res.get_data(as_text=True)

    def test_sso_admin_has_no_password_route(self, sso_app, client):
        callback(sso_app, client, claims(groups=['netforge-users', 'netforge-admins']))
        assert client.post('/account', data={'current_password': 'x', 'new_password': 'y' * 8,
                                              'confirm_password': 'y' * 8}).status_code == 403

    def test_everyone_can_without_sso(self, app, client):
        User.create('bob', 'password1', app)
        assert self._login(client, 'bob').headers['Location'].endswith('/projects')

    def test_login_page_shows_the_button(self, sso_app, client):
        body = client.get('/login').get_data(as_text=True)
        assert 'Sign in with Corp SSO' in body and '/login/sso' in body

    def test_login_page_without_sso(self, app, client):
        assert '/login/sso' not in client.get('/login').get_data(as_text=True)


class TestLinking:

    @pytest.fixture
    def admin(self, sso_app, client):
        User.create('root', 'password1', sso_app, is_admin=True)
        client.post('/login', data={'username': 'root', 'password': 'password1'})
        return client

    def test_link_before_first_sign_in(self, sso_app, admin, client):
        User.create('sam', 'password1', sso_app)
        ensure_workspace(sso_app, 'sam')
        sam = User.get_by_username('sam', sso_app)
        admin.post('/admin/link/%d' % sam.id, data={'email': 'Sam.Thomson@corp.com'})
        assert User.get(sam.id, sso_app).link_email == 'sam.thomson@corp.com'
        assert 'link pending' in admin.get('/admin').get_data(as_text=True)

        admin.post('/logout')
        callback(sso_app, client, claims())
        linked = User.get(sam.id, sso_app)
        assert linked.oidc_sub == 'sub-1' and linked.link_email is None
        assert User.get_by_username('sam-thomson', sso_app) is None
        status, body = whoami(client)
        assert 'Change password' in body       # still a local account

    def test_unverified_email_does_not_link(self, sso_app, admin, client):
        User.create('sam', 'password1', sso_app)
        sam = User.get_by_username('sam', sso_app)
        admin.post('/admin/link/%d' % sam.id, data={'email': 'sam.thomson@corp.com'})
        admin.post('/logout')
        callback(sso_app, client, claims(email_verified=False))
        assert User.get(sam.id, sso_app).oidc_sub is None
        assert User.get_by_sub('sub-1', sso_app).username == 'sam-thomson'

    def test_link_after_an_empty_sso_account_was_made(self, sso_app, admin, client):
        from flask.testing import FlaskClient
        other = FlaskClient(sso_app, use_cookies=True)
        callback(sso_app, other, claims())
        User.create('sam', 'password1', sso_app)
        sam = User.get_by_username('sam', sso_app)
        admin.post('/admin/link/%d' % sam.id, data={'email': 'sam.thomson@corp.com'})
        assert User.get(sam.id, sso_app).oidc_sub == 'sub-1'
        assert User.get_by_username('sam-thomson', sso_app) is None
        assert not os.path.isdir(user_dir(sso_app, 'sam-thomson'))
        # The old session belonged to the removed account.
        assert other.get('/account').status_code in (302, 401)

    def test_link_by_sso_account_name_when_there_is_no_email(self, sso_app, admin, client):
        from flask.testing import FlaskClient
        callback(sso_app, FlaskClient(sso_app, use_cookies=True),
                 claims(email=None, preferred_username='jo'))
        User.create('joanne', 'password1', sso_app)
        jo = User.get_by_username('joanne', sso_app)
        admin.post('/admin/link/%d' % jo.id, data={'email': 'JO'})
        assert User.get(jo.id, sso_app).oidc_sub == 'sub-1'
        assert User.get_by_username('jo', sso_app) is None

    @pytest.mark.parametrize('name', ['nobody', 'admin'])
    def test_link_by_name_needs_an_existing_sso_account(self, sso_app, admin, client, name):
        User.create('joanne', 'password1', sso_app)
        jo = User.get_by_username('joanne', sso_app)
        body = admin.post('/admin/link/%d' % jo.id, data={'email': name},
                          follow_redirects=True).get_data(as_text=True)
        assert 'There is no SSO account called %s' % name in body
        assert User.get(jo.id, sso_app).oidc_sub is None
        assert User.get(jo.id, sso_app).link_email is None

    def test_link_refused_when_the_sso_account_has_projects(self, sso_app, admin, client):
        from app.project import create_project
        from flask.testing import FlaskClient
        callback(sso_app, FlaskClient(sso_app, use_cookies=True), claims())
        create_project(sso_app, 'sam-thomson', 'work')
        User.create('sam', 'password1', sso_app)
        sam = User.get_by_username('sam', sso_app)
        body = admin.post('/admin/link/%d' % sam.id, data={'email': 'sam.thomson@corp.com'},
                          follow_redirects=True).get_data(as_text=True)
        assert 'has projects' in body
        assert User.get(sam.id, sso_app).oidc_sub is None

    def test_bad_email_and_sso_accounts_are_refused(self, sso_app, admin, client):
        User.create('sam', 'password1', sso_app)
        sam = User.get_by_username('sam', sso_app)
        admin.post('/admin/link/%d' % sam.id, data={'email': 'not-an-email'})
        assert User.get(sam.id, sso_app).link_email is None
        callback(sso_app, client, claims(sub='s2', email='x@corp.com'))
        x = User.get_by_sub('s2', sso_app)
        admin.post('/admin/link/%d' % x.id, data={'email': 'y@corp.com'})
        assert User.get(x.id, sso_app).link_email is None

    def test_unlink_ends_sessions(self, sso_app, admin, client):
        User.create('sam', 'password1', sso_app, is_admin=True)
        sam = User.get_by_username('sam', sso_app)
        User.set_link_email(sam.id, 'sam.thomson@corp.com', sso_app)
        from flask.testing import FlaskClient
        other = FlaskClient(sso_app, use_cookies=True)
        callback(sso_app, other, claims())
        assert other.get('/account').status_code == 200
        admin.post('/admin/unlink/%d' % sam.id)
        assert User.get(sam.id, sso_app).oidc_sub is None
        assert other.get('/account').status_code in (302, 401)

    def test_non_admin_cannot_link(self, sso_app, client):
        callback(sso_app, client, claims())
        assert client.post('/admin/link/1', data={'email': 'a@b.com'}).status_code == 403


class TestMigration:

    def test_old_database_gains_the_columns(self, app, tmp_path):
        db = tmp_path / 'old.db'
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, '
                    'is_admin INTEGER NOT NULL DEFAULT 0)')
        con.execute("INSERT INTO users (username, password, is_admin) VALUES ('old', 'x', 1)")
        con.commit()
        con.close()
        app.config['DATABASE'] = str(db)
        init_db(app)
        init_db(app)
        u = User.get_by_username('old', app)
        assert u.source == 'local' and u.is_admin and u.oidc_sub is None

    def test_subjects_are_unique(self, app):
        User.create_sso('a', 'same-sub', app)
        with pytest.raises(sqlite3.IntegrityError):
            User.create_sso('b', 'same-sub', app)


# ---------------------------------------------------------------------------
# End to end against a real (local) identity provider: discovery, the code
# exchange, and Authlib's checks of the ID token's signature, nonce,
# audience and issuer — the part that must not be stubbed to be trusted.
# ---------------------------------------------------------------------------

import json
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeIdP:

    def __init__(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            from authlib.jose import JsonWebKey, jwt
        self._jwt = jwt
        self.key = JsonWebKey.generate_key('RSA', 2048, is_private=True, options={'kid': 'k1'})
        self.forger = JsonWebKey.generate_key('RSA', 2048, is_private=True, options={'kid': 'k1'})
        self.nonce = None
        self.claims = {}
        self.tamper = {}            # overrides for the next ID token
        self.sign_with = self.key
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith('/.well-known/openid-configuration'):
                    self._json({'issuer': idp.issuer,
                                'authorization_endpoint': idp.issuer + '/auth',
                                'token_endpoint': idp.issuer + '/token',
                                'userinfo_endpoint': idp.issuer + '/userinfo',
                                'jwks_uri': idp.issuer + '/jwks',
                                'id_token_signing_alg_values_supported': ['RS256']})
                elif self.path.endswith('/jwks'):
                    self._json({'keys': [idp.key.as_dict(is_private=False)]})
                elif self.path.endswith('/userinfo'):
                    self._json({'sub': idp.claims.get('sub')})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                now = int(time.time())
                payload = {'iss': idp.issuer, 'aud': 'netforge', 'iat': now,
                           'exp': now + 300, 'nonce': idp.nonce, **idp.claims}
                payload.update(idp.tamper)
                token = idp._jwt.encode({'alg': 'RS256', 'kid': 'k1'}, payload,
                                        idp.sign_with).decode()
                self._json({'access_token': 'at', 'token_type': 'Bearer',
                            'expires_in': 300, 'id_token': token})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.issuer = 'http://127.0.0.1:%d/realms/corp' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def idp():
    i = FakeIdP()
    yield i
    i.close()


@pytest.fixture
def live_app(app, idp):
    app.config.update(SSO_CONFIG, OIDC_ISSUER=idp.issuer)
    sso.init_sso(app)
    app.register_blueprint(sso.sso_bp)
    return app


def sign_in_live(client, idp, claims_):
    """Browser round trip: to the provider, and back with a code."""
    idp.claims = claims_
    q = parse_qs(urlparse(client.get('/login/sso').headers['Location']).query)
    idp.nonce = q['nonce'][0]
    return client.get('/login/sso/callback?code=abc&state=' + q['state'][0])


class TestEndToEnd:

    def test_valid_token_signs_in(self, live_app, client, idp):
        res = sign_in_live(client, idp, claims())
        assert res.headers['Location'].endswith('/projects')
        assert User.get_by_sub('sub-1', live_app).username == 'sam-thomson'

    @pytest.mark.parametrize('what, tamper', [
        ('forged signature', 'forge'),
        ('wrong audience', {'aud': 'some-other-app'}),
        ('wrong issuer', {'iss': 'https://evil.example.com'}),
        ('replayed nonce', {'nonce': 'not-the-one-we-sent'}),
        ('expired', {'exp': 1000, 'iat': 900}),
    ])
    def test_bad_tokens_are_refused(self, live_app, client, idp, what, tamper):
        if tamper == 'forge':
            idp.sign_with = idp.forger
        else:
            idp.tamper = tamper
        res = sign_in_live(client, idp, claims())
        assert res.headers['Location'].endswith('/login'), what
        assert User.get_by_sub('sub-1', live_app) is None, what

    def test_state_must_match(self, live_app, client, idp):
        idp.claims = claims()
        q = parse_qs(urlparse(client.get('/login/sso').headers['Location']).query)
        idp.nonce = q['nonce'][0]
        res = client.get('/login/sso/callback?code=abc&state=attacker-state')
        assert res.headers['Location'].endswith('/login')
        assert User.get_by_sub('sub-1', live_app) is None

    def test_callback_without_starting_is_refused(self, live_app, client, idp):
        idp.claims = claims()
        res = client.get('/login/sso/callback?code=abc&state=x')
        assert res.headers['Location'].endswith('/login')


class TestAdminPage:
    """Admin → Single sign-on: settings read-only, discovery test, and
    recent sign-ins with what the provider sent on a refusal."""

    def admin(self, app, client, base_url='http://localhost'):
        User.create('boss', 'password1', app, is_admin=True)
        client.post('/login', data={'username': 'boss', 'password': 'password1'},
                    base_url=base_url)

    def test_off_says_how_to_turn_it_on(self, auth_client):
        body = auth_client.get('/admin/sso').get_data(as_text=True)
        assert 'OIDC_ISSUER' in body and 'Test connection' not in body
        assert 'href="/admin/sso"' in body

    def test_shows_settings_but_never_the_secret(self, sso_app, client):
        self.admin(sso_app, client)
        body = client.get('/admin/sso').get_data(as_text=True)
        assert SSO_CONFIG['OIDC_ISSUER'] in body and 'netforge-users' in body
        assert 'netforge-admins' in body and '>set<' in body
        assert 's3cret' not in body
        assert 'http://localhost/login/sso/callback' in body
        assert 'Test connection' in body

    def test_http_behind_a_proxy_is_called_out(self, sso_app, client):
        self.admin(sso_app, client, base_url='http://netforge.corp')
        body = client.get('/admin/sso', base_url='http://netforge.corp').get_data(as_text=True)
        assert 'http://netforge.corp/login/sso/callback' in body
        assert 'TRUSTED_PROXIES' in body
        sso_app.config['OIDC_REDIRECT_URI'] = 'https://netforge.corp/login/sso/callback'
        body = client.get('/admin/sso', base_url='http://netforge.corp').get_data(as_text=True)
        assert 'Set by <code>OIDC_REDIRECT_URI</code>' in body
        assert 'TRUSTED_PROXIES' not in body

    def test_non_admins_get_403(self, sso_app, client):
        User.create('plain', 'password1', sso_app)
        callback(sso_app, client, claims())
        assert client.get('/admin/sso').status_code == 403
        assert client.post('/admin/sso/test').status_code == 403

    def test_sign_ins_and_refusals_are_listed(self, sso_app, client):
        for c in (claims(), claims(),
                  claims(sub='sub-2', email='eve@corp.com', groups=['g-1234', 'other'])):
            callback(sso_app, sso_app.test_client(), c)
        outcomes = [e['outcome'] for e in sso.recent_events(sso_app)]
        assert outcomes == ['refused', 'signed_in', 'created']
        self.admin(sso_app, client)
        body = client.get('/admin/sso').get_data(as_text=True)
        assert 'eve@corp.com' in body and 'Groups sent: g-1234, other' in body
        assert 'not in the group allowed' in body
        assert 'new account' in body and 'sam-thomson' in body

    def test_failures_are_listed(self, sso_app, client):
        from authlib.integrations.base_client import OAuthError
        oidc = sso_app.extensions['netforge_oidc']
        with patch.object(oidc, 'authorize_access_token',
                          side_effect=OAuthError('invalid_client', 'bad secret')):
            client.get('/login/sso/callback')
        [e] = sso.recent_events(sso_app)
        assert e['outcome'] == 'failed' and 'invalid_client' in e['detail']

    def test_only_the_latest_are_kept(self, sso_app):
        for i in range(sso.KEEP_EVENTS + 5):
            sso.record_event(sso_app, 'refused', email='u%d@corp.com' % i)
        events = sso.recent_events(sso_app, limit=1000)
        assert len(events) == sso.KEEP_EVENTS
        assert events[0]['email'] == 'u%d@corp.com' % (sso.KEEP_EVENTS + 4)


class TestDiscoveryCheck:

    META = {
        'issuer': SSO_CONFIG['OIDC_ISSUER'],
        'authorization_endpoint': 'https://idp.example.com/auth',
        'token_endpoint': 'https://idp.example.com/token',
        'userinfo_endpoint': 'https://idp.example.com/userinfo',
        'jwks_uri': 'https://idp.example.com/certs',
        'code_challenge_methods_supported': ['S256'],
        'claims_supported': ['sub', 'email', 'groups'],
    }

    def run(self, app, meta=None, status=200, exc=None):
        import requests
        resp = requests.Response()
        resp.status_code = status
        resp._content = (__import__('json').dumps(meta) if meta is not None else 'nope').encode()
        with patch('requests.get', side_effect=exc, return_value=resp) as get:
            result = sso.check_discovery(app)
        if exc is None:
            assert get.call_args[0][0] == SSO_CONFIG['OIDC_ISSUER'] + '/.well-known/openid-configuration'
        return result

    def test_good(self, sso_app):
        r = self.run(sso_app, self.META)
        assert r['ok'] and not r['problems'] and not r['notes']
        assert r['endpoints']['token_endpoint'] == 'https://idp.example.com/token'

    def test_issuer_mismatch(self, sso_app):
        r = self.run(sso_app, dict(self.META, issuer='https://idp.example.com/realms/other'))
        assert not r['ok'] and 'realms/other' in r['problems'][0]

    def test_trailing_slash_is_not_a_mismatch(self, sso_app):
        assert self.run(sso_app, dict(self.META, issuer=SSO_CONFIG['OIDC_ISSUER'] + '/'))['ok']

    def test_no_pkce_or_endpoints(self, sso_app):
        meta = dict(self.META, code_challenge_methods_supported=['plain'])
        del meta['jwks_uri']
        r = self.run(sso_app, meta)
        assert not r['ok'] and len(r['problems']) == 2

    def test_groups_claim_not_advertised_is_a_note(self, sso_app):
        r = self.run(sso_app, dict(self.META, claims_supported=['sub', 'email']))
        assert r['ok'] and '"groups"' in r['notes'][0]

    def test_http_error_and_not_json(self, sso_app):
        assert '404' in self.run(sso_app, self.META, status=404)['error']
        assert 'not JSON' in self.run(sso_app, None)['error']

    def test_unreachable(self, sso_app):
        import requests
        r = self.run(sso_app, exc=requests.ConnectionError('no route'))
        assert not r['ok'] and 'ConnectionError' in r['error']

    def test_route(self, sso_app, client):
        User.create('boss', 'password1', sso_app, is_admin=True)
        client.post('/login', data={'username': 'boss', 'password': 'password1'})
        with patch.object(sso, 'check_discovery', return_value={'ok': True}):
            assert client.post('/admin/sso/test').get_json() == {'ok': True}

    def test_route_when_off(self, auth_client):
        assert auth_client.post('/admin/sso/test').status_code == 400


class TestLogout:
    """OIDC_LOGOUT: local by default; provider also ends the provider's
    session, for SSO sign-ins only."""

    END = 'https://idp.example.com/end-session/'

    def sign_in(self, app, client, id_token='header.payload.sig'):
        oidc = app.extensions['netforge_oidc']
        with patch.object(oidc, 'authorize_access_token',
                          return_value={'userinfo': claims(), 'id_token': id_token}), \
             patch.object(oidc, 'userinfo', return_value={}):
            client.get('/login/sso/callback')
        assert whoami(client)[0] == 200

    def provider(self, app, endpoint=END):
        app.config['OIDC_LOGOUT'] = 'provider'
        meta = app.extensions['netforge_oidc'].server_metadata
        if endpoint:
            meta['end_session_endpoint'] = endpoint
        else:
            meta.pop('end_session_endpoint', None)

    def test_local_by_default(self, sso_app, client):
        self.sign_in(sso_app, client)
        res = client.post('/logout')
        assert res.headers['Location'].endswith('/login')
        assert whoami(client)[0] in (302, 401)

    def test_provider_logout(self, sso_app, client):
        self.provider(sso_app)
        self.sign_in(sso_app, client)
        res = client.post('/logout')
        url = urlparse(res.headers['Location'])
        q = parse_qs(url.query)
        assert res.headers['Location'].startswith(self.END)
        assert q['id_token_hint'] == ['header.payload.sig']
        assert q['client_id'] == ['netforge']
        assert q['post_logout_redirect_uri'] == ['http://localhost/login']
        assert whoami(client)[0] in (302, 401)
        # The session's SSO details went with it.
        with client.session_transaction() as s:
            assert 'oidc_id_token' not in s and 'oidc_login' not in s

    def test_oversized_id_token_is_not_kept(self, sso_app, client):
        self.provider(sso_app)
        self.sign_in(sso_app, client, id_token='x' * 5000)
        q = parse_qs(urlparse(client.post('/logout').headers['Location']).query)
        assert 'id_token_hint' not in q and q['client_id'] == ['netforge']

    def test_password_sign_in_stays_local(self, sso_app, client):
        self.provider(sso_app)
        User.create('root', 'password1', sso_app, is_admin=True)
        client.post('/login', data={'username': 'root', 'password': 'password1'})
        assert client.post('/logout').headers['Location'].endswith('/login')

    def test_no_end_session_endpoint_stays_local(self, sso_app, client):
        self.provider(sso_app, endpoint=None)
        self.sign_in(sso_app, client)
        assert client.post('/logout').headers['Location'].endswith('/login')
        assert whoami(client)[0] in (302, 401)

    def test_bad_setting_refuses_to_start(self, app):
        app.config.update(SSO_CONFIG, OIDC_LOGOUT='everywhere')
        with pytest.raises(RuntimeError, match='OIDC_LOGOUT'):
            sso.init_sso(app)

    def test_shown_on_the_admin_page(self, sso_app, client):
        self.provider(sso_app)
        User.create('root', 'password1', sso_app, is_admin=True)
        client.post('/login', data={'username': 'root', 'password': 'password1'})
        assert 'NetForgeUI and the identity provider' in \
            client.get('/admin/sso').get_data(as_text=True)


class TestSessionLength:
    """OIDC_SESSION_HOURS: an SSO session goes back through the provider
    after this long, so leaving a group takes effect."""

    def sign_in(self, app, client):
        callback(app, client, claims())
        assert whoami(client)[0] == 200

    def age(self, client, hours):
        import time
        with client.session_transaction() as s:
            s['oidc_login_at'] = int(time.time() - hours * 3600)

    def test_fresh_session_is_fine(self, sso_app, client):
        self.sign_in(sso_app, client)
        self.age(client, 11.9)
        assert whoami(client)[0] == 200

    def test_expired_session_is_signed_out(self, sso_app, client):
        self.sign_in(sso_app, client)
        self.age(client, 12.1)
        res = client.get('/projects')
        assert res.status_code == 302 and res.headers['Location'].endswith('/login')
        assert 'Your sign-in has expired' in client.get('/login').get_data(as_text=True)
        assert whoami(client)[0] in (302, 401)
        with client.session_transaction() as s:
            assert 'oidc_login' not in s and 'oidc_login_at' not in s

    def test_expired_fetch_gets_json(self, sso_app, client):
        self.sign_in(sso_app, client)
        self.age(client, 13)
        res = client.get('/projects', headers={'Accept': 'application/json'})
        assert res.status_code == 401 and 'expired' in res.get_json()['error']

    def test_sso_account_without_a_sign_in_time_is_expired(self, sso_app, client):
        # Sessions from before the limit existed carry no time.
        self.sign_in(sso_app, client)
        with client.session_transaction() as s:
            del s['oidc_login_at']
            del s['oidc_login']
        assert client.get('/projects').status_code == 302

    def test_local_admin_password_session_is_not_limited(self, sso_app, client):
        User.create('root', 'password1', sso_app, is_admin=True)
        client.post('/login', data={'username': 'root', 'password': 'password1'})
        self.age(client, 100)
        assert whoami(client)[0] == 200

    def test_signing_in_again_still_works(self, sso_app, client):
        self.sign_in(sso_app, client)
        self.age(client, 24)
        assert client.get('/login/sso').status_code == 302
        self.sign_in(sso_app, client)
        assert whoami(client)[0] == 200

    def test_zero_means_no_limit(self, sso_app, client):
        sso_app.config['OIDC_SESSION_HOURS'] = '0'
        self.sign_in(sso_app, client)
        self.age(client, 1000)
        assert whoami(client)[0] == 200

    def test_configurable(self, sso_app, client):
        sso_app.config['OIDC_SESSION_HOURS'] = '1'
        self.sign_in(sso_app, client)
        self.age(client, 1.5)
        assert client.get('/projects').status_code == 302

    @pytest.mark.parametrize('value', ['twelve', '-1'])
    def test_bad_setting_refuses_to_start(self, app, value):
        app.config.update(SSO_CONFIG, OIDC_SESSION_HOURS=value)
        with pytest.raises(RuntimeError, match='OIDC_SESSION_HOURS'):
            sso.init_sso(app)

    def test_off_when_sso_is_off(self, auth_client):
        with auth_client.session_transaction() as s:
            s['oidc_login'] = True
        assert auth_client.get('/projects').status_code == 200

    def test_shown_on_the_admin_page(self, sso_app, client):
        User.create('root', 'password1', sso_app, is_admin=True)
        client.post('/login', data={'username': 'root', 'password': 'password1'})
        assert '12 hours, then sign in again' in client.get('/admin/sso').get_data(as_text=True)
