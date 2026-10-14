from flask import (Blueprint, render_template, redirect, url_for, request, flash,
                   current_app, abort)
from flask_login import login_user, logout_user, login_required, current_user
from .models import User
from .utils import ensure_workspace
from . import sso

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('projects.projects_page'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/healthz')
def healthz():
    """Liveness for Docker's HEALTHCHECK. Unauthenticated, reveals nothing.

    Answering at all shows gunicorn is serving requests; the query shows
    the user database — which every login needs — is readable.
    """
    from .models import get_db
    try:
        with get_db(current_app._get_current_object()) as db:
            db.execute('SELECT 1').fetchone()
    except Exception:
        return {'ok': False}, 503
    return {'ok': True}


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('projects.projects_page'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        throttle = _login_throttle()
        addr = request.remote_addr or 'unknown'

        if throttle.blocked(addr, username):
            # Checked before the password, so a correct guess made while
            # blocked is not confirmed either.
            flash('Too many failed attempts. Try again later.', 'error')
            return _login_page(), 429

        user = User.authenticate(username, password, current_app._get_current_object())
        # With SSO on, passwords are the way in for local admins only — for
        # when the identity provider is unavailable. Refused with the same
        # message as a wrong password, so it says nothing about the account.
        if user and sso.enabled(current_app) and not (user.local_admin and user.has_password):
            user = None
        if user:
            throttle.record_success(addr, username)
            login_user(user)
            ensure_workspace(current_app._get_current_object(), user.username)
            return redirect(url_for('projects.projects_page'))
        throttle.record_failure(addr, username)
        flash('Invalid username or password.', 'error')

    return _login_page()


def _login_page():
    app = current_app
    return render_template('login.html', sso_enabled=sso.enabled(app),
                           sso_label=app.config.get('OIDC_BUTTON_LABEL') or 'Sign in with SSO')


def _login_throttle():
    """One throttle per app, created on first use."""
    from .security import LoginThrottle
    app = current_app._get_current_object()
    if 'login_throttle' not in app.extensions:
        app.extensions['login_throttle'] = LoginThrottle(
            max_failures=app.config.get('LOGIN_MAX_FAILURES', 5),
            window_seconds=app.config.get('LOGIN_WINDOW_SECONDS', 900))
    return app.extensions['login_throttle']


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST' and not current_user.has_password:
        abort(403)
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw     = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        app = current_app._get_current_object()
        user = User.authenticate(current_user.username, current_pw, app)
        if not user:
            flash('Current password is incorrect.', 'error')
        elif new_pw != confirm_pw:
            flash('New passwords do not match.', 'error')
        elif len(new_pw) < 8:
            flash('Password must be at least 8 characters.', 'error')
        else:
            User.change_password(current_user.id, new_pw, app)
            # The change ends every existing session, this one included;
            # re-issue this one so the user is not logged out mid-page.
            login_user(User.get(current_user.id, app))
            flash('Password changed successfully. Other sessions have been '
                  'signed out.', 'success')

    return render_template('account.html')
