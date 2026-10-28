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
    from .sso import enabled as sso_enabled
    return render_template('admin.html', users=users, sso_enabled=sso_enabled(app))


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

    # Projects other people use must go to one of them first; deleting
    # the owner would delete the project out from under them.
    from . import sharing
    shared = sharing.shared_projects_owned_by(app, user.username)
    if shared:
        flash(f'{user.username} owns shared projects ({", ".join(shared)}). Transfer '
              f'them from each project\'s Sharing page first.', 'error')
        return redirect(url_for('admin.admin_page'))

    workspace = user_dir(app, user.username)
    if os.path.isdir(workspace):
        shutil.rmtree(workspace)

    sharing.forget_user(app, user.username)
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
    """Link a local account to someone's SSO identity.

    With an email, normally before the person's first SSO sign-in, which
    then lands in this account and its projects. With the username of an
    SSO account — for people the provider knows by username alone — after
    it. Either way, if they already have an SSO account, its identity moves
    here, provided that account has no projects to lose.
    """
    _require_admin()
    from .project import list_projects
    app   = current_app._get_current_object()
    user  = User.get(user_id, app)
    value = request.form.get('email', '').strip().lower()
    if not user or user.source != 'local' or user.oidc_sub:
        flash('Only an unlinked local account can be linked.', 'error')
        return redirect(url_for('admin.admin_page'))

    if '@' in value:
        if not _EMAIL.match(value):
            flash('Enter the SSO email address, or the username of their SSO account.', 'error')
            return redirect(url_for('admin.admin_page'))
        existing = User.get_sso_by_email(value, app)
    else:
        existing = User.get_by_username(value, app) if value else None
        if not existing or existing.source != 'sso':
            flash(f'There is no SSO account called {value or "that"}. They need to sign '
                  f'in by SSO once first, or enter their SSO email instead.', 'error')
            return redirect(url_for('admin.admin_page'))

    if existing:
        if list_projects(app, existing.username):
            flash(f'{value} has already signed in as {existing.username}, which has '
                  f'projects. Export and remove them, or delete that account, '
                  f'then link again.', 'error')
            return redirect(url_for('admin.admin_page'))
        User.move_sso_identity(existing.id, user.id, app)
        workspace = user_dir(app, existing.username)
        if os.path.isdir(workspace):
            shutil.rmtree(workspace)
        flash(f'{user.username} is now linked to that SSO identity; the empty account '
              f'{existing.username} was removed.', 'success')
        return redirect(url_for('admin.admin_page'))

    User.set_link_email(user.id, value, app)
    flash(f'{user.username} will be linked when {value} next signs in by SSO.', 'success')
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

# The playbooks NetForgeUI runs, and what for.
REQUIRED_PLAYBOOKS = [
    ('generate_configs.yml', 'Generate'),
    ('deploy_dryrun.yml',    'Deploy dry run'),
    ('deploy_push.yml',      'Deploy push'),
    ('firmware_upgrade.yml', 'Firmware'),
]


@admin_bp.route('/admin/repo')
@login_required
def repo_page():
    """The NetForge repo: what's checked out, whether the playbooks and
    skeleton this app relies on are there, and which projects are missing
    skeleton files added since they were created."""
    _require_admin()
    app = current_app._get_current_object()
    repo = app.config['CONFIGGEN_REPO']
    playbooks = [{'file': f, 'used_by': used_by,
                  'present': os.path.isfile(os.path.join(repo, 'playbooks', f))}
                 for f, used_by in REQUIRED_PLAYBOOKS]
    skeleton = os.path.join(app.config['SKELETON_DIR'], 'aoscx')
    skeleton_files = sorted(f for f in os.listdir(skeleton) if f.endswith('.yml')) \
        if os.path.isdir(skeleton) else []
    return render_template('admin_repo.html', repo_status=_get_repo_status(app),
                           playbooks=playbooks, skeleton_files=skeleton_files,
                           behind=_projects_missing_skeleton_files(app))


@admin_bp.route('/admin/sso')
@login_required
def sso_page():
    """Single sign-on settings, read-only: they come from the environment
    so the client secret stays out of users.db."""
    _require_admin()
    app = current_app._get_current_object()
    from . import sso
    settings = sso.settings_summary(app)
    from werkzeug.routing import BuildError
    try:
        derived = url_for('sso.callback', _external=True)
    except BuildError:
        derived = None
    return render_template('admin_sso.html', s=settings,
                           redirect_uri=settings['redirect_override'] or derived,
                           derived_redirect=derived,
                           discovery_url=sso.discovery_url(settings['issuer'] or ''),
                           events=sso.recent_events(app))


@admin_bp.route('/admin/sso/test', methods=['POST'])
@login_required
def sso_test():
    _require_admin()
    app = current_app._get_current_object()
    from . import sso
    if not sso.enabled(app):
        return jsonify({'ok': False, 'error': 'Single sign-on is off.'}), 400
    return jsonify(sso.check_discovery(app))


# ---------------------------------------------------------------------------
# Firmware images — the .swi files every project's Firmware page offers
# ---------------------------------------------------------------------------

def _images_in_use():
    from .firmware_routes import _fw_jobs, _fw_jobs_lock
    with _fw_jobs_lock:
        return {j.get('image') for j in _fw_jobs.values() if j.get('_finished_at') is None}


@admin_bp.route('/admin/firmware')
@login_required
def firmware_page():
    _require_admin()
    from .firmware_images import catalogue, writable
    app = current_app._get_current_object()
    return render_template('admin_firmware.html', images=catalogue(app),
                           writable=writable(app),
                           firmware_dir=app.config.get('FIRMWARE_DIR', ''),
                           max_mb=app.config.get('FIRMWARE_MAX_UPLOAD_MB', 2048))


@admin_bp.route('/admin/firmware/upload', methods=['POST'])
@login_required
def firmware_upload():
    """The raw file as the request body, its name in ?name=. Streamed to
    disk in chunks rather than parsed as a form, so a 700 MB image is
    written once and never held in memory."""
    _require_admin()
    from .firmware_images import ImageError, save_upload
    app = current_app._get_current_object()
    try:
        row = save_upload(app, request.args.get('name', '').strip(), request.stream,
                          length=request.content_length, by=current_user.username,
                          max_bytes=app.config.get('FIRMWARE_MAX_UPLOAD_MB', 2048) * 1024 * 1024)
    except ImageError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, image=row)


@admin_bp.route('/admin/firmware/delete', methods=['POST'])
@login_required
def firmware_delete():
    _require_admin()
    from .firmware_images import ImageError, delete_image
    app = current_app._get_current_object()
    name = request.form.get('name', '')
    try:
        delete_image(app, name, in_use=_images_in_use())
        flash(f'{name} deleted.', 'success')
    except ImageError as e:
        flash(str(e), 'error')
    return redirect(url_for('admin.firmware_page'))


def _projects_missing_skeleton_files(app):
    """[{owner, project, hosts, files}] for projects whose switches lack
    skeleton files — what each project's Hosts page offers to add."""
    from .project import list_projects
    from .utils import reconcile_host_vars
    out = []
    for user in User.all_users(app):
        for p in list_projects(app, user.username):
            try:
                r = reconcile_host_vars(app, user.username, p['name'], dry_run=True)
            except Exception:                       # noqa: BLE001
                continue
            if r['total']:
                out.append({'owner': user.username, 'project': p['name'],
                            'hosts': len(r['hosts']), 'files': r['total']})
    return out


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
