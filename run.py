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


def _sync_repo(source=None):
    """Clone a repo on first boot, pull on subsequent restarts. source is
    the repo_sync.Source to sync: NetForge unless told otherwise."""
    from app import audit, repo_sync
    source = source or repo_sync.NETFORGE
    netforge = source is repo_sync.NETFORGE

    if not source.url(app):
        # The Central collection is optional; say nothing when it isn't used.
        if netforge:
            print('[repo] CONFIGGEN_REPO_URL not set — skipping repo sync')
        return

    if not repo_sync.cloned(app, source):
        repo_sync.clone(app, source)
        return

    rc, out, before, after = repo_sync.sync_at_startup(app, source=source)
    try:
        audit.record_startup_pull(app, before, after, rc, out,
                                  repo=None if netforge else source.name)
    except Exception as e:                      # noqa: BLE001
        print(f'[repo] Could not record the pull in the audit log: {e}')


def _run_bootstrap():
    """Run all startup tasks — called before Gunicorn starts."""
    with app.app_context():
        from app.ssh_setup import setup_ssh
        setup_ssh(app)

    _bootstrap_admin()
    _sync_repo()
    from app import repo_sync
    _sync_repo(repo_sync.CENTRAL)

    # Runs that were in progress when the container stopped never finished.
    with app.app_context():
        from app.audit import mark_interrupted, trim_old
        mark_interrupted(app)
        trim_old(app)

    from app.docker_runner import cleanup_orphaned_containers
    cleanup_orphaned_containers()

    # Firmware uploads cut off by the restart.
    from app.firmware_images import remove_stale_uploads
    remove_stale_uploads(app.config.get('FIRMWARE_DIR') or '')


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
