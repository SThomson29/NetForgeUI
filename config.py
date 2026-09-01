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
