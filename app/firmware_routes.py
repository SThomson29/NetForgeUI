"""
Firmware routes — extends the projects blueprint.

Handles:
  - Listing .swi images available on the host
  - Running the firmware upgrade playbook as a background job
  - Polling job status and per-host results

Why this does not reuse the deploy runner
-----------------------------------------
Deploy runs its playbook synchronously with a 300s subprocess timeout, and
gunicorn is started with --timeout 120, so a long request would be killed by
the worker before the subprocess limit was reached.

A firmware upgrade is upload (which polls for up to 10 minutes) plus a boot,
reboot and reconnect — realistically 15-20 minutes. So this follows the
job model used by generation instead: kick off a thread, return a job_id
immediately, and let the UI poll. Output is streamed line by line so progress
is visible while it runs.
"""

import os
import re
import json
import glob
import uuid
import shlex
import yaml
import tempfile
import threading
import subprocess

from flask import request, jsonify, current_app, abort
from flask_login import login_required, current_user

from .project import project_dir as _project_dir_base


# ── Job store ──────────────────────────────────────────────
#
# Separate from the generate job store in projects.py to avoid a circular
# import; firmware_routes is imported *by* projects.py.

_fw_jobs = {}
_fw_jobs_lock = threading.Lock()

# Hard ceiling on a run. Generous: a non-failsafe update can involve several
# reboots. Well beyond the playbook's own 900s wait_for_connection.
PLAYBOOK_TIMEOUT_SECS = 45 * 60

VALID_PARTITIONS = ('primary', 'secondary')


# ── Helpers ────────────────────────────────────────────────

def list_firmware_images(app):
    """Return the .swi filenames available, sorted.

    Names only — never paths. The caller resolves a name back to a path via
    resolve_image_path(), which is what keeps a crafted name from escaping
    the firmware directory.
    """
    fw_dir = app.config.get('FIRMWARE_DIR')
    if not fw_dir or not os.path.isdir(fw_dir):
        return []
    # Filter through resolve_image_path so the list can only ever contain
    # names that will also be accepted on submit — otherwise the UI could
    # offer an entry (a symlink out of the directory, say) that is then
    # rejected.
    return sorted(
        f for f in os.listdir(fw_dir)
        if f.endswith('.swi') and resolve_image_path(app, f)
    )


def resolve_image_path(app, image_name):
    """Resolve a submitted image name to an absolute path, or None.

    Rejects anything that is not a plain filename sitting directly in the
    firmware directory — no separators, no traversal, no symlinks pointing
    elsewhere.
    """
    if not image_name or not image_name.endswith('.swi'):
        return None
    if os.path.basename(image_name) != image_name:
        return None

    fw_dir = app.config.get('FIRMWARE_DIR')
    if not fw_dir:
        return None

    candidate = os.path.realpath(os.path.join(fw_dir, image_name))
    if os.path.dirname(candidate) != os.path.realpath(fw_dir):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


def _load_mapping(app, username, project_name):
    """Reuse the deploy mapping — hostname to management IP."""
    from .deploy_routes import _load_mapping as _deploy_load_mapping
    return _deploy_load_mapping(app, username, project_name)


def _build_inventory(hosts, username, password):
    """Inventory for the firmware playbook.

    Connection is deliberately NOT set here. The playbook switches between
    the REST and SSH connection plugins per play, and a host var would
    override the play var and break play 2.
    """
    inventory = {'all': {'hosts': {}, 'vars': {
        'ansible_user': username,
        'ansible_password': password,
    }}}
    for h in hosts:
        inventory['all']['hosts'][h['hostname']] = {
            'ansible_host': h['mgmt_ip'],
        }
    return inventory


def _run_firmware_playbook(job_id, playbook_path, repo_dir, ansible_bin,
                           inventory_dict, extra_vars, limit_hosts=None):
    """Run the playbook, streaming output into the job store.

    Runs inside a worker thread, so everything it needs is passed in —
    there is no application context here and current_app would fail.
    """
    def _set(**kwargs):
        with _fw_jobs_lock:
            _fw_jobs[job_id].update(kwargs)

    def _append(line):
        with _fw_jobs_lock:
            _fw_jobs[job_id]['output'] += line

    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = os.path.join(tmpdir, 'inventory.yml')
        with open(inv_path, 'w') as f:
            yaml.dump(inventory_dict, f, default_flow_style=False)

        results_dir = os.path.join(tmpdir, 'results')
        os.makedirs(results_dir, exist_ok=True)
        extra_vars['results_file'] = results_dir

        vars_path = os.path.join(tmpdir, 'extra_vars.yml')
        with open(vars_path, 'w') as f:
            yaml.dump(extra_vars, f, default_flow_style=False)

        cmd = [ansible_bin, playbook_path, '-i', inv_path, '-e', '@' + vars_path]
        if limit_hosts:
            cmd.extend(['--limit', ','.join(limit_hosts)])

        env = os.environ.copy()
        env['ANSIBLE_HOST_KEY_CHECKING'] = 'False'
        env['ANSIBLE_NOCOLOR'] = '1'
        env['PYTHONUNBUFFERED'] = '1'
        # Deliberately NOT the json callback that deploy uses — it buffers
        # everything until the run ends, which defeats live progress on a
        # job this long. Per-host detail comes from the result files instead.

        _set(status='running', command=' '.join(shlex.quote(c) for c in cmd))

        try:
            proc = subprocess.Popen(
                cmd, cwd=repo_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            _set(status='failed', returncode=-1,
                 output='ansible-playbook not found. Is ansible-core '
                        'installed in this image?')
            return

        timer = threading.Timer(PLAYBOOK_TIMEOUT_SECS, proc.kill)
        timer.start()
        try:
            for line in proc.stdout:
                _append(line)
            proc.wait()
        finally:
            timer.cancel()

        results = []
        for rf in sorted(glob.glob(os.path.join(results_dir, '*_firmware.json'))):
            try:
                with open(rf) as f:
                    results.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass

        rc = proc.returncode
        _set(
            status='done' if rc == 0 else 'failed',
            returncode=rc,
            results=results,
        )


# ── Routes ─────────────────────────────────────────────────

@login_required
def firmware_images(project_name):
    """List .swi images available to select."""
    app = current_app._get_current_object()
    return jsonify({
        'ok': True,
        'images': list_firmware_images(app),
        'firmware_dir': app.config.get('FIRMWARE_DIR', ''),
    })


@login_required
def firmware_run(project_name):
    """Start a firmware upgrade as a background job."""
    app = current_app._get_current_object()
    username = current_user.username
    body = request.json or {}

    image      = (body.get('image') or '').strip()
    partition  = (body.get('partition') or 'secondary').strip()
    hosts_sel  = body.get('hosts') or []
    sw_user    = (body.get('switch_username') or '').strip()
    sw_pass    = body.get('switch_password') or ''
    allow      = bool(body.get('allow_unsafe', False))
    window     = body.get('unsafe_window_mins', 30)

    if not sw_user or not sw_pass:
        return jsonify(ok=False, error='Switch credentials are required.'), 400
    if partition not in VALID_PARTITIONS:
        return jsonify(ok=False, error='Partition must be primary or secondary.'), 400

    image_path = resolve_image_path(app, image)
    if not image_path:
        return jsonify(ok=False, error='Unknown firmware image.'), 400

    try:
        window = int(window)
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Window must be a whole number of minutes.'), 400
    if allow and not (1 <= window <= 120):
        return jsonify(ok=False, error='Window must be between 1 and 120 minutes.'), 400

    mapping = _load_mapping(app, username, project_name)
    all_hosts = mapping.get('hosts', [])
    if hosts_sel:
        chosen = [h for h in all_hosts if h['hostname'] in hosts_sel]
    else:
        chosen = all_hosts
    chosen = [h for h in chosen if h.get('mgmt_ip')]
    if not chosen:
        return jsonify(
            ok=False,
            error='No target hosts with a management IP. Set them on the '
                  'Deploy tab first.'), 400

    repo_dir = app.config.get('CONFIGGEN_REPO', 'configgen')
    playbook_path = os.path.join(repo_dir, 'playbooks', 'firmware_upgrade.yml')
    if not os.path.isfile(playbook_path):
        return jsonify(
            ok=False,
            error='firmware_upgrade.yml not found in the config repo. Pull '
                  'the latest NetForge from the Admin panel.'), 400

    inventory = _build_inventory(chosen, sw_user, sw_pass)
    extra_vars = {
        'deploy_username':    sw_user,
        'deploy_password':    sw_pass,
        'firmware_file':      image_path,
        'firmware_partition': partition,
        'allow_unsafe':       allow,
    }
    if allow:
        extra_vars['unsafe_window_mins'] = window

    job_id = str(uuid.uuid4())
    with _fw_jobs_lock:
        _fw_jobs[job_id] = {
            'status': 'starting',
            'output': '',
            'returncode': None,
            'results': [],
            'hosts': [h['hostname'] for h in chosen],
            'image': image,
            'partition': partition,
            'allow_unsafe': allow,
        }

    ansible_bin = app.config.get('ANSIBLE_BIN', 'ansible-playbook')

    def _worker():
        try:
            _run_firmware_playbook(
                job_id, playbook_path, repo_dir, ansible_bin,
                inventory, extra_vars,
                limit_hosts=[h['hostname'] for h in chosen],
            )
        except Exception as ex:                     # noqa: BLE001
            with _fw_jobs_lock:
                _fw_jobs[job_id]['status'] = 'failed'
                _fw_jobs[job_id]['returncode'] = -1
                _fw_jobs[job_id]['output'] += '\n%s\n' % ex

    threading.Thread(target=_worker, daemon=True).start()

    return jsonify({'ok': True, 'job_id': job_id})


@login_required
def firmware_status(project_name, job_id):
    """Poll a running or finished firmware job."""
    with _fw_jobs_lock:
        job = _fw_jobs.get(job_id)
        snapshot = dict(job) if job else None
    if not snapshot:
        abort(404)
    return jsonify(snapshot)
