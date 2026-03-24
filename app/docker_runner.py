"""
docker_runner.py — sandboxed Ansible execution via ephemeral Docker containers.

Each generate run spawns a fresh container from a minimal Ansible image.
The container is destroyed on exit (auto_remove=True) so no state is retained
between runs and user-supplied YAML values cannot affect the NetForgeUI process.

Mounts (all host paths, read-only except output):
    /ansible        ← configgen repo  (roles, playbooks, templates)
    /inventory      ← hosts.ini + host_vars/  (project workspace)
    /output         ← generated_configs/  (writable)

The container is named netforge-generate-<job_id> so the startup cleanup
in run.py can find and remove any orphaned containers from previous crashes.
"""

import os
import logging

log = logging.getLogger(__name__)

# Lightweight official Ansible image. Override with ANSIBLE_IMAGE env var.
DEFAULT_ANSIBLE_IMAGE = 'cytopia/ansible:latest'

CONTAINER_PREFIX = 'netforge-generate-'


def _get_client():
    """Return a Docker client or raise RuntimeError if socket unavailable."""
    try:
        import docker
    except ImportError:
        raise RuntimeError(
            'docker Python package is not installed. '
            'Add "docker" to requirements.txt.'
        )

    socket_path = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')
    if not os.path.exists(socket_path):
        raise RuntimeError(
            f'Docker socket not found at {socket_path}. '
            'Mount the Docker socket into the NetForgeUI container: '
            '-v /var/run/docker.sock:/var/run/docker.sock'
        )

    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as e:
        raise RuntimeError(f'Cannot connect to Docker daemon: {e}')


def cleanup_orphaned_containers():
    """
    Remove any netforge-generate-* containers left over from a previous crash.
    Called once at NetForgeUI startup.
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        log.warning(f'[docker_runner] Skipping orphan cleanup: {e}')
        return

    try:
        containers = client.containers.list(
            all=True,
            filters={'name': CONTAINER_PREFIX}
        )
        for c in containers:
            log.warning(f'[docker_runner] Removing orphaned container: {c.name}')
            try:
                c.remove(force=True)
            except Exception as e:
                log.error(f'[docker_runner] Failed to remove {c.name}: {e}')
        if containers:
            log.info(f'[docker_runner] Cleaned up {len(containers)} orphaned container(s)')
    except Exception as e:
        log.error(f'[docker_runner] Orphan cleanup failed: {e}')


def run_generate(job_id, configgen_repo, hosts_ini_path, host_vars_dir,
                 output_dir, playbook_rel, roles_rel, limit=None, tags=None):
    """
    Run ansible-playbook in an ephemeral Docker container.

    Args:
        job_id:          UUID string for this job — used to name the container
        configgen_repo:  Absolute host path to the NetForge repo
        hosts_ini_path:  Absolute host path to the project hosts.ini
        host_vars_dir:   Absolute host path to the project host_vars/ directory
        output_dir:      Absolute host path to the generated_configs/ directory
        playbook_rel:    Path to the playbook relative to configgen_repo
        roles_rel:       Path to roles/ relative to configgen_repo
        limit:           Optional ansible --limit string
        tags:            Optional ansible --tags string

    Returns:
        (returncode, output_text)

    Raises:
        RuntimeError if Docker socket is unavailable
    """
    client = _get_client()  # hard-fail if socket missing

    image = os.environ.get('ANSIBLE_IMAGE', DEFAULT_ANSIBLE_IMAGE)

    # Pull image if not present
    try:
        client.images.get(image)
    except Exception:
        log.info(f'[docker_runner] Pulling image {image} ...')
        client.images.pull(image)

    # Build the ansible-playbook command
    playbook_in_container = f'/ansible/{playbook_rel}'
    cmd = [
        'ansible-playbook',
        '-i', '/inventory/hosts.ini',
        playbook_in_container,
        '-e', 'config_output_dir=/output',
    ]
    if limit:
        cmd += ['--limit', limit]
    if tags:
        cmd += ['--tags', tags]

    # Bind mounts
    volumes = {
        configgen_repo: {
            'bind': '/ansible',
            'mode': 'ro',
        },
        hosts_ini_path: {
            'bind': '/inventory/hosts.ini',
            'mode': 'ro',
        },
        host_vars_dir: {
            'bind': '/inventory/host_vars',
            'mode': 'ro',
        },
        output_dir: {
            'bind': '/output',
            'mode': 'rw',
        },
    }

    # Environment inside the container
    environment = {
        'ANSIBLE_ROLES_PATH': f'/ansible/{roles_rel}',
        'ANSIBLE_HOST_KEY_CHECKING': 'False',
        'ANSIBLE_STDOUT_CALLBACK': 'default',
        'ANSIBLE_FORCE_COLOR': '0',
    }

    container_name = f'{CONTAINER_PREFIX}{job_id}'
    log.info(f'[docker_runner] Starting container {container_name}')

    try:
        container = client.containers.run(
            image=image,
            command=cmd,
            name=container_name,
            volumes=volumes,
            environment=environment,
            working_dir='/ansible',
            network_mode='none',       # no network access needed
            read_only=False,           # output mount needs writes
            auto_remove=True,          # Docker removes on exit
            detach=False,              # block until complete
            stdout=True,
            stderr=True,
        )
        # container is bytes when detach=False and auto_remove=True
        output = container.decode('utf-8', errors='replace') if isinstance(container, bytes) else str(container)
        returncode = 0
    except Exception as e:
        import docker.errors
        if hasattr(e, 'exit_status'):
            # ContainerError — playbook ran but exited non-zero
            output = e.stderr.decode('utf-8', errors='replace') if isinstance(e.stderr, bytes) else str(e.stderr)
            returncode = e.exit_status
        else:
            output = str(e)
            returncode = -1
        log.error(f'[docker_runner] Container {container_name} failed (rc={returncode}): {output[:200]}')

    log.info(f'[docker_runner] Container {container_name} finished rc={returncode}')
    return returncode, output
