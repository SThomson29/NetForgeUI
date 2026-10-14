import os
import re
import shutil
import subprocess
import threading
from flask import (Blueprint, render_template, redirect, url_for,
                   request, flash, current_app, abort, jsonify)
from flask_login import login_required, current_user
from .models import User
from .utils import ensure_workspace, user_dir
from .ssh_setup import get_git_env

admin_bp = Blueprint('admin', __name__)

# In-memory pull job store
_pull_job  = {'status': 'idle', 'output': '', 'error': ''}
_pull_lock = threading.Lock()


def _require_admin():
    if not current_user.is_admin:
        abort(403)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@admin_bp.route('/admin')
@login_required
def admin_page():
    _require_admin()
    app   = current_app._get_current_object()
    users = User.all_users(app)
    repo_status = _get_repo_status(app)
    from .sso import enabled as sso_enabled
    return render_template('admin.html', users=users, repo_status=repo_status,
                           sso_enabled=sso_enabled(app))


@admin_bp.route('/admin/create', methods=['POST'])
@login_required
def create_user():
    _require_admin()
    app      = current_app._get_current_object()
    username = request.form.get('username', '').strip().lower()
    password = request.form.get('password', '')
    is_admin = bool(request.form.get('is_admin'))

    if not re.match(r'^[a-z0-9_\-]+$', username):
        flash('Username may only contain lowercase letters, numbers, hyphens and underscores.', 'error')
        return redirect(url_for('admin.admin_page'))

    if len(password) < 8:
        flash('Password must be at least 8 characters.', 'error')
        return redirect(url_for('admin.admin_page'))

    if User.get_by_username(username, app):
        flash(f'User {username} already exists.', 'error')
        return redirect(url_for('admin.admin_page'))

    User.create(username, password, app, is_admin=is_admin)
    ensure_workspace(app, username)
    flash(f'User {username} created.', 'success')
    return redirect(url_for('admin.admin_page'))


@admin_bp.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    _require_admin()
    app  = current_app._get_current_object()
    user = User.get(user_id, app)

    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('admin.admin_page'))

    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('admin.admin_page'))

    workspace = user_dir(app, user.username)
    if os.path.isdir(workspace):
        shutil.rmtree(workspace)

    User.delete(user_id, app)
    flash(f'User {user.username} deleted.', 'success')
    return redirect(url_for('admin.admin_page'))


# ---------------------------------------------------------------------------
# SSO linking — attach a local account to someone's SSO identity
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@admin_bp.route('/admin/link/<int:user_id>', methods=['POST'])
@login_required
def link_sso(user_id):
    """Link a local account to an SSO email.

    Normally done before the person's first SSO sign-in, which then lands
    in this account and its projects. If they have already signed in and
    got a new account, its identity moves here — provided that account has
    no projects to lose.
    """
    _require_admin()
    from .project import list_projects
    app   = current_app._get_current_object()
    user  = User.get(user_id, app)
    email = request.form.get('email', '').strip().lower()
    if not user or user.source != 'local' or user.oidc_sub:
        flash('Only an unlinked local account can be linked.', 'error')
        return redirect(url_for('admin.admin_page'))
    if not _EMAIL.match(email):
        flash('Enter the email address the person signs in to SSO with.', 'error')
        return redirect(url_for('admin.admin_page'))

    existing = User.get_sso_by_email(email, app)
    if existing:
        if list_projects(app, existing.username):
            flash(f'{email} has already signed in as {existing.username}, which has '
                  f'projects. Export and remove them, or delete that account, '
                  f'then link again.', 'error')
            return redirect(url_for('admin.admin_page'))
        User.move_sso_identity(existing.id, user.id, app)
        workspace = user_dir(app, existing.username)
        if os.path.isdir(workspace):
            shutil.rmtree(workspace)
        flash(f'{user.username} is now linked to {email}; the empty account '
              f'{existing.username} was removed.', 'success')
        return redirect(url_for('admin.admin_page'))

    User.set_link_email(user.id, email, app)
    flash(f'{user.username} will be linked when {email} next signs in by SSO.', 'success')
    return redirect(url_for('admin.admin_page'))


@admin_bp.route('/admin/unlink/<int:user_id>', methods=['POST'])
@login_required
def unlink_sso(user_id):
    _require_admin()
    app  = current_app._get_current_object()
    user = User.get(user_id, app)
    if not user or user.source != 'local':
        flash('Only a local account can be unlinked.', 'error')
        return redirect(url_for('admin.admin_page'))
    if user.id == current_user.id and user.oidc_sub:
        flash('You cannot unlink your own account.', 'error')
        return redirect(url_for('admin.admin_page'))
    User.unlink_sso(user.id, app)
    flash(f'{user.username} is no longer linked to SSO.', 'success')
    return redirect(url_for('admin.admin_page'))


# ---------------------------------------------------------------------------
# Audit log — every user's deploys and firmware runs
# ---------------------------------------------------------------------------

@admin_bp.route('/admin/audit')
@login_required
def audit_log():
    _require_admin()
    from . import audit
    app = current_app._get_current_object()
    user = (request.args.get('user') or '').strip() or None
    project = (request.args.get('project') or '').strip() or None
    show_gen = request.args.get('generations', '1') != '0'
    return render_template(
        'admin_audit.html', filter_user=user, filter_project=project,
        show_generations=show_gen, detail_days=audit.AUDIT_DETAIL_DAYS,
        entries=audit.entries(app, username=user, project=project, limit=500,
                              include_generations=show_gen))


# ---------------------------------------------------------------------------
# Repo management — admin only
# ---------------------------------------------------------------------------

def _get_repo_status(app):
    """Return dict with current commit, branch, and remote URL."""
    repo = app.config['CONFIGGEN_REPO']
    if not os.path.isdir(os.path.join(repo, '.git')):
        return {'cloned': False, 'branch': None, 'commit': None, 'remote': None}
    try:
        env = get_git_env(app)
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=repo, text=True, stderr=subprocess.DEVNULL, env=env
        ).strip()
        commit = subprocess.check_output(
            ['git', 'log', '-1', '--format=%h %s %cr'],
            cwd=repo, text=True, stderr=subprocess.DEVNULL, env=env
        ).strip()
        remote = subprocess.check_output(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=repo, text=True, stderr=subprocess.DEVNULL, env=env
        ).strip()
        return {'cloned': True, 'branch': branch, 'commit': commit, 'remote': remote}
    except Exception as e:
        return {'cloned': True, 'branch': '?', 'commit': str(e), 'remote': None}


def _run_pull(repo_path, repo_url, git_env):
    """Clone or pull the config repo. Runs in a background thread."""
    with _pull_lock:
        _pull_job['status'] = 'running'
        _pull_job['output'] = ''
        _pull_job['error']  = ''

    try:
        if os.path.isdir(os.path.join(repo_path, '.git')):
            result = subprocess.run(
                ['git', 'pull', '--rebase'],
                cwd=repo_path,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=git_env
            )
        else:
            os.makedirs(repo_path, exist_ok=True)
            result = subprocess.run(
                ['git', 'clone', repo_url, repo_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=git_env
            )

        with _pull_lock:
            _pull_job['output'] = result.stdout
            if result.returncode == 0:
                _pull_job['status'] = 'done'
            else:
                _pull_job['status'] = 'failed'
                _pull_job['error']  = f'Exit code {result.returncode}'

    except Exception as e:
        with _pull_lock:
            _pull_job['status'] = 'failed'
            _pull_job['error']  = str(e)


@admin_bp.route('/admin/pull', methods=['POST'])
@login_required
def pull_repo():
    _require_admin()
    app = current_app._get_current_object()

    with _pull_lock:
        if _pull_job['status'] == 'running':
            return jsonify({'ok': False, 'error': 'A pull is already in progress.'})

    repo_path = app.config['CONFIGGEN_REPO']
    repo_url  = app.config['CONFIGGEN_REPO_URL']

    if not repo_url and not os.path.isdir(os.path.join(repo_path, '.git')):
        return jsonify({'ok': False, 'error': 'CONFIGGEN_REPO_URL is not set and no repo is cloned.'})

    git_env = get_git_env(app)

    t = threading.Thread(
        target=_run_pull,
        args=(repo_path, repo_url, git_env),
        daemon=True
    )
    t.start()
    return jsonify({'ok': True})


@admin_bp.route('/admin/pull/status')
@login_required
def pull_status():
    _require_admin()
    with _pull_lock:
        return jsonify(dict(_pull_job))
