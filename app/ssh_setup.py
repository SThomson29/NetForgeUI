import os
import stat
import subprocess


def setup_ssh(app):
    """
    Configure SSH for git operations using a deploy key.

    - Ensures ~/.ssh exists with correct permissions
    - Sets correct permissions on the private key file
    - Writes an SSH config entry that forces use of the deploy key
    - Adds the git host to known_hosts via ssh-keyscan if not already present
    """
    key_path = app.config['SSH_KEY_PATH']
    known_hosts_path = app.config['SSH_KNOWN_HOSTS']
    repo_url = app.config['CONFIGGEN_REPO_URL']

    if not repo_url:
        return  # No repo URL configured, nothing to do

    # Determine if this is an SSH URL (git@ or ssh://)
    is_ssh = repo_url.startswith('git@') or repo_url.startswith('ssh://')
    if not is_ssh:
        return  # HTTPS URL, no SSH setup needed

    if not os.path.isfile(key_path):
        print(f'[ssh] Warning: SSH key not found at {key_path}')
        print(f'[ssh] Mount your deploy key to {key_path} to enable git operations')
        return

    ssh_dir = os.path.expanduser('~/.ssh')
    os.makedirs(ssh_dir, exist_ok=True)
    os.chmod(ssh_dir, stat.S_IRWXU)

    # If the key is mounted read-only, copy it to a writable location
    working_key = key_path
    if not os.access(key_path, os.W_OK):
        working_key = os.path.join(ssh_dir, 'deploy_key')
        import shutil
        shutil.copy2(key_path, working_key)
        print(f'[ssh] Copied read-only key to {working_key}')

    os.chmod(working_key, stat.S_IRUSR | stat.S_IWUSR)

    # Parse hostname from repo URL
    # git@github.com:org/repo.git  -> github.com
    # ssh://git@github.com/org/repo.git -> github.com
    hostname = _parse_hostname(repo_url)
    if not hostname:
        print(f'[ssh] Could not parse hostname from repo URL: {repo_url}')
        return

    # Write SSH config to force the deploy key for this host
    ssh_config_path = os.path.join(ssh_dir, 'config')
    _write_ssh_config(ssh_config_path, hostname, working_key)

    # Add host to known_hosts if not already there
    _ensure_known_hosts(known_hosts_path, hostname)

    print(f'[ssh] SSH configured for {hostname} using key {working_key}')


def _parse_hostname(repo_url):
    """Extract hostname from git SSH URL."""
    if repo_url.startswith('git@'):
        # git@github.com:org/repo.git
        try:
            return repo_url.split('@')[1].split(':')[0]
        except IndexError:
            return None
    elif repo_url.startswith('ssh://'):
        # ssh://git@github.com/org/repo.git
        try:
            without_scheme = repo_url[6:]  # strip ssh://
            if '@' in without_scheme:
                without_scheme = without_scheme.split('@')[1]
            return without_scheme.split('/')[0]
        except IndexError:
            return None
    return None


def _write_ssh_config(config_path, hostname, key_path):
    """Write or update SSH config entry for the git host."""
    entry = (
        f'\n# Added by CX-ConfigGen service\n'
        f'Host {hostname}\n'
        f'    IdentityFile {key_path}\n'
        f'    IdentitiesOnly yes\n'
        f'    StrictHostKeyChecking no\n'
        f'    UserKnownHostsFile /dev/null\n'
    )

    existing = ''
    if os.path.isfile(config_path):
        with open(config_path) as f:
            existing = f.read()

    # Don't duplicate the entry if it's already there
    if f'Host {hostname}' in existing:
        return

    with open(config_path, 'a') as f:
        f.write(entry)

    os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)


def _ensure_known_hosts(known_hosts_path, hostname):
    """Add host fingerprint to known_hosts via ssh-keyscan."""
    # Check if already present
    if os.path.isfile(known_hosts_path):
        with open(known_hosts_path) as f:
            if hostname in f.read():
                return

    try:
        result = subprocess.run(
            ['ssh-keyscan', '-H', hostname],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout:
            os.makedirs(os.path.dirname(known_hosts_path), exist_ok=True)
            with open(known_hosts_path, 'a') as f:
                f.write(result.stdout)
            os.chmod(known_hosts_path, stat.S_IRUSR | stat.S_IWUSR)
            print(f'[ssh] Added {hostname} to known_hosts')
        else:
            print(f'[ssh] ssh-keyscan failed for {hostname} — host key check will be skipped')
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print(f'[ssh] ssh-keyscan not available or timed out for {hostname}')


def get_git_env(app):
    """
    Return an os.environ copy with GIT_SSH_COMMAND set to use the deploy key.
    Uses the working copy of the key if the original mount is read-only.
    """
    env = os.environ.copy()
    key_path = app.config['SSH_KEY_PATH']

    # Use the working copy if it exists, otherwise fall back to the original
    ssh_dir = os.path.expanduser('~/.ssh')
    working_key = os.path.join(ssh_dir, 'deploy_key')
    active_key = working_key if os.path.isfile(working_key) else key_path

    if os.path.isfile(active_key):
        env['GIT_SSH_COMMAND'] = (
            f'ssh -i {active_key} '
            f'-o IdentitiesOnly=yes '
            f'-o StrictHostKeyChecking=no '
            f'-o UserKnownHostsFile=/dev/null'
        )
    return env
