"""
Deploy routes — extends the projects blueprint.

Handles:
  - Deploy mapping editor (hostname → mgmt IP)
  - Dry-run (diff preview)
  - Config push with checkpoint + rollback
"""

import json
import os
import subprocess
import tempfile
import glob
import yaml
from datetime import datetime
from flask import (
    render_template, request, jsonify,
    current_app, abort
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


def _run_playbook(playbook_name, inventory_dict, extra_vars, limit_hosts=None):
    """
    Run an Ansible playbook.

    Returns (success: bool, output: str, results: list[dict])
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write inventory
        inv_path = os.path.join(tmpdir, 'inventory.yml')
        with open(inv_path, 'w') as f:
            yaml.dump(inventory_dict, f, default_flow_style=False)

        # Write extra vars
        extra_vars['results_file'] = os.path.join(tmpdir, 'results')
        os.makedirs(extra_vars['results_file'], exist_ok=True)

        vars_path = os.path.join(tmpdir, 'extra_vars.yml')
        with open(vars_path, 'w') as f:
            yaml.dump(extra_vars, f, default_flow_style=False)

        # Locate playbook
        repo_dir = current_app.config.get('CONFIGGEN_DIR', 'configgen')
        playbook_path = os.path.join(repo_dir, 'playbooks', playbook_name)

        if not os.path.exists(playbook_path):
            return False, f'Playbook not found: {playbook_path}', []

        # Build command
        cmd = [
            'ansible-playbook',
            playbook_path,
            '-i', inv_path,
            '-e', f'@{vars_path}',
        ]
        if limit_hosts:
            cmd.extend(['--limit', ','.join(limit_hosts)])

        # For dry run playbook, add check+diff flags
        if 'dryrun' in playbook_name:
            cmd.extend(['--check', '--diff'])

        env = os.environ.copy()
        env['ANSIBLE_HOST_KEY_CHECKING'] = 'False'
        env['ANSIBLE_STDOUT_CALLBACK'] = 'json'
        env['ANSIBLE_NOCOLOR'] = '1'

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                cwd=repo_dir
            )
        except subprocess.TimeoutExpired:
            return False, 'Playbook execution timed out (5 min limit)', []

        # Collect per-host result files
        results = []
        result_files = glob.glob(os.path.join(
            extra_vars['results_file'], '*.json'
        ))
        for rf in result_files:
            try:
                with open(rf) as f:
                    results.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass

        success = proc.returncode == 0
        output = proc.stdout + proc.stderr

        return success, output, results


# ── Routes ─────────────────────────────────────────────────

@login_required
def project_deploy(project_name):
    """Render the deploy tab."""
    app = current_app._get_current_object()
    username = current_user.username
    mapping = _load_mapping(app, username, project_name)
    config_dir = project_generated_configs_dir(app, username, project_name)

    enriched = []
    for h in mapping['hosts']:
        # NetForge names a full generation <hostname>_FULL.ios; tag-filtered
        # runs produce <hostname>_PARTIAL_<tags>.ios. There is no bare
        # <hostname>.ios, so match the full-run name.
        config_file = os.path.join(config_dir, f"{h['hostname']}_FULL.ios")
        enriched.append({
            'hostname': h['hostname'],
            'mgmt_ip': h.get('mgmt_ip', ''),
            'has_config': os.path.exists(config_file)
        })

    return render_template(
        'project_deploy.html',
        project_name=project_name,
        mappings=enriched,
        settings=mapping.get('settings', {})
    )


@login_required
def deployment_ips(project_name):
    """Manage the hostname to management IP mapping.

    Kept separate from the Hosts page, which is scoped to config generation,
    and from Deploy, which consumes this rather than owning it. Firmware reads
    the same mapping.
    """
    app = current_app._get_current_object()
    mapping = _load_mapping(app, current_user.username, project_name)
    return render_template(
        'project_deployment_ips.html',
        project_name=project_name,
        mappings=mapping.get('hosts', []),
    )


@login_required
def deployment_ips_save(project_name):
    """Persist the mapping, leaving other settings untouched."""
    app = current_app._get_current_object()
    username = current_user.username
    body = request.json or {}

    rows = body.get('mappings') or []
    cleaned = []
    seen = set()
    for r in rows:
        name = (r.get('hostname') or '').strip()
        ip = (r.get('mgmt_ip') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append({'hostname': name, 'mgmt_ip': ip})

    mapping = _load_mapping(app, username, project_name)
    mapping['hosts'] = cleaned
    _save_mapping(app, username, project_name, mapping)
    return jsonify(ok=True, count=len(cleaned))


@login_required
def deploy_save_mapping(project_name):
    """Save the mapping table."""
    data = request.get_json()
    if not data:
        return jsonify(ok=False, error='No data'), 400

    app = current_app._get_current_object()
    username = current_user.username
    mapping_data = {
        'hosts': data.get('mappings', []),
        'settings': {
            'rollback_timeout': data.get('rollback_timeout', 5)
        }
    }
    _save_mapping(app, username, project_name, mapping_data)
    return jsonify(ok=True)


@login_required
def deploy_auto_populate(project_name):
    """Auto-populate mapping from project hosts."""
    from .utils import read_project_hosts

    app = current_app._get_current_object()
    username = current_user.username

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
    data = request.get_json()
    if not data:
        return jsonify(ok=False, error='No data'), 400

    hosts = data.get('hosts', [])
    username = data.get('username', '')
    password = data.get('password', '')

    if not hosts or not username or not password:
        return jsonify(ok=False, error='Missing hosts or credentials'), 400

    app = current_app._get_current_object()
    config_dir = project_generated_configs_dir(
        app, current_user.username, project_name
    )
    inventory = _build_inventory(hosts, username, password)

    extra_vars = {
        'config_dir': config_dir,
        'deploy_username': username,
        'deploy_password': password,
    }

    success, output, results = _run_playbook(
        'deploy_dryrun.yml',
        inventory,
        extra_vars,
        limit_hosts=[h['hostname'] for h in hosts]
    )

    return jsonify(
        ok=success,
        output=output if not success else '',
        results=results
    )


@login_required
def deploy_push(project_name):
    """Run deploy playbook — checkpoint, push, verify, confirm."""
    data = request.get_json()
    if not data:
        return jsonify(ok=False, error='No data'), 400

    hosts = data.get('hosts', [])
    username = data.get('username', '')
    password = data.get('password', '')
    rollback_timeout = data.get('rollback_timeout', 5)

    if not hosts or not username or not password:
        return jsonify(ok=False, error='Missing hosts or credentials'), 400

    app = current_app._get_current_object()
    config_dir = project_generated_configs_dir(
        app, current_user.username, project_name
    )
    inventory = _build_inventory(hosts, username, password)

    extra_vars = {
        'config_dir': config_dir,
        'deploy_username': username,
        'deploy_password': password,
        'rollback_timeout': rollback_timeout,
    }

    success, output, results = _run_playbook(
        'deploy_push.yml',
        inventory,
        extra_vars,
        limit_hosts=[h['hostname'] for h in hosts]
    )

    return jsonify(
        ok=success,
        output=output if not success else '',
        results=results
    )
