import os
import uuid
import threading
import subprocess
from flask import (Blueprint, render_template, request, jsonify,
                   current_app, send_file, abort)
from flask_login import login_required, current_user
from .utils import (read_hosts, list_generated_configs,
                    delete_generated_config, generated_configs_dir,
                    hosts_ini_path)

generate_bp = Blueprint('generate', __name__)

# In-memory job store: {job_id: {status, output, returncode}}
_jobs = {}
_jobs_lock = threading.Lock()


def _run_job(job_id, cmd, cwd, env):
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True
        )
        with _jobs_lock:
            _jobs[job_id]['output']     = proc.stdout
            _jobs[job_id]['returncode'] = proc.returncode
            _jobs[job_id]['status']     = 'done' if proc.returncode == 0 else 'failed'
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]['output']     = str(e)
            _jobs[job_id]['returncode'] = -1
            _jobs[job_id]['status']     = 'failed'


@generate_bp.route('/generate')
@login_required
def generate_page():
    app     = current_app._get_current_object()
    hosts   = read_hosts(app, current_user.username)
    configs = list_generated_configs(app, current_user.username)
    return render_template('generate.html', hosts=hosts, configs=configs)


@generate_bp.route('/generate/run', methods=['POST'])
@login_required
def run():
    app      = current_app._get_current_object()
    limit    = request.json.get('limit', '').strip()   # hostname or empty for all
    tags     = request.json.get('tags', '').strip()

    ini_path = hosts_ini_path(app, current_user.username)
    out_dir  = generated_configs_dir(app, current_user.username)
    os.makedirs(out_dir, exist_ok=True)

    cmd = [
        app.config['ANSIBLE_BIN'],
        '-i', ini_path,
        app.config['PLAYBOOK'],
        '-e', f'config_output_dir={out_dir}',
    ]
    if limit:
        cmd += ['--limit', limit]
    if tags:
        cmd += ['--tags', tags]

    # Ansible needs to find roles/ in the shared config repo
    env = os.environ.copy()
    env['ANSIBLE_ROLES_PATH'] = app.config['ROLES_PATH']

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {'status': 'running', 'output': '', 'returncode': None}

    t = threading.Thread(
        target=_run_job,
        args=(job_id, cmd, app.config['CONFIGGEN_REPO'], env),
        daemon=True
    )
    t.start()

    return jsonify({'job_id': job_id})


@generate_bp.route('/generate/status/<job_id>')
@login_required
def status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@generate_bp.route('/generate/download_all')
@login_required
def download_all():
    import zipfile, io
    app   = current_app._get_current_object()
    gcdir = generated_configs_dir(app, current_user.username)
    files = list_generated_configs(app, current_user.username)
    if not files:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            zf.write(os.path.join(gcdir, fname), fname)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='configs.zip', mimetype='application/zip')


@generate_bp.route('/generate/download/<filename>')
@login_required
def download(filename):
    app    = current_app._get_current_object()
    gcdir  = generated_configs_dir(app, current_user.username)
    fpath  = os.path.join(gcdir, filename)
    if not os.path.isfile(fpath):
        abort(404)
    return send_file(fpath, as_attachment=True, download_name=filename)


@generate_bp.route('/generate/delete/<filename>', methods=['POST'])
@login_required
def delete_config(filename):
    app = current_app._get_current_object()
    delete_generated_config(app, current_user.username, filename)
    return jsonify({'ok': True})
