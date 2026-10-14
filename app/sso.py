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
(sam.thomson@corp.com -> sam-thomson), unless an admin has linked an
existing local account to that email, in which case it attaches to that
account and its projects. Password sign-in stays available to local
admins only, as the way in when the provider is unavailable.

Signing out ends the NetForgeUI session only.
"""

import logging
import re
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, url_for
from flask_login import login_user

from .models import User
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


def _email(claims):
    for key in ('email', 'preferred_username', 'upn'):
        value = str(claims.get(key) or '').strip()
        if '@' in value:
            return value.lower()
    return None


def username_for(email, taken):
    """sam.thomson@corp.com -> 'sam-thomson', or 'sam-thomson-2' if taken.

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
        if not email:
            raise SSOError('The identity provider did not send an email address, '
                           'which a first sign-in needs.')
        # An admin-linked local account claims the first sign-in with its
        # email — but not on an address the provider says is unverified.
        if claims.get('email_verified') is not False:
            user = User.get_by_link_email(email, app)
        if user is None:
            name = username_for(email, lambda n: User.get_by_username(n, app) is not None)
            user = User.create_sso(name, sub, app)
            log.info('SSO: created %s for %s', user.username, email)
        else:
            log.info('SSO: linked %s to %s', user.username, email)

    User.record_sso_login(
        user.id, sub=sub, email=email or user.email, groups=groups,
        sso_admin=bool(admin_group) and admin_group in groups,
        when=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), app=app)
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
    return _client().authorize_redirect(_redirect_uri())


@sso_bp.route('/login/sso/callback')
def callback():
    from authlib.integrations.base_client import OAuthError
    client = _client()
    app = current_app._get_current_object()
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
        flash(str(e), 'error')
        return redirect(url_for('auth.login'))
    except OAuthError as e:
        log.warning('SSO sign-in failed: %s', e)
        flash('Single sign-on failed: %s' % (e.description or e.error), 'error')
        return redirect(url_for('auth.login'))
    except Exception:                               # noqa: BLE001
        log.exception('SSO sign-in failed')
        flash('Single sign-on failed. Check the server log for details.', 'error')
        return redirect(url_for('auth.login'))

    login_user(user)
    ensure_workspace(app, user.username)
    return redirect(url_for('projects.projects_page'))

