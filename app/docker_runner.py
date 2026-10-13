"""
docker_runner.py — sandboxed Ansible execution via ephemeral Docker containers.

Each generate run spawns a fresh container from a minimal Ansible image with:

    /ansible        <- the configgen repo volume, read-only
    /work/project   <- a COPY of the one project being generated

The data volume is deliberately NOT mounted. It holds every user's projects
and the users database, and Ansible templates variable values — so a crafted
value in a host_vars file would otherwise be able to read other users'
credentials or rewrite users.db. Copying in only the one project means the
worst such a value can do is affect a copy of the project it came from.

The project's hosts.ini, host_vars/ and generated_configs/ are copied in with
put_archive; afterwards generated_configs/ is copied back out with
get_archive and mirrored onto the real project. The returned archive is
treated as untrusted: only plain .ios files with safe names are accepted.

The container has no network (network_mode='none') and no Docker socket.

The container is named netforge-generate-<job_id> so the startup cleanup
in run.py can find and remove any orphaned containers from previous crashes.
"""

import io
import os
import re
import tarfile
import logging

log = logging.getLogger(__name__)

DEFAULT_ANSIBLE_IMAGE = os.environ.get('ANSIBLE_IMAGE', 'cytopia/ansible:latest')
CONTAINER_PREFIX = 'netforge-generate-'

# Named volume names — must match what is configured in docker-compose / Terraform.
# DATA_VOLUME_NAME is still read so existing deployments that set it keep
# starting cleanly, but the data volume is no longer mounted into the
# ephemeral container — see the module docstring.
REPO_VOLUME  = os.environ.get('REPO_VOLUME_NAME',  'netforgeui_repo')
DATA_VOLUME  = os.environ.get('DATA_VOLUME_NAME',  'netforgeui_data')

# Where the copied project lives inside the ephemeral container.
WORK_PROJECT = '/work/project'

# Generated outputs are flat <hostname>_*.ios files. Anything else coming
# back out of the container is ignored.
_OUTPUT_NAME = re.compile(r'^[A-Za-z0-9_.\-]+\.ios$')

# The returned archive is untrusted, so cap how much of it is read.
MAX_OUTPUT_BYTES = 50 * 1024 * 1024


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


def _skip_links(tarinfo):
    """tarfile filter: never follow or copy symlinks into the container."""
    if tarinfo.issym() or tarinfo.islnk():
        return None
    return tarinfo


def _build_project_archive(project_root):
    """Tar the parts of one project that generation needs.

    Rooted at work/project/ so it can be put_archive'd straight to '/'. The
    directories are always present, even when empty, so the playbook's
    output directory exists and get_archive has something to return.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for d in ('work', 'work/project', 'work/project/host_vars',
                  'work/project/generated_configs'):
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)

        hosts = os.path.join(project_root, 'hosts.ini')
        if os.path.isfile(hosts) and not os.path.islink(hosts):
            tar.add(hosts, arcname='work/project/hosts.ini', filter=_skip_links)

        for sub in ('host_vars', 'generated_configs'):
            src = os.path.join(project_root, sub)
            if not os.path.isdir(src) or os.path.islink(src):
                continue
            for name in sorted(os.listdir(src)):
                full = os.path.join(src, name)
                if os.path.islink(full):
                    continue
                tar.add(full, arcname='work/project/%s/%s' % (sub, name),
                        recursive=True, filter=_skip_links)
    return buf.getvalue()


def _read_output_archive(stream):
    """Pull the generated .ios files out of a get_archive stream.

    The archive comes back from a container that ran user-influenced
    templates, so it is treated as untrusted: only regular files directly
    inside generated_configs/ with a plain .ios name are accepted. Links,
    nested paths and anything oversized are dropped.
    """
    chunks, total = [], 0
    for chunk in stream:
        total += len(chunk)
        if total > MAX_OUTPUT_BYTES:
            raise RuntimeError('Generated output exceeded %d bytes; refusing it.'
                               % MAX_OUTPUT_BYTES)
        chunks.append(chunk)

    files = {}
    with tarfile.open(fileobj=io.BytesIO(b''.join(chunks)), mode='r') as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split('/')
            if len(parts) != 2 or parts[0] != 'generated_configs':
                continue
            name = parts[1]
            if not _OUTPUT_NAME.match(name):
                continue
            fh = tar.extractfile(member)
            if fh is not None:
                files[name] = fh.read()
    return files


def _mirror_outputs(out_dir, files):
    """Make the project's generated_configs match what the run produced.

    A mirror rather than a merge: the playbook deletes section files that
    rendered empty, and those must disappear here too, or a section that
    used to have content would survive and keep being reassembled.
    """
    os.makedirs(out_dir, exist_ok=True)
    for name, content in files.items():
        dest = os.path.join(out_dir, name)
        tmp = dest + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, dest)

    for existing in os.listdir(out_dir):
        path = os.path.join(out_dir, existing)
        if (existing.endswith('.ios') and existing not in files
                and os.path.isfile(path) and not os.path.islink(path)):
            os.remove(path)


def run_generate(job_id, username, project_name, data_dir, limit=None, tags=None,
                 progress_callback=None):
    """
    Run ansible-playbook for one project in an isolated ephemeral container.

    Args:
        job_id:       UUID string for this job
        username:     NetForgeUI username
        project_name: Project name
        data_dir:     DATA_DIR as seen by this (the NetForgeUI) container
        limit:        Optional ansible --limit string
        tags:         Optional ansible --tags string
        progress_callback: Optional callable(output_so_far) called as log lines arrive

    Returns:
        (returncode, output_text)

    Raises:
        RuntimeError if Docker socket is unavailable
    """
    client = _get_client()

    project_root = os.path.join(data_dir, username, 'projects', project_name)
    out_dir = os.path.join(project_root, 'generated_configs')

    inventory_path = WORK_PROJECT + '/hosts.ini'
    output_path    = WORK_PROJECT + '/generated_configs'

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

    # Only the repo is mounted. The project is copied in below.
    volumes = {
        REPO_VOLUME: {
            'bind': '/ansible',
            'mode': 'ro',
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

    container = None
    output = ''
    returncode = -1
    try:
        container = client.containers.create(
            image=DEFAULT_ANSIBLE_IMAGE,
            command=cmd,
            name=container_name,
            volumes=volumes,
            environment=environment,
            working_dir='/ansible',
            network_mode='none',
        )
        container.put_archive('/', _build_project_archive(project_root))
        container.start()

        # Stream logs as they arrive so the frontend can show progress
        output_parts = []
        for chunk in container.logs(stream=True, follow=True):
            line = chunk.decode('utf-8', errors='replace')
            output_parts.append(line)
            if progress_callback:
                progress_callback(''.join(output_parts))

        output = ''.join(output_parts)

        result = container.wait()
        returncode = result.get('StatusCode', -1)

        # Mirror back whatever the run produced, success or not — the same
        # as when it wrote into the real directory directly.
        stream, _stat = container.get_archive(output_path)
        _mirror_outputs(out_dir, _read_output_archive(stream))

    except Exception as e:
        output = (output + '\n' if output else '') + str(e)
        returncode = -1
        log.error(f'[docker_runner] Container {container_name} error: {str(e)[:200]}')
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass

    log.info(f'[docker_runner] Container {container_name} finished rc={returncode}')
    return returncode, output
