import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.models import User

app = create_app()

if os.environ.get('FLASK_PROXY_FIX', 'false').lower() == 'true':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


def _bootstrap_admin():
    """Create a default admin account on first run if no users exist."""
    with app.app_context():
        users = User.all_users(app)
        if not users:
            default_pass = os.environ.get('ADMIN_PASSWORD', 'changeme123')
            User.create('admin', default_pass, app, is_admin=True)
            from app.utils import ensure_workspace
            ensure_workspace(app, 'admin')
            print(f'[bootstrap] Created default admin user (password: {default_pass})')
            print('[bootstrap] Change this password immediately via /account')


def _sync_repo():
    """Clone the config repo on first boot, pull on subsequent restarts."""
    import subprocess
    import shutil
    repo = app.config['CONFIGGEN_REPO']
    url  = app.config.get('CONFIGGEN_REPO_URL', '')

    if not url:
        print('[repo] CONFIGGEN_REPO_URL not set — skipping repo sync')
        return

    if not os.path.isdir(os.path.join(repo, '.git')):
        # A non-empty directory with no .git means a previous clone was
        # interrupted, or image content seeded the volume. git clone refuses
        # to write into it, so clear it out rather than wedging every restart.
        if os.path.isdir(repo) and os.listdir(repo):
            print(f'[repo] {repo} is non-empty but not a git repo — clearing it')
            for entry in os.listdir(repo):
                path = os.path.join(repo, entry)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        print(f'[repo] Cloning {url} into {repo} ...')
        os.makedirs(repo, exist_ok=True)
        result = subprocess.run(
            ['git', 'clone', url, repo],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print('[repo] Clone successful')
        else:
            print(f'[repo] Clone failed: {result.stderr.strip()}')
    else:
        print(f'[repo] Pulling latest from {url} ...')
        result = subprocess.run(
            ['git', '-C', repo, 'pull'],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f'[repo] Pull successful: {result.stdout.strip()}')
        else:
            print(f'[repo] Pull failed: {result.stderr.strip()}')


def _run_bootstrap():
    """Run all startup tasks — called before Gunicorn starts."""
    with app.app_context():
        from app.ssh_setup import setup_ssh
        setup_ssh(app)

    _bootstrap_admin()
    _sync_repo()

    from app.docker_runner import cleanup_orphaned_containers
    cleanup_orphaned_containers()


if __name__ == '__main__':
    if '--bootstrap' in sys.argv:
        # Bootstrap mode — run startup tasks then exit.
        # Gunicorn is started separately by the Dockerfile CMD.
        _run_bootstrap()
    else:
        # Dev mode — bootstrap and run Flask dev server.
        _run_bootstrap()
        app.run(
            host='0.0.0.0',
            port=int(os.environ.get('PORT', 5000)),
            debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
        )
