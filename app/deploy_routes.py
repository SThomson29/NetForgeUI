"""
Deploy routes — extends the projects blueprint.

Handles:
  - Deploy mapping editor (hostname → mgmt IP)
  - Dry-run (diff preview)
  - Config push with checkpoint + rollback
"""

import os
import re
import uuid
import threading
import yaml

from .ansible_safe import validate_targets
from . import audit, checks
from .api import json_body, bad_request
from .jobs import run_ansible_job, prune_finished, mark_finished, busy_hosts
from .security import job_tag, visible_job
from flask import (
    render_template, jsonify,
    current_app, abort, g
)
from flask_login import login_required, current_user

from .project import project_dir as _project_dir_base, project_generated_configs_dir


# ── Helpers ────────────────────────────────────────────────

def _mapping_path(app, username, project_name):
    return os.path.join(
        _project_dir_base(app, username, project_name),
        'deploy_mapping.yml'
    )


def _load_mapping(app, username, project_name):
    """Load deploy mapping from YAML."""
    path = _mapping_path(app, username, project_name)
    if not os.path.exists(path):
        return {'hosts': [], 'settings': {'rollback_timeout': 5}}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {
        'hosts': data.get('hosts', []),
        'settings': data.get('settings', {'rollback_timeout': 5})
    }


def _save_mapping(app, username, project_name, mapping_data):
    """Save deploy mapping to YAML."""
    path = _mapping_path(app, username, project_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        yaml.dump(mapping_data, f, default_flow_style=False)


def _build_inventory(hosts, username, password):
    """
    Build a dynamic Ansible inventory dict from the mapping + creds.
    Returns a dict suitable for writing to a YAML inventory file.
    """
    inventory = {
        'all': {
            'hosts': {},
            'vars': {
                'ansible_network_os': 'arubanetworks.aoscx.aoscx',
                'ansible_connection': 'network_cli',
                'ansible_user': username,
                'ansible_password': password,
                'ansible_become': True,
                'ansible_become_method': 'enable',
                'ansible_become_password': password,
            }
        }
    }
    for h in hosts:
        hostvars = {'ansible_host': h['mgmt_ip']}
        # Optional per-host override of which generated file to deploy.
        # Set as a host var rather than an extra-var: a deploy run covers
        # several hosts and each needs its own file. Omitted means the
        # playbook falls back to <config_dir>/<hostname>_FULL.ios.
        if h.get('config_file'):
            hostvars['deploy_config_file'] = h['config_file']
        inventory['all']['hosts'][h['hostname']] = hostvars
    return inventory


# A generated config name, as the download route accepts it: no separators,
# and no leading dot, so neither '..' nor hidden files can be named.
_CONFIG_NAME = re.compile(r'^[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,127}\.ios$')


def _resolve_config_files(hosts, config_dir):
    """Turn each host's config_file name into a path inside config_dir.

    The playbook treats deploy_config_file as a full path on this machine and
    sends whatever it names to the switch. Taking a path from the request
    let a user name any file in the container — users.db, other users'
    projects, the git key — and have it sent to an address they chose. So
    only a bare file name is accepted, and it must be one of this project's
    generated configs; the path itself is built here.

    Returns (hosts_with_paths, problems).
    """
    resolved, problems = [], []
    for h in hosts:
        h = dict(h)
        name = h.get('config_file')
        if name:
            name = str(name)
            path = os.path.join(config_dir, name)
            if (not _CONFIG_NAME.match(name) or os.path.islink(path)
                    or not os.path.isfile(path)):
                problems.append('unknown config file for %s' % h['hostname'])
            else:
                h['config_file'] = path
        resolved.append(h)
    return resolved, problems


# The playbook does rollback_timeout | int, so anything that is not a number
# becomes 0 and the rollback fires immediately after the push.
MAX_ROLLBACK_MINUTES = 60


def _rollback_minutes(value):
    """Whole minutes between 1 and MAX_ROLLBACK_MINUTES, or None."""
    if isinstance(value, bool):
        return None
    try:
        mins = int(value)
    except (TypeError, ValueError):
        return None
    if mins != value and str(mins) != str(value).strip():
        return None     # 2.5, '5 mins'
    return mins if 1 <= mins <= MAX_ROLLBACK_MINUTES else None


_ROLLBACK_ERROR = ('Rollback timeout must be a whole number of minutes '
                   'between 1 and %d.' % MAX_ROLLBACK_MINUTES)


# ── Job store ──────────────────────────────────────────────
#
# Deploy runs can take minutes — an SSH connect to an unreachable switch
# blocks until it times out. Running synchronously gave the page nothing to
# show but a spinner, with no way to tell a slow connection from a wedged
# one. These run in a thread with output streamed line by line, the same way
# firmware does.

_deploy_jobs = {}
_deploy_jobs_lock = threading.Lock()

DEPLOY_TIMEOUT_SECS = 30 * 60


def _run_playbook_job(job_id, playbook_name, repo_dir, ansible_bin,
                      inventory_dict, extra_vars, limit_hosts=None):
    """Run a deploy playbook, streaming output into the job store.

    The dry run adds --check --diff: the playbook itself is the same shape
    as a push, and check mode is what keeps it from changing the switch.
    """
    run_ansible_job(
        _deploy_jobs, _deploy_jobs_lock, job_id,
        playbook_path=os.path.join(repo_dir, 'playbooks', playbook_name),
        repo_dir=repo_dir, ansible_bin=ansible_bin,
        inventory=inventory_dict, extra_vars=extra_vars,
        limit_hosts=limit_hosts,
        extra_args=['--check', '--diff'] if 'dryrun' in playbook_name else [],
        timeout_secs=DEPLOY_TIMEOUT_SECS,
    )


def _start_deploy_job(app, playbook_name, inventory, extra_vars, limit_hosts,
                      owner=None, project_name=None, audit_id=None):
    """Kick off a playbook in the background. Returns the job id."""
    prune_finished(_deploy_jobs, _deploy_jobs_lock)
    job_id = str(uuid.uuid4())
    with _deploy_jobs_lock:
        _deploy_jobs[job_id] = {
            'status': 'starting', 'output': '',
            'returncode': None, 'results': [],
            'playbook': playbook_name,
            '_hosts': list(limit_hosts or ()),
            **job_tag(owner, project_name),
        }

    repo_dir = app.config['CONFIGGEN_REPO']
    ansible_bin = app.config.get('ANSIBLE_BIN', 'ansible-playbook')

    def _worker():
        try:
            _run_playbook_job(job_id, playbook_name, repo_dir, ansible_bin,
                              inventory, extra_vars, limit_hosts)
        except Exception as ex:                     # noqa: BLE001
            with _deploy_jobs_lock:
                _deploy_jobs[job_id]['status'] = 'failed'
                _deploy_jobs[job_id]['returncode'] = -1
                _deploy_jobs[job_id]['output'] += '\n%s\n' % ex
        finally:
            with _deploy_jobs_lock:
                mark_finished(_deploy_jobs[job_id])
                final = dict(_deploy_jobs[job_id])
            try:
                audit.record_finish(app, audit_id, final)
            except Exception:                       # noqa: BLE001
                pass    # never let the log take down the job's own result

    threading.Thread(target=_worker, daemon=True).start()
    return job_id


def _project_key(project_name):
    return '%s/%s' % (g.project_owner, project_name)


def switches_in_use(project_key, hostnames):
    """A message naming any of hostnames a push or firmware upgrade in
    this project is still working on, or None. Two at once against one
    switch would fight over its checkpoint and rollback."""
    from .firmware_routes import _fw_jobs, _fw_jobs_lock
    busy = busy_hosts(_deploy_jobs, _deploy_jobs_lock, project_key, hostnames,
                      lambda job: job.get('playbook') == 'deploy_push.yml')
    busy.update(busy_hosts(_fw_jobs, _fw_jobs_lock, project_key, hostnames))
    if not busy:
        return None
    return 'Already being changed: %s. Try again when that finishes.' % ', '.join(
        '%s (started by %s)' % (h, who) for h, who in sorted(busy.items()))


# ── Routes ─────────────────────────────────────────────────

def push_states(app, username, project_name, mapping=None):
    """Each mapped switch's config against what it last confirmed from a
    push: push_state is 'changed', 'never', 'same' or 'none' (no config).
    The Deploy page ticks 'changed' and 'never'; the projects list counts
    them."""
    if mapping is None:
        mapping = _load_mapping(app, username, project_name)
    config_dir = project_generated_configs_dir(app, username, project_name)
    pushed = audit.last_pushed(app, username, project_name)
    out = []
    for h in mapping['hosts']:
        # NetForge names a full generation <hostname>_FULL.ios; tag-filtered
        # runs produce <hostname>_PARTIAL_<tags>.ios. There is no bare
        # <hostname>.ios, so match the full-run name.
        config_file = os.path.join(config_dir, f"{h['hostname']}_FULL.ios")
        has_config = os.path.exists(config_file)
        last = pushed.get(h['hostname'])
        if not has_config:
            state = 'none'
        elif not last:
            state = 'never'
        elif last['sha256'] == audit.file_fingerprint(config_file):
            state = 'same'
        else:
            state = 'changed'
        out.append({
            'hostname': h['hostname'],
            'mgmt_ip': h.get('mgmt_ip', ''),
            'has_config': has_config,
            'push_state': state,
            'last_push': last,
        })
    return out


@login_required
def project_deploy(project_name):
    """Render the deploy tab."""
    app = current_app._get_current_object()
    username = g.project_owner
    mapping = _load_mapping(app, username, project_name)
    enriched = push_states(app, username, project_name, mapping)

    return render_template(
        'project_deploy.html',
        project_name=project_name,
        mappings=enriched,
        settings=mapping.get('settings', {}),
        checks_summary=checks.summary(app, username, project_name),
    )


@login_required
def deployment_ips(project_name):
    """Manage the hostname to management IP mapping.

    Kept separate from the Hosts page, which is scoped to config generation,
    and from Deploy, which consumes this rather than owning it. Firmware reads
    the same mapping.
    """
    app = current_app._get_current_object()
    mapping = _load_mapping(app, g.project_owner, project_name)
    return render_template(
        'project_deployment_ips.html',
        project_name=project_name,
        mappings=mapping.get('hosts', []),
    )


@login_required
def deployment_ips_save(project_name):
    """Persist the mapping, leaving other settings untouched."""
    app = current_app._get_current_object()
    username = g.project_owner
    body = json_body()

    rows = body.get('mappings') or []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        bad_request('mappings must be a list of objects.')
    cleaned = []
    seen = set()
    for r in rows:
        name = str(r.get('hostname') or '').strip()
        ip = str(r.get('mgmt_ip') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append({'hostname': name, 'mgmt_ip': ip})

    # These names become Ansible inventory keys and --limit patterns.
    problems = validate_targets(cleaned)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400

    mapping = _load_mapping(app, username, project_name)
    mapping['hosts'] = cleaned
    _save_mapping(app, username, project_name, mapping)
    return jsonify(ok=True, count=len(cleaned))


@login_required
def deploy_save_mapping(project_name):
    """Save the mapping table."""
    data = json_body()

    rows = data.get('mappings', [])
    problems = validate_targets(rows)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400
    rollback = _rollback_minutes(data.get('rollback_timeout', 5))
    if rollback is None:
        return jsonify(ok=False, error=_ROLLBACK_ERROR), 400

    app = current_app._get_current_object()
    username = g.project_owner
    mapping_data = {
        # Only the fields the mapping holds — nothing else from the request.
        'hosts': [{'hostname': str(r.get('hostname')),
                   'mgmt_ip': str(r.get('mgmt_ip') or '')} for r in rows],
        'settings': {
            'rollback_timeout': rollback
        }
    }
    _save_mapping(app, username, project_name, mapping_data)
    return jsonify(ok=True)


@login_required
def deploy_auto_populate(project_name):
    """Auto-populate mapping from project hosts."""
    from .utils import read_project_hosts

    app = current_app._get_current_object()
    username = g.project_owner

    try:
        hosts = read_project_hosts(app, username, project_name)
    except Exception:
        hosts = []

    mapping = _load_mapping(app, username, project_name)
    existing_names = {h['hostname'] for h in mapping['hosts']}

    for h in hosts:
        name = h if isinstance(h, str) else h.get('hostname', h.get('name', ''))
        if name and name not in existing_names:
            mapping['hosts'].append({
                'hostname': name,
                'mgmt_ip': ''
            })

    _save_mapping(app, username, project_name, mapping)
    return jsonify(ok=True)


@login_required
def deploy_dryrun(project_name):
    """Run dry-run playbook — returns diffs per host."""
    data = json_body()

    hosts = data.get('hosts', [])
    username = data.get('username', '')
    password = data.get('password', '')

    if not hosts or not username or not password:
        return jsonify(ok=False, error='Missing hosts or credentials'), 400

    problems = validate_targets(hosts)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400

    app = current_app._get_current_object()
    config_dir = project_generated_configs_dir(
        app, g.project_owner, project_name
    )
    hosts, problems = _resolve_config_files(hosts, config_dir)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400
    inventory = _build_inventory(hosts, username, password)

    extra_vars = {
        'config_dir': config_dir,
        'deploy_username': username,
        'deploy_password': password,
    }

    audit_id = audit.record_start(
        app, username=current_user.username, owner=g.project_owner,
        project=project_name,
        action='deploy_dryrun', switch_user=username,
        targets=audit.deploy_targets(hosts, config_dir), params={})
    job_id = _start_deploy_job(
        app, 'deploy_dryrun.yml', inventory, extra_vars,
        limit_hosts=[h['hostname'] for h in hosts],
        owner=current_user.username, project_name=_project_key(project_name),
        audit_id=audit_id,
    )
    return jsonify(ok=True, job_id=job_id)


@login_required
def reachability(project_name):
    """TCP-probe the mapped switches on the ports deploy and firmware use.

    POST {"hosts": [hostname, ...]}; omitted or empty means every mapped
    switch. Addresses are looked up in the saved mapping — the request
    names switches, never addresses.
    """
    from . import reachability as reach
    body = json_body()
    wanted = body.get('hosts') or []
    if not isinstance(wanted, list):
        bad_request('hosts must be a list of hostnames.')
    mapping = _load_mapping(current_app._get_current_object(),
                            g.project_owner, project_name)
    hosts = [h for h in mapping.get('hosts', [])
             if isinstance(h, dict) and (not wanted or h.get('hostname') in wanted)]
    # The mapping may predate validation.
    hosts = [h for h in hosts if not validate_targets([h])]
    return jsonify(ok=True, results=reach.check(hosts))


@login_required
def deploy_status(project_name, job_id):
    """Poll a running or finished deploy job."""
    with _deploy_jobs_lock:
        snapshot = visible_job(_deploy_jobs.get(job_id),
                               current_user.username, _project_key(project_name))
    if not snapshot:
        abort(404)
    return jsonify(snapshot)


@login_required
def deploy_push(project_name):
    """Run deploy playbook — checkpoint, push, verify, confirm."""
    data = json_body()

    hosts = data.get('hosts', [])
    username = data.get('username', '')
    password = data.get('password', '')
    rollback_timeout = _rollback_minutes(data.get('rollback_timeout', 5))
    if rollback_timeout is None:
        return jsonify(ok=False, error=_ROLLBACK_ERROR), 400

    if not hosts or not username or not password:
        return jsonify(ok=False, error='Missing hosts or credentials'), 400

    problems = validate_targets(hosts)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400

    app = current_app._get_current_object()
    config_dir = project_generated_configs_dir(
        app, g.project_owner, project_name
    )
    hosts, problems = _resolve_config_files(hosts, config_dir)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400
    busy = switches_in_use(_project_key(project_name), [h['hostname'] for h in hosts])
    if busy:
        return jsonify(ok=False, error=busy), 409
    inventory = _build_inventory(hosts, username, password)

    extra_vars = {
        'config_dir': config_dir,
        'deploy_username': username,
        'deploy_password': password,
        'rollback_timeout': rollback_timeout,
    }

    audit_id = audit.record_start(
        app, username=current_user.username, owner=g.project_owner,
        project=project_name,
        action='deploy_push', switch_user=username,
        targets=audit.deploy_targets(hosts, config_dir),
        params={'rollback_timeout': rollback_timeout})
    job_id = _start_deploy_job(
        app, 'deploy_push.yml', inventory, extra_vars,
        limit_hosts=[h['hostname'] for h in hosts],
        owner=current_user.username, project_name=_project_key(project_name),
        audit_id=audit_id,
    )
    return jsonify(ok=True, job_id=job_id)
