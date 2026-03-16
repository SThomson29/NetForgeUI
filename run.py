import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from app.models import User

app = create_app()


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


def _check_repo():
    """Warn if the config repo has not been cloned yet."""
    repo = app.config['CONFIGGEN_REPO']
    if not os.path.isdir(os.path.join(repo, '.git')):
        url = app.config.get('CONFIGGEN_REPO_URL', '')
        print(f'[warning] Config repo not found at {repo}')
        if url:
            print(f'[warning] Log in as admin and use the Pull button to clone it from {url}')
        else:
            print('[warning] Set CONFIGGEN_REPO_URL env var, then pull via the admin panel')


if __name__ == '__main__':
    with app.app_context():
        from app.ssh_setup import setup_ssh
        setup_ssh(app)

    _bootstrap_admin()
    _check_repo()

    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    )
