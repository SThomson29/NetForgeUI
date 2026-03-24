import os
import io
import zipfile
import uuid
import threading

from flask import (Blueprint, render_template, redirect, url_for,
                   request, flash, current_app, jsonify, send_file, abort)
from flask_login import login_required, current_user

from .project import (
    sync_allocations as _sync_allocations,
    list_projects, create_project, delete_project,
    get_project_config, save_project_config,
    add_pool, remove_pool,
    get_available_ips, allocate_unique, release_unique,
    get_available_ptp_pairs, allocate_ptp, release_ptp,
    get_carved_subnets, assign_vlan_subnet, release_vlan_subnet,
    get_common, save_common,
    get_conventions, save_conventions,
    get_all_allocations,
    project_generated_configs_dir,
)
from .utils import (
    read_project_hosts, add_project_host, remove_project_host,
    list_project_generated_configs,
    project_hosts_ini_path, project_host_vars_dir,
    HOSTVARS_FILES,
)

projects_bp = Blueprint('projects', __name__)

# In-memory job store shared with generate
_jobs = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Projects landing page
# ---------------------------------------------------------------------------

@projects_bp.route('/')
@projects_bp.route('/projects')
@login_required
def projects_page():
    app = current_app._get_current_object()
    projects = list_projects(app, current_user.username)
    return render_template('projects.html', projects=projects)


@projects_bp.route('/projects/new', methods=['POST'])
@login_required
def new_project():
    app  = current_app._get_current_object()
    name = request.form.get('name', '').strip()
    if not name:
        flash('Project name is required.', 'error')
        return redirect(url_for('projects.projects_page'))
    try:
        create_project(app, current_user.username, name)
        flash(f'Project {name} created.', 'success')
    except ValueError as e:
        flash(str(e), 'error')
    return redirect(url_for('projects.projects_page'))


@projects_bp.route('/projects/<project_name>/delete', methods=['POST'])
@login_required
def delete(project_name):
    app = current_app._get_current_object()
    delete_project(app, current_user.username, project_name)
    flash(f'Project {project_name} deleted.', 'success')
    return redirect(url_for('projects.projects_page'))


@projects_bp.route('/projects/<project_name>/download_all')
@login_required
def download_all(project_name):
    app   = current_app._get_current_object()
    gcdir = project_generated_configs_dir(app, current_user.username, project_name)
    files = list_project_generated_configs(app, current_user.username, project_name)
    if not files:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            zf.write(os.path.join(gcdir, fname), fname)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f'{project_name}_configs.zip',
                     mimetype='application/zip')


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/hosts')
@login_required
def hosts_page(project_name):
    app   = current_app._get_current_object()
    hosts = read_project_hosts(app, current_user.username, project_name)
    return render_template('project_hosts.html',
                           project_name=project_name, hosts=hosts)


@projects_bp.route('/projects/<project_name>/hosts/add', methods=['POST'])
@login_required
def add_host(project_name):
    import re
    app      = current_app._get_current_object()
    hostname = request.form.get('hostname', '').strip()
    stacking = request.form.get('stacking', 'none')

    if not hostname:
        flash('Hostname is required.', 'error')
        return redirect(url_for('projects.hosts_page', project_name=project_name))

    if not re.match(r'^[A-Za-z0-9_\-]+$', hostname):
        flash('Hostname may only contain letters, numbers, hyphens and underscores.', 'error')
        return redirect(url_for('projects.hosts_page', project_name=project_name))

    existing = [h['hostname'].lower() for h in
                read_project_hosts(app, current_user.username, project_name)]
    if hostname.lower() in existing:
        flash(f'{hostname} already exists.', 'error')
        return redirect(url_for('projects.hosts_page', project_name=project_name))

    try:
        add_project_host(app, current_user.username, project_name, hostname, stacking)
        flash(f'{hostname} added.', 'success')
    except RuntimeError as e:
        flash(str(e), 'error')
    return redirect(url_for('projects.hosts_page', project_name=project_name))


@projects_bp.route('/projects/<project_name>/hosts/delete/<hostname>', methods=['POST'])
@login_required
def delete_host(project_name, hostname):
    app = current_app._get_current_object()
    remove_project_host(app, current_user.username, project_name, hostname)
    flash(f'{hostname} removed.', 'success')
    return redirect(url_for('projects.hosts_page', project_name=project_name))


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/editor')
@login_required
def editor(project_name):
    app    = current_app._get_current_object()
    hosts  = read_project_hosts(app, current_user.username, project_name)
    cfg    = get_project_config(app, current_user.username, project_name)
    allocs = get_all_allocations(app, current_user.username, project_name)
    selected = request.args.get('host', '')
    return render_template('project_editor.html',
                           project_name=project_name,
                           hosts=hosts,
                           selected=selected,
                           project_config=cfg,
                           allocations=allocs)


@projects_bp.route('/projects/<project_name>/api/hostvars/<hostname>/state')
@login_required
def get_state(project_name, hostname):
    from .hostvars import _parse_state
    app   = current_app._get_current_object()
    hvdir = os.path.join(
        project_host_vars_dir(app, current_user.username, project_name),
        hostname
    )
    if not os.path.isdir(hvdir):
        return jsonify({'error': 'Host not found'}), 404
    return jsonify(_parse_state(hvdir))


@projects_bp.route('/projects/<project_name>/api/hostvars/<hostname>/save_all',
                   methods=['POST'])
@login_required
def save_all_hostvars(project_name, hostname):
    app   = current_app._get_current_object()
    files = request.json or []
    hvdir = project_host_vars_dir(app, current_user.username, project_name)
    errors = []
    for item in files:
        try:
            fname    = item['filename']
            fcontent = item['content']
            if fname not in HOSTVARS_FILES:
                raise ValueError(f'Unknown file: {fname}')
            dest = os.path.join(hvdir, hostname)
            os.makedirs(dest, exist_ok=True)
            with open(os.path.join(dest, fname), 'w') as f:
                f.write(fcontent)
        except (ValueError, KeyError) as e:
            errors.append(str(e))
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 400

    # Sync IP allocations from saved host_vars
    try:
        _sync_allocations(app, current_user.username, project_name, hostname)
    except Exception as e:
        pass  # Don't fail the save if allocation sync errors

    return jsonify({'ok': True, 'saved': len(files)})


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/generate')
@login_required
def generate_page(project_name):
    app     = current_app._get_current_object()
    hosts   = read_project_hosts(app, current_user.username, project_name)
    configs = list_project_generated_configs(app, current_user.username, project_name)
    return render_template('project_generate.html',
                           project_name=project_name,
                           hosts=hosts,
                           configs=configs)


@projects_bp.route('/projects/<project_name>/generate/run', methods=['POST'])
@login_required
def run_generate(project_name):
    from .docker_runner import run_generate as docker_run_generate

    app    = current_app._get_current_object()
    limit  = request.json.get('limit', '').strip()
    tags   = request.json.get('tags', '').strip()

    ini_path     = project_hosts_ini_path(app, current_user.username, project_name)
    host_vars    = project_host_vars_dir(app, current_user.username, project_name)
    out_dir      = project_generated_configs_dir(app, current_user.username, project_name)
    os.makedirs(out_dir, exist_ok=True)

    configgen_repo = app.config['CONFIGGEN_REPO']

    # Derive playbook and roles paths relative to the repo root
    playbook_rel = os.path.relpath(app.config['PLAYBOOK'], configgen_repo)
    roles_rel    = os.path.relpath(app.config['ROLES_PATH'], configgen_repo)

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'status': 'running', 'output': '', 'returncode': None}

    def _run(jid):
        try:
            rc, output = docker_run_generate(
                job_id       = jid,
                configgen_repo = configgen_repo,
                hosts_ini_path = ini_path,
                host_vars_dir  = host_vars,
                output_dir     = out_dir,
                playbook_rel   = playbook_rel,
                roles_rel      = roles_rel,
                limit          = limit or None,
                tags           = tags or None,
            )
            with _jobs_lock:
                _jobs[jid]['output']     = output
                _jobs[jid]['returncode'] = rc
                _jobs[jid]['status']     = 'done' if rc == 0 else 'failed'
        except RuntimeError as ex:
            # Hard failure — Docker socket unavailable or similar
            with _jobs_lock:
                _jobs[jid]['output']     = str(ex)
                _jobs[jid]['returncode'] = -1
                _jobs[jid]['status']     = 'failed'
        except Exception as ex:
            with _jobs_lock:
                _jobs[jid]['output']     = str(ex)
                _jobs[jid]['returncode'] = -1
                _jobs[jid]['status']     = 'failed'

    threading.Thread(target=_run, args=(job_id,), daemon=True).start()

    return jsonify({'job_id': job_id})


@projects_bp.route('/projects/<project_name>/generate/status/<job_id>')
@login_required
def generate_status(project_name, job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@projects_bp.route('/projects/<project_name>/generate/download/<filename>')
@login_required
def download_config(project_name, filename):
    app   = current_app._get_current_object()
    gcdir = project_generated_configs_dir(app, current_user.username, project_name)
    fpath = os.path.join(gcdir, filename)
    if not os.path.isfile(fpath):
        abort(404)
    return send_file(fpath, as_attachment=True, download_name=filename)


@projects_bp.route('/projects/<project_name>/generate/delete/<filename>',
                   methods=['POST'])
@login_required
def delete_config(project_name, filename):
    app   = current_app._get_current_object()
    gcdir = project_generated_configs_dir(app, current_user.username, project_name)
    fpath = os.path.join(gcdir, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Resources — Pools
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/resources')
@login_required
def resources_page(project_name):
    app  = current_app._get_current_object()
    cfg  = get_project_config(app, current_user.username, project_name)
    allocs = get_all_allocations(app, current_user.username, project_name)
    hosts  = read_project_hosts(app, current_user.username, project_name)
    return render_template('project_resources.html',
                           project_name=project_name,
                           config=cfg,
                           allocations=allocs,
                           hosts=hosts)


@projects_bp.route('/projects/<project_name>/api/pools', methods=['POST'])
@login_required
def api_add_pool(project_name):
    app  = current_app._get_current_object()
    data = request.json
    import uuid as _uuid
    data['id'] = 'pool_' + _uuid.uuid4().hex[:8]
    try:
        add_pool(app, current_user.username, project_name, data)
        return jsonify({'ok': True, 'id': data['id']})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@projects_bp.route('/projects/<project_name>/api/pools/<pool_id>', methods=['DELETE'])
@login_required
def api_remove_pool(project_name, pool_id):
    app = current_app._get_current_object()
    remove_pool(app, current_user.username, project_name, pool_id)
    return jsonify({'ok': True})


@projects_bp.route('/projects/<project_name>/api/pools/<pool_id>/available')
@login_required
def api_available_ips(project_name, pool_id):
    app  = current_app._get_current_object()
    cfg  = get_project_config(app, current_user.username, project_name)
    pool = next((p for p in cfg.get('pools', []) if p['id'] == pool_id), None)
    if not pool:
        return jsonify([])
    if pool['type'] == 'unique':
        return jsonify(get_available_ips(app, current_user.username, project_name, pool_id))
    if pool['type'] == 'point_to_point':
        return jsonify(get_available_ptp_pairs(app, current_user.username, project_name, pool_id))
    if pool['type'] == 'vlan_supernet':
        return jsonify(get_carved_subnets(app, current_user.username, project_name, pool_id))
    return jsonify([])


@projects_bp.route('/projects/<project_name>/api/allocations/unique', methods=['POST'])
@login_required
def api_allocate_unique(project_name):
    app  = current_app._get_current_object()
    data = request.json
    allocate_unique(app, current_user.username, project_name,
                    data['pool_id'], data['ip'], data['hostname'], data['interface'])
    return jsonify({'ok': True})


@projects_bp.route('/projects/<project_name>/api/allocations/unique/release', methods=['POST'])
@login_required
def api_release_unique(project_name):
    app  = current_app._get_current_object()
    data = request.json
    release_unique(app, current_user.username, project_name,
                   data['pool_id'], data['ip'])
    return jsonify({'ok': True})


@projects_bp.route('/projects/<project_name>/api/allocations/ptp', methods=['POST'])
@login_required
def api_allocate_ptp(project_name):
    app  = current_app._get_current_object()
    data = request.json
    peer_ip = allocate_ptp(app, current_user.username, project_name,
                           data['pool_id'], data['ip'],
                           data['hostname'], data['interface'],
                           data.get('peer_note'))
    return jsonify({'ok': True, 'peer_ip': peer_ip})


@projects_bp.route('/projects/<project_name>/api/allocations/ptp/release', methods=['POST'])
@login_required
def api_release_ptp(project_name):
    app  = current_app._get_current_object()
    data = request.json
    release_ptp(app, current_user.username, project_name,
                data['pool_id'], data['ip'])
    return jsonify({'ok': True})


@projects_bp.route('/projects/<project_name>/api/allocations/vlan', methods=['POST'])
@login_required
def api_assign_vlan(project_name):
    app  = current_app._get_current_object()
    data = request.json
    try:
        assign_vlan_subnet(app, current_user.username, project_name,
                           data['pool_id'], data['subnet'],
                           data['vlan_id'], data['vlan_name'],
                           data['hostname'], data.get('peer_hostname'))
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@projects_bp.route('/projects/<project_name>/api/allocations/vlan/release', methods=['POST'])
@login_required
def api_release_vlan(project_name):
    app  = current_app._get_current_object()
    data = request.json
    release_vlan_subnet(app, current_user.username, project_name,
                        data['pool_id'], data['subnet'])
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Resources — Common infrastructure
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/api/common', methods=['GET'])
@login_required
def api_get_common(project_name):
    app = current_app._get_current_object()
    return jsonify(get_common(app, current_user.username, project_name))


@projects_bp.route('/projects/<project_name>/api/common', methods=['POST'])
@login_required
def api_save_common(project_name):
    app  = current_app._get_current_object()
    data = request.json
    save_common(app, current_user.username, project_name, data)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Resources — Conventions
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/api/conventions', methods=['GET'])
@login_required
def api_get_conventions(project_name):
    app = current_app._get_current_object()
    return jsonify(get_conventions(app, current_user.username, project_name))


@projects_bp.route('/projects/<project_name>/api/conventions', methods=['POST'])
@login_required
def api_save_conventions(project_name):
    app  = current_app._get_current_object()
    data = request.json
    save_conventions(app, current_user.username, project_name, data)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Resources — Full allocations (read-only view)
# ---------------------------------------------------------------------------

@projects_bp.route('/projects/<project_name>/api/allocations')
@login_required
def api_get_allocations(project_name):
    app = current_app._get_current_object()
    return jsonify(get_all_allocations(app, current_user.username, project_name))
