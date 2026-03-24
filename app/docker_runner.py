"""
docker_runner.py — sandboxed Ansible execution via ephemeral Docker containers.

Each generate run spawns a fresh container from a minimal Ansible image.
The container mounts the same named volumes as the NetForgeUI container,
so no host paths are needed — this works on any OS including Mac with Docker Desktop.

Volume layout inside the ephemeral container:
    /ansible        ← netforgeui_repo volume (configgen repo, read-only)
    /data           ← netforgeui_data volume (all user data, read-only)
    /output         ← netforgeui_data volume (writable, same volume, different mount)

The inventory path and output path inside the container are derived from the
known structure of the data volume relative to /data.

The container is named netforge-generate-<job_id> so the startup cleanup
in run.py can find and remove any orphaned containers from previous crashes.
"""

import os
import logging

log = logging.getLogger(__name__)

DEFAULT_ANSIBLE_IMAGE = os.environ.get('ANSIBLE_IMAGE', 'cytopia/ansible:latest')
CONTAINER_PREFIX = 'netforge-generate-'

# Named volume names — must match what is configured in docker-compose / Terraform
REPO_VOLUME  = os.environ.get('REPO_VOLUME_NAME',  'netforgeui_repo')
DATA_VOLUME  = os.environ.get('DATA_VOLUME_NAME',  'netforgeui_data')


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


def run_generate(job_id, username, project_name, data_dir, limit=None, tags=None):
    """
    Run ansible-playbook in an ephemeral Docker container.

    Mounts the named volumes directly — no host paths required.
    The data volume is mounted read-only at /data; a separate writable mount
    covers just the output directory within the same volume.

    Args:
        job_id:       UUID string for this job
        username:     NetForgeUI username
        project_name: Project name
        data_dir:     Container-internal DATA_DIR (e.g. /app/service/data)
        limit:        Optional ansible --limit string
        tags:         Optional ansible --tags string

    Returns:
        (returncode, output_text)

    Raises:
        RuntimeError if Docker socket is unavailable
    """
    client = _get_client()

    # Derive paths inside the ephemeral container from known volume structure.
    # The data volume is mounted at /data — it contains users/ and users.db at its root.
    inventory_path = f'/data/users/{username}/projects/{project_name}/hosts.ini'
    output_path    = f'/data/users/{username}/projects/{project_name}/generated_configs'

    # Pull image if not present
    try:
        client.images.get(DEFAULT_ANSIBLE_IMAGE)
    except Exception:
        log.info(f'[docker_runner] Pulling image {DEFAULT_ANSIBLE_IMAGE} ...')
        client.images.pull(DEFAULT_ANSIBLE_IMAGE)

    cmd = [
        'ansible-playbook',
        '-i', inventory_path,
        '/ansible/playbooks/generate_configs.yml',
        '-e', f'config_output_dir={output_path}',
    ]
    if limit:
        cmd += ['--limit', limit]
    if tags:
        cmd += ['--tags', tags]

    # Mount named volumes directly — works on any OS, no host paths needed
    volumes = {
        REPO_VOLUME: {
            'bind': '/ansible',
            'mode': 'ro',
        },
        DATA_VOLUME: {
            'bind': '/data',
            'mode': 'rw',
        },
    }

    environment = {
        'ANSIBLE_ROLES_PATH': '/ansible/roles',
        'ANSIBLE_HOST_KEY_CHECKING': 'False',
        'ANSIBLE_STDOUT_CALLBACK': 'default',
        'ANSIBLE_FORCE_COLOR': '0',
    }

    container_name = f'{CONTAINER_PREFIX}{job_id}'
    log.info(f'[docker_runner] Starting container {container_name}')
    log.info(f'[docker_runner] inventory={inventory_path} output={output_path}')

    try:
        container = client.containers.run(
            image=DEFAULT_ANSIBLE_IMAGE,
            command=cmd,
            name=container_name,
            volumes=volumes,
            environment=environment,
            working_dir='/ansible',
            network_mode='none',
            auto_remove=False,
            detach=True,
            stdout=True,
            stderr=True,
        )

        result = container.wait()
        returncode = result.get('StatusCode', -1)
        output = container.logs(stdout=True, stderr=True).decode('utf-8', errors='replace')

        try:
            container.remove(force=True)
        except Exception:
            pass

    except Exception as e:
        output = str(e)
        returncode = -1
        log.error(f'[docker_runner] Container {container_name} error: {output[:200]}')

    log.info(f'[docker_runner] Container {container_name} finished rc={returncode}')
    return returncode, output
