import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# The config repo is cloned here at container start.
# Override with CONFIGGEN_REPO_PATH env var if mounting differently.
CONFIGGEN_REPO_PATH = os.environ.get(
    'CONFIGGEN_REPO_PATH',
    os.path.join(BASE_DIR, 'configgen')
)


class Config:
    SECRET_KEY         = os.environ.get('SECRET_KEY', 'change-me-in-production')
    DATABASE           = os.path.join(BASE_DIR, 'data', 'users.db')
    DATA_DIR           = os.path.join(BASE_DIR, 'data', 'users')

    # Shared read-only config repo
    CONFIGGEN_REPO     = CONFIGGEN_REPO_PATH
    CONFIGGEN_REPO_URL = os.environ.get('CONFIGGEN_REPO_URL', '')
    SKELETON_DIR       = os.path.join(CONFIGGEN_REPO_PATH, 'inventory', 'skeleton')
    PLAYBOOK           = os.path.join(CONFIGGEN_REPO_PATH, 'playbooks', 'generate_configs.yml')
    ROLES_PATH         = os.path.join(CONFIGGEN_REPO_PATH, 'roles')

    # SSH deploy key for private git repo.
    # Mount the private key file into the container and set this path.
    SSH_KEY_PATH       = os.environ.get('SSH_KEY_PATH', '/root/.ssh/id_rsa')
    SSH_KNOWN_HOSTS    = os.environ.get('SSH_KNOWN_HOSTS', '/root/.ssh/known_hosts')

    # Ansible binary — override with ANSIBLE_PLAYBOOK env var if using a venv
    ANSIBLE_BIN        = os.environ.get('ANSIBLE_PLAYBOOK', 'ansible-playbook')

    # AOS-CX .swi images for the firmware feature. Mounted from the host
    # rather than uploaded — images are large and machine-local.
    FIRMWARE_DIR       = os.environ.get(
        'FIRMWARE_DIR', os.path.join(BASE_DIR, 'firmware'))

    # ── Session cookies ──
    # Lax stops the session cookie riding along on cross-site form posts,
    # which is the main CSRF route for the plain HTML forms (delete project,
    # delete host, admin user management). Browsers mostly default to Lax
    # already, but not all of them, so it is set explicitly.
    SESSION_COOKIE_SAMESITE  = 'Lax'
    SESSION_COOKIE_HTTPONLY  = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_HTTPONLY = True
    # Only send cookies over HTTPS. Off by default because the app is often
    # served over plain HTTP on an internal network, where a Secure cookie
    # would never be sent and nobody could log in. Turn it on behind TLS.
    SESSION_COOKIE_SECURE    = os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
    REMEMBER_COOKIE_SECURE   = SESSION_COOKIE_SECURE

    # ── Reverse proxy ──
    # Number of proxies in front of the app (Nginx alone is 1). When set,
    # the client address, scheme and host are taken from the X-Forwarded-*
    # headers the proxy adds. Leave at 0 when serving directly: trusting
    # those headers without a proxy lets a client set its own address and
    # sidestep the login throttle.
    TRUSTED_PROXIES = int(os.environ.get('TRUSTED_PROXIES', '0'))

    # ── Login throttling ──
    LOGIN_MAX_FAILURES   = int(os.environ.get('LOGIN_MAX_FAILURES', '5'))
    LOGIN_WINDOW_SECONDS = int(os.environ.get('LOGIN_WINDOW_SECONDS', '900'))
