"""
Firmware routes — extends the projects blueprint.

Handles:
  - Listing .swi images available on the host
  - Running the firmware upgrade playbook as a background job
  - Polling job status and per-host results

A firmware upgrade is upload (which polls for up to 10 minutes) plus a boot,
reboot and reconnect — realistically 15-20 minutes, far past gunicorn's
120s request timeout. So it runs as a background job (app/jobs.py, shared
with deploy): the request returns a job_id immediately and the UI polls,
with output streamed so progress is visible while it runs.
"""

import os
import uuid
import threading

from .ansible_safe import validate_targets
from . import audit
from .api import json_body
from .jobs import run_ansible_job, prune_finished, mark_finished
from .security import job_tag, visible_job

from flask import render_template, jsonify, current_app, abort, g
from flask_login import login_required, current_user



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
    """Run the playbook, streaming output into the job store."""
    run_ansible_job(
        _fw_jobs, _fw_jobs_lock, job_id,
        playbook_path=playbook_path, repo_dir=repo_dir,
        ansible_bin=ansible_bin, inventory=inventory_dict,
        extra_vars=extra_vars, limit_hosts=limit_hosts,
        timeout_secs=PLAYBOOK_TIMEOUT_SECS,
        result_glob='*_firmware.json',
    )


# ── Routes ─────────────────────────────────────────────────

@login_required
def project_firmware(project_name):
    """Render the firmware tab."""
    app = current_app._get_current_object()
    mapping = _load_mapping(app, g.project_owner, project_name)
    return render_template(
        'project_firmware.html',
        project_name=project_name,
        hosts=mapping.get('hosts', []),
    )


@login_required
def firmware_images(project_name):
    """The .swi images available to select, with their versions."""
    from .firmware_images import catalogue
    app = current_app._get_current_object()
    return jsonify({
        'ok': True,
        'images': [{'name': i['name'], 'version': i['version'], 'platform': i['platform']}
                   for i in catalogue(app)],
        'firmware_dir': app.config.get('FIRMWARE_DIR', ''),
    })


@login_required
def firmware_versions(project_name):
    """What the chosen switches run, against the chosen image.

    POST {hosts: [hostname], switch_username, switch_password, image}. The
    request names switches; their addresses come from the saved mapping.
    """
    from .firmware_images import catalogue
    from .firmware_versions import check
    app = current_app._get_current_object()
    body = json_body()
    user = str(body.get('switch_username') or '').strip()
    password = body.get('switch_password') or ''
    if not user or not password:
        return jsonify(ok=False, error='Enter the switch username and password.'), 400
    wanted = body.get('hosts') or []
    if not isinstance(wanted, list):
        return jsonify(ok=False, error='hosts must be a list of switch names.'), 400
    wanted = {str(h) for h in wanted}
    mapped = [h for h in _load_mapping(app, g.project_owner, project_name).get('hosts', [])
              if isinstance(h, dict) and h.get('hostname') in wanted and h.get('mgmt_ip')]
    problems = validate_targets(mapped)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400
    name = str(body.get('image') or '')
    image = next(({'platform': i['platform'], 'version': i['version']}
                  for i in catalogue(app) if i['name'] == name), None)
    return jsonify(ok=True, image=image,
                   results=check(mapped, user, str(password), image))


@login_required
def firmware_run(project_name):
    """Start a firmware upgrade as a background job."""
    app = current_app._get_current_object()
    username = g.project_owner              # whose project: the files
    actor = current_user.username           # who started the run
    key = '%s/%s' % (username, project_name)
    body = json_body()

    image      = str(body.get('image') or '').strip()
    partition  = str(body.get('partition') or 'secondary').strip()
    hosts_sel  = body.get('hosts') or []
    sw_user    = str(body.get('switch_username') or '').strip()
    sw_pass    = body.get('switch_password') or ''
    allow      = bool(body.get('allow_unsafe', False))
    window     = body.get('unsafe_window_mins', 30)

    if not sw_user or not sw_pass:
        return jsonify(ok=False, error='Switch credentials are required.'), 400
    if not isinstance(hosts_sel, list):
        # A string would be matched by substring: 'core' selecting core-01.
        return jsonify(ok=False, error='hosts must be a list.'), 400
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

    # The mapping may predate validation, so check what is actually used.
    problems = validate_targets(chosen)
    if problems:
        return jsonify(ok=False, error='; '.join(problems)), 400

    from .deploy_routes import switches_in_use
    busy = switches_in_use(key, [h['hostname'] for h in chosen])
    if busy:
        return jsonify(ok=False, error=busy), 409

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

    prune_finished(_fw_jobs, _fw_jobs_lock)
    job_id = str(uuid.uuid4())
    with _fw_jobs_lock:
        _fw_jobs[job_id] = {
            'status': 'starting',
            'output': '',
            'returncode': None,
            'results': [],
            'hosts': [h['hostname'] for h in chosen],
            '_hosts': [h['hostname'] for h in chosen],
            'image': image,
            'partition': partition,
            'allow_unsafe': allow,
            **job_tag(actor, key),
        }

    ansible_bin = app.config.get('ANSIBLE_BIN', 'ansible-playbook')

    audit_id = audit.record_start(
        app, username=actor, owner=username, project=project_name, action='firmware',
        switch_user=sw_user,
        targets=[{'hostname': h['hostname'], 'mgmt_ip': h.get('mgmt_ip', '')}
                 for h in chosen],
        params={'image': image, 'partition': partition,
                'allow_unsafe': allow,
                'unsafe_window_mins': window if allow else None})

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
        finally:
            with _fw_jobs_lock:
                mark_finished(_fw_jobs[job_id])
                final = dict(_fw_jobs[job_id])
            try:
                audit.record_finish(app, audit_id, final)
            except Exception:                       # noqa: BLE001
                pass    # never let the log take down the job's own result

    threading.Thread(target=_worker, daemon=True).start()

    return jsonify({'ok': True, 'job_id': job_id})


@login_required
def firmware_status(project_name, job_id):
    """Poll a running or finished firmware job."""
    with _fw_jobs_lock:
        snapshot = visible_job(_fw_jobs.get(job_id), current_user.username,
                               '%s/%s' % (g.project_owner, project_name))
    if not snapshot:
        abort(404)
    return jsonify(snapshot)
