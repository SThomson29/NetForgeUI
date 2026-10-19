"""
Single sign-on through one OpenID Connect provider (Entra ID, Okta,
Keycloak, Authentik, ...), configured by environment variables.

    GET /login/sso            redirect to the provider (code flow + PKCE)
    GET /login/sso/callback   back from it: validate, then sign in

Authlib does the protocol work — discovery, state, nonce, PKCE, the code
exchange and checking the ID token's signature, issuer, audience, expiry
and nonce. This module decides who gets in:

  - only members of OIDC_ALLOWED_GROUP
  - members of OIDC_ADMIN_GROUP are admins, re-checked at every sign-in
  - the account is found by the provider's subject id, which never
    changes, rather than by email, which can

A first sign-in creates an account named from the email's local part
(sam.thomson@corp.com -> sam-thomson), or from the provider's username
when it sends no email, unless an admin has linked an
existing local account to that email, in which case it attaches to that
account and its projects. Password sign-in stays available to local
admins only, as the way in when the provider is unavailable.

Signing out ends the NetForgeUI session only.

Settings stay in the environment, never the database, so the client
secret isn't in users.db or its backups. Admin → Single sign-on shows
them read-only, tests discovery, and lists recent sign-ins and refusals.
"""

import json
import logging
import re
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, url_for
from flask_login import login_user

from .models import User, get_db
from .utils import ensure_workspace

log = logging.getLogger(__name__)

sso_bp = Blueprint('sso', __name__)

_REQUIRED = ('OIDC_ISSUER', 'OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET', 'OIDC_ALLOWED_GROUP')


class SSOError(Exception):
    """A sign-in that must be refused, with a message the user can act on."""


def enabled(app):
    return bool(app.config.get('OIDC_ISSUER'))


def check_settings(app):
    """Refuse to start half-configured. Without an allowed group, anyone the
    provider knows — possibly a whole tenant — could sign in."""
    if not enabled(app):
        return
    missing = [k for k in _REQUIRED if not app.config.get(k)]
    if missing:
        raise RuntimeError('OIDC_ISSUER is set, so SSO is on, but %s %s not set.'
                           % (', '.join(missing), 'is' if len(missing) == 1 else 'are'))


def init_sso(app):
    """Register the provider with Authlib. Nothing is fetched until the
    first sign-in, so the app starts even if the provider is unreachable."""
    check_settings(app)
    _init_events(app)
    if not enabled(app):
        return
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    issuer = app.config['OIDC_ISSUER'].rstrip('/')
    oauth.register(
        'oidc',
        client_id=app.config['OIDC_CLIENT_ID'],
        client_secret=app.config['OIDC_CLIENT_SECRET'],
        server_metadata_url=issuer + '/.well-known/openid-configuration',
        client_kwargs={'scope': app.config.get('OIDC_SCOPES') or 'openid email profile',
                       'code_challenge_method': 'S256'},
    )
    app.extensions['netforge_oidc'] = oauth.oidc


# ── Recent sign-ins, for Admin → Single sign-on ───────────

KEEP_EVENTS = 100


def _init_events(app):
    with get_db(app) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS sso_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                at       TEXT NOT NULL,
                outcome  TEXT NOT NULL,     -- signed_in | created | linked | refused | failed
                username TEXT,
                email    TEXT,
                detail   TEXT,
                groups   TEXT               -- JSON list, what the provider sent
            )
        ''')


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def record_event(app, outcome, *, username=None, email=None, detail=None, groups=None):
    """Keep the last KEEP_EVENTS sign-in attempts. Never lets a failure to
    record get in the way of signing in."""
    try:
        with get_db(app) as db:
            db.execute('INSERT INTO sso_events (at, outcome, username, email, detail, groups) '
                       'VALUES (?,?,?,?,?,?)',
                       (_now(), outcome, username, email, detail,
                        json.dumps(sorted(groups)[:50]) if groups is not None else None))
            db.execute('DELETE FROM sso_events WHERE id <= '
                       '(SELECT id FROM sso_events ORDER BY id DESC LIMIT 1 OFFSET ?)',
                       (KEEP_EVENTS,))
    except Exception:                               # noqa: BLE001
        log.exception('Could not record SSO sign-in event')


def recent_events(app, limit=50):
    import sqlite3
    try:
        with get_db(app) as db:
            rows = db.execute('SELECT * FROM sso_events ORDER BY id DESC LIMIT ?',
                              (limit,)).fetchall()
    except sqlite3.OperationalError:                # table not made yet
        return []
    out = []
    for r in rows:
        e = dict(r)
        e['groups'] = json.loads(e['groups']) if e['groups'] else None
        out.append(e)
    return out


# ── Settings, read-only, for Admin → Single sign-on ────────

def settings_summary(app):
    """The effective settings, with the secret reduced to set / not set."""
    cfg = app.config
    return {
        'enabled':       enabled(app),
        'issuer':        cfg.get('OIDC_ISSUER') or None,
        'client_id':     cfg.get('OIDC_CLIENT_ID') or None,
        'secret_set':    bool(cfg.get('OIDC_CLIENT_SECRET')),
        'allowed_group': cfg.get('OIDC_ALLOWED_GROUP') or None,
        'admin_group':   cfg.get('OIDC_ADMIN_GROUP') or None,
        'groups_claim':  cfg.get('OIDC_GROUPS_CLAIM') or 'groups',
        'scopes':        cfg.get('OIDC_SCOPES') or 'openid email profile',
        'redirect_override': cfg.get('OIDC_REDIRECT_URI') or None,
        'button_label':  cfg.get('OIDC_BUTTON_LABEL') or 'Sign in with SSO',
    }


def discovery_url(issuer):
    return issuer.rstrip('/') + '/.well-known/openid-configuration'


def check_discovery(app, timeout=8):
    """Fetch the provider's discovery document and check it against what
    sign-in needs. Returns {ok, url, error?, issuer?, endpoints?, problems,
    notes} — problems would stop sign-in, notes might not."""
    import requests
    issuer = app.config.get('OIDC_ISSUER') or ''
    url = discovery_url(issuer)
    result = {'ok': False, 'url': url, 'problems': [], 'notes': []}
    try:
        res = requests.get(url, timeout=timeout, headers={'Accept': 'application/json'})
    except requests.RequestException as e:
        result['error'] = 'Could not reach the provider: %s' % e.__class__.__name__
        if str(e):
            result['error'] += ' (%s)' % str(e)[:300]
        return result
    if res.status_code != 200:
        result['error'] = 'The provider answered HTTP %d. Check OIDC_ISSUER.' % res.status_code
        return result
    try:
        meta = res.json()
        if not isinstance(meta, dict):
            raise ValueError
    except ValueError:
        result['error'] = 'The provider did not return a discovery document (not JSON).'
        return result

    result['issuer'] = meta.get('issuer')
    result['endpoints'] = {k: meta.get(k) for k in
                           ('authorization_endpoint', 'token_endpoint',
                            'userinfo_endpoint', 'jwks_uri')}
    problems, notes = result['problems'], result['notes']
    if (meta.get('issuer') or '').rstrip('/') != issuer.rstrip('/'):
        problems.append('The provider calls itself %s, not %s. ID tokens will fail '
                        'the issuer check; set OIDC_ISSUER to exactly what it reports.'
                        % (meta.get('issuer') or 'nothing', issuer))
    for k in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        if not meta.get(k):
            problems.append('The discovery document has no %s.' % k)
    methods = meta.get('code_challenge_methods_supported')
    if methods is not None and 'S256' not in methods:
        problems.append('The provider does not list PKCE S256, which sign-in uses.')
    claim = app.config.get('OIDC_GROUPS_CLAIM') or 'groups'
    supported = meta.get('claims_supported')
    if supported is not None and claim not in supported:
        notes.append('The provider does not advertise a "%s" claim. Many send it '
                     'anyway once configured; if sign-ins are refused for the '
                     'group, check the refusals below for what was sent.' % claim)
    result['ok'] = not problems
    return result


# ── Deciding who gets in ───────────────────────────────────

def _groups(claims, claim_name):
    """The user's groups as a set of strings.

    Keycloak can send full paths ('/network'), so a leading slash is
    ignored. Entra ID omits the list when a user is in too many groups and
    sends a pointer instead; that is refused with what to change, rather
    than looking like the user isn't in the group.
    """
    value = claims.get(claim_name)
    if value is None:
        if claim_name in (claims.get('_claim_names') or {}):
            raise SSOError('Your account is in too many groups for the sign-in '
                           'token to list them. Ask an administrator to limit the '
                           'groups the identity provider sends to this app.')
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(g).lstrip('/') for g in value if g}


def _groups_or_none(app, claims):
    """The groups sent, for the refusal record, or None if there were none
    to read (no claims yet, or an Entra overage pointer)."""
    try:
        return _groups(claims, app.config.get('OIDC_GROUPS_CLAIM') or 'groups') or None
    except SSOError:
        return None


def _email(claims):
    for key in ('email', 'preferred_username', 'upn'):
        value = str(claims.get(key) or '').strip()
        if '@' in value:
            return value.lower()
    return None


def _login_name(claims):
    """The provider's username for this person, for naming an account
    when it sends no email. Only names the account; never matches one."""
    for key in ('preferred_username', 'nickname'):
        value = str(claims.get(key) or '').strip()
        if value:
            return value
    return None


def _who(claims):
    """Email or provider username, for the sign-in record."""
    return _email(claims) or _login_name(claims)


def username_for(email, taken):
    """sam.thomson@corp.com -> 'sam-thomson', or 'sam-thomson-2' if taken.
    A provider username without an @ is used as it is: 'Jo.Bloggs' -> 'jo-bloggs'.

    Usernames are folder names and must match what Admin allows: lowercase
    letters, numbers, hyphens and underscores.
    """
    base = re.sub(r'[^a-z0-9_-]+', '-', email.split('@')[0].lower()).strip('-_')
    base = re.sub(r'-{2,}', '-', base)[:56] or 'user'
    name, n = base, 2
    while taken(name):
        name, n = '%s-%d' % (base, n), n + 1
    return name


def sign_in(app, claims):
    """The account for a validated set of claims, creating or linking it.
    Raises SSOError if this person may not sign in."""
    sub = str(claims.get('sub') or '').strip()
    if not sub:
        raise SSOError('The identity provider did not identify the account.')

    groups = _groups(claims, app.config.get('OIDC_GROUPS_CLAIM') or 'groups')
    allowed = app.config['OIDC_ALLOWED_GROUP'].lstrip('/')
    admin_group = (app.config.get('OIDC_ADMIN_GROUP') or '').lstrip('/')
    if allowed not in groups:
        log.warning('SSO sign-in refused for %s: not in %s', claims.get('email') or sub, allowed)
        raise SSOError('Your account is not in the group allowed to use NetForgeUI.')

    email = _email(claims)
    user = User.get_by_sub(sub, app)
    if user is None:
        name_from = email or _login_name(claims)
        if not name_from:
            raise SSOError('The identity provider sent neither an email address nor '
                           'a username, and a first sign-in needs one to name the account.')
        # An admin-linked local account claims the first sign-in with its
        # email — but not on an address the provider says is unverified.
        if email and claims.get('email_verified') is not False:
            user = User.get_by_link_email(email, app)
        if user is None:
            name = username_for(name_from, lambda n: User.get_by_username(n, app) is not None)
            user = User.create_sso(name, sub, app)
            log.info('SSO: created %s for %s', user.username, name_from)
            outcome = 'created'
        else:
            log.info('SSO: linked %s to %s', user.username, email)
            outcome = 'linked'
    else:
        outcome = 'signed_in'

    User.record_sso_login(
        user.id, sub=sub, email=email or user.email, groups=groups,
        sso_admin=bool(admin_group) and admin_group in groups,
        when=_now(), app=app)
    record_event(app, outcome, username=user.username, email=email or _login_name(claims),
                 groups=groups)
    return User.get(user.id, app)


# ── Routes ─────────────────────────────────────────────────

def _client():
    client = current_app.extensions.get('netforge_oidc')
    if client is None:
        from flask import abort
        abort(404)
    return client


def _redirect_uri():
    return (current_app.config.get('OIDC_REDIRECT_URI')
            or url_for('sso.callback', _external=True))


@sso_bp.route('/login/sso')
def start():
    """Redirect to the provider. The first call fetches its discovery
    document; if that fails (unreachable, DNS, untrusted certificate) the
    login page says so instead of an Internal Server Error."""
    client = _client()
    try:
        return client.authorize_redirect(_redirect_uri())
    except Exception as e:                          # noqa: BLE001
        app = current_app._get_current_object()
        log.exception('SSO sign-in could not start')
        reason = _start_failure(e)
        record_event(app, 'failed', detail='Could not start sign-in: %s' % reason)
        flash('Single sign-on is unavailable: %s. An admin can see more under '
              'Admin → Single sign-on.' % reason, 'error')
        return redirect(url_for('auth.login'))


def _start_failure(exc):
    """A short reason for a failed discovery fetch, without the URL soup."""
    import requests
    if isinstance(exc, requests.exceptions.SSLError):
        return "the identity provider's certificate isn't trusted"
    if isinstance(exc, requests.exceptions.Timeout):
        return 'the identity provider did not answer in time'
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "the identity provider can't be reached"
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return 'the identity provider answered HTTP %d' % exc.response.status_code
    return 'the sign-in request failed (%s)' % exc.__class__.__name__


@sso_bp.route('/login/sso/callback')
def callback():
    from authlib.integrations.base_client import OAuthError
    client = _client()
    app = current_app._get_current_object()
    claims = {}
    try:
        token = client.authorize_access_token()
        claims = dict(token.get('userinfo') or {})
        # Some providers only put groups in the userinfo response.
        claim_name = app.config.get('OIDC_GROUPS_CLAIM') or 'groups'
        if claim_name not in claims and claim_name not in (claims.get('_claim_names') or {}):
            try:
                extra = client.userinfo(token=token)
                if extra.get('sub') == claims.get('sub'):
                    claims.setdefault(claim_name, extra.get(claim_name))
            except Exception:                       # noqa: BLE001
                pass
        user = sign_in(app, claims)
    except SSOError as e:
        record_event(app, 'refused', email=_who(claims), detail=str(e),
                     groups=_groups_or_none(app, claims))
        flash(str(e), 'error')
        return redirect(url_for('auth.login'))
    except OAuthError as e:
        log.warning('SSO sign-in failed: %s', e)
        record_event(app, 'failed', email=_who(claims),
                     detail='%s: %s' % (e.error, e.description or ''))
        flash('Single sign-on failed: %s' % (e.description or e.error), 'error')
        return redirect(url_for('auth.login'))
    except Exception as e:                          # noqa: BLE001
        log.exception('SSO sign-in failed')
        record_event(app, 'failed', email=_who(claims),
                     detail='%s (see the server log)' % e.__class__.__name__)
        flash('Single sign-on failed. Check the server log for details.', 'error')
        return redirect(url_for('auth.login'))

    login_user(user)
    ensure_workspace(app, user.username)
    return redirect(url_for('projects.projects_page'))

