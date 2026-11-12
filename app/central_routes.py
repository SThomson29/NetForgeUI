"""
Central project routes — extends the projects blueprint.

    Overview    what the project is, the collection in use, the design so far
    Design      forms drawn from the collection's schema, one section at a time
    Apply       unlock the tenant, plan (check mode with diffs), then apply

Design saves need edit access. Anything that uses the tenant's API client
— unlocking, planning, applying — needs deploy access, the same level that
guards a config project's switches.

A run is one ansible-playbook of the collection's tenant build, with the
design passed as data (every string tagged !unsafe, so nothing typed into
a form is ever templated) and the API client passed in the environment, so
it is in neither the command line nor a file.
"""

import os
import sys
import tempfile
import threading
import uuid

from flask import abort, current_app, g, jsonify, render_template
from flask_login import current_user, login_required

from . import audit, central_design, central_schema, central_session, project_types, repo_sync, repo_version
from .api import bad_request, json_body
from .central_schema import CentralError
from .jobs import mark_finished, prune_finished, run_ansible_job
from .security import job_tag, visible_job

_central_jobs = {}
_central_jobs_lock = threading.Lock()

RUN_TIMEOUT_SECS = 30 * 60


def _key(project_name):
    return '%s/%s' % (g.project_owner, project_name)


def _schema_or_400(app):
    try:
        return central_schema.load(app)
    except CentralError as e:
        bad_request(str(e))


def _stages(schema, counts=None):
    out = []
    for stage, label in central_schema.STAGES:
        sections = [dict(s, rows=(counts or {}).get(s['id'], 0))
                    for s in schema['sections'] if s['stage'] == stage]
        if sections:
            out.append({'id': stage, 'label': label, 'sections': sections})
    return out


# ── Pages ──────────────────────────────────────────────────

@login_required
def central_overview(project_name):
    """Where a central project opens: what it is, the collection that
    applies it, and how much of the design is filled in."""
    app = current_app._get_current_object()
    from .project import get_project_config
    problem, stages = None, []
    try:
        schema = central_schema.load(app)
        stages = _stages(schema, central_design.counts(app, g.project_owner, project_name, schema))
    except CentralError as e:
        problem = str(e)
    return render_template('central_overview.html', project_name=project_name,
                           created=get_project_config(app, g.project_owner, project_name).get('created', ''),
                           collection=project_types.collection_state(app),
                           stages=stages, problem=problem)


@login_required
def central_design_page(project_name):
    app = current_app._get_current_object()
    problem = None
    try:
        central_schema.load(app)
    except CentralError as e:
        problem = str(e)
    from . import sharing
    return render_template('central_design.html', project_name=project_name, problem=problem,
                           can_edit=sharing.at_least(g.project_level, sharing.EDIT))


@login_required
def central_apply_page(project_name):
    app = current_app._get_current_object()
    problem, stages = None, []
    try:
        schema = central_schema.load(app)
        stages = _stages(schema, central_design.counts(app, g.project_owner, project_name, schema))
    except CentralError as e:
        problem = str(e)
    from . import sharing
    return render_template('central_apply.html', project_name=project_name, problem=problem, stages=stages,
                           can_run=sharing.at_least(g.project_level, sharing.DEPLOY),
                           tenant=central_session.status(current_user.username, g.project_owner, project_name),
                           collection=project_types.collection_state(app))


# ── Design API ─────────────────────────────────────────────

@login_required
def central_api_schema(project_name):
    """The schema and the profile catalogue, for the forms."""
    app = current_app._get_current_object()
    schema = _schema_or_400(app)
    try:
        types = [{k: p.get(k) for k in ('type', 'label', 'category', 'named', 'fields')}
                 for p in central_schema.catalogue(app)]
    except CentralError:
        types = []
    return jsonify(ok=True, schema=schema, stages=central_schema.STAGES, profile_types=types,
                   counts=central_design.counts(app, g.project_owner, project_name, schema),
                   saved=central_design.saved_info(app, g.project_owner, project_name),
                   mask=central_schema.MASK)


@login_required
def central_api_profile_type(project_name, ptype):
    app = current_app._get_current_object()
    try:
        described = central_schema.profile_type(app, ptype)
    except CentralError as e:
        bad_request(str(e))
    return jsonify(ok=True, profile=described)


def _refs(schema, design):
    """{var: [row keys]} for the pickers that point at other sections."""
    out = {}
    for sect in schema['sections']:
        for part in sect['lists']:
            if part['key']:
                out[part['var']] = [str(r[part['key']]) for r in design.get(part['var']) or []
                                    if r.get(part['key']) not in (None, '')]
    return out


@login_required
def central_api_design_get(project_name, section):
    app = current_app._get_current_object()
    schema = _schema_or_400(app)
    try:
        sect = central_schema.section(app, section)
    except CentralError:
        abort(404)
    data, mark = central_design.read_section(app, g.project_owner, project_name, sect)
    masked = central_schema.mask(app, sect, data)
    design = central_design.read_design(app, g.project_owner, project_name, schema)
    return jsonify(ok=True, data=masked, base=mark,
                   yaml=central_design.dump(masked) if any(masked.values()) else '',
                   refs=_refs(schema, design))


@login_required
def central_api_design_save(project_name, section):
    """Save a section. POST {data: {var: [rows]}, base: fingerprint}."""
    app = current_app._get_current_object()
    schema = _schema_or_400(app)
    try:
        sect = central_schema.section(app, section)
    except CentralError:
        abort(404)
    body = json_body('data')
    owner = g.project_owner
    saved, _mark = central_design.read_section(app, owner, project_name, sect)
    data = central_schema.unmask(sect, body['data'], saved)
    try:
        clean, problems = central_schema.check(
            app, sect, data, central_design.read_design(app, owner, project_name, schema))
    except CentralError as e:
        bad_request(str(e))
    if problems:
        return jsonify(ok=False, error='The section was not saved.', problems=problems[:50]), 400
    try:
        mark = central_design.save_section(app, owner, project_name, sect, clean,
                                           str(body.get('base') or ''), current_user.username)
    except central_design.DesignConflict:
        return jsonify(ok=False, error='This section was saved by someone else since you opened it. '
                                       'Reload to see their changes; yours have not been saved.'), 409
    masked = central_schema.mask(app, sect, clean)
    return jsonify(ok=True, base=mark, data=masked,
                   yaml=central_design.dump(masked) if any(masked.values()) else '')


# ── The tenant's API client ────────────────────────────────

@login_required
def central_unlock(project_name):
    """Check the API client and keep it for this session. POST {base_url,
    client_id, client_secret}."""
    body = json_body('base_url', 'client_id', 'client_secret')
    try:
        status = central_session.unlock(current_user.username, g.project_owner, project_name,
                                        body['base_url'], body['client_id'], body['client_secret'])
    except central_session.UnlockError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, tenant=status)


@login_required
def central_lock(project_name):
    central_session.lock(current_user.username, g.project_owner, project_name)
    return jsonify(ok=True, tenant={'unlocked': False})


# ── Plan and apply ─────────────────────────────────────────

def _busy(project_key):
    with _central_jobs_lock:
        for job in _central_jobs.values():
            if job.get('_project') == project_key and job.get('_finished_at') is None:
                return job.get('_owner')
    return None


def _slim(report):
    """A section's report without its diffs, for the audit log."""
    if not isinstance(report, dict):
        return report
    return dict(report, entries=[{k: v for k, v in e.items() if k != 'diff'}
                                 for e in report.get('entries') or [] if isinstance(e, dict)])


@login_required
def central_run(project_name):
    """Plan or apply. POST {mode: 'plan'|'apply', sections: [ids]}; no
    sections means the whole design. A plan is the same run in check mode
    with diffs, and changes nothing in the tenant."""
    app = current_app._get_current_object()
    body = json_body('mode')
    mode = body['mode']
    if mode not in ('plan', 'apply'):
        bad_request('mode must be plan or apply.')
    schema = _schema_or_400(app)
    owner, user = g.project_owner, current_user.username
    wanted = body.get('sections') or []
    known = [s['id'] for s in schema['sections']]
    if not isinstance(wanted, list) or any(s not in known for s in wanted):
        bad_request('Unknown section.')
    chosen = [s for s in schema['sections'] if not wanted or s['id'] in wanted]

    creds = central_session.get(user, owner, project_name)
    if creds is None:
        return jsonify(ok=False, error='Unlock the tenant first: enter its API client.', locked=True), 409
    running = _busy(_key(project_name))
    if running:
        return jsonify(ok=False, error='%s already has a run in progress on this project.' % running), 409

    design = central_design.read_design(app, owner, project_name, schema)
    problems = []
    for sect in chosen:
        try:
            data = {part['var']: design.get(part['var']) or [] for part in sect['lists']}
            problems += central_schema.check(app, sect, data, design)[1]
        except CentralError as e:
            bad_request(str(e))
    if problems:
        return jsonify(ok=False, error='The design has problems. Fix them on the Design page first.',
                       problems=problems[:50]), 400
    # Only the chosen sections' rows are sent, so nothing else is touched.
    sending = {part['var']: design.get(part['var']) or [] for sect in chosen for part in sect['lists']}
    if not any(sending.values()):
        return jsonify(ok=False, error='There is nothing in the design to %s yet.' % mode), 400

    build = central_schema.build_dir(app)
    playbook = chosen[0]['playbook'] if len(chosen) == 1 else 'build.yml'
    version = repo_version.read(app.config.get('CENTRAL_REPO') or '')
    audit_id = audit.record_start(
        app, username=user, owner=owner, project=project_name,
        action='central_plan' if mode == 'plan' else 'central_apply',
        switch_user=central_session.status(user, owner, project_name).get('client_id'),
        targets=[{'hostname': s['label'], 'mgmt_ip': ''} for s in chosen],
        params={'tenant': creds['base_url'], 'collection': version['label'] if version else None})

    prune_finished(_central_jobs, _central_jobs_lock)
    job_id = str(uuid.uuid4())
    with _central_jobs_lock:
        _central_jobs[job_id] = {
            'status': 'starting', 'output': '', 'returncode': None, 'results': [],
            'mode': mode, 'sections': [s['id'] for s in chosen],
            **job_tag(user, _key(project_name)),
        }
    collections = os.pathsep.join(p for p in (
        app.config.get('CENTRAL_COLLECTIONS_PATH'), os.environ.get('ANSIBLE_COLLECTIONS_PATH')) if p)
    ansible_bin = app.config.get('ANSIBLE_BIN', 'ansible-playbook')
    inventory = {'all': {'hosts': {'localhost': {
        'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable}}}}

    def _worker():
        try:
            # The collection caches its API token in the temp folder; a
            # folder of its own means the token goes when the run does.
            with tempfile.TemporaryDirectory(prefix='central-run-') as scratch:
                run_ansible_job(
                    _central_jobs, _central_jobs_lock, job_id,
                    playbook_path=os.path.join(build, playbook), repo_dir=build,
                    ansible_bin=ansible_bin, inventory=inventory,
                    extra_vars={'design': sending},
                    extra_args=['--check', '--diff'] if mode == 'plan' else [],
                    timeout_secs=RUN_TIMEOUT_SECS,
                    extra_env={'API_CLIENT_ID': creds['client_id'],
                               'API_CLIENT_SECRET': creds['client_secret'],
                               'CENTRAL_BASE_URL': creds['base_url'],
                               'TMPDIR': scratch,
                               'ANSIBLE_COLLECTIONS_PATH': collections,
                               'ANSIBLE_LOCALHOST_WARNING': 'False',
                               'ANSIBLE_INVENTORY_UNPARSED_WARNING': 'False'})
        except Exception as ex:                     # noqa: BLE001
            with _central_jobs_lock:
                _central_jobs[job_id]['status'] = 'failed'
                _central_jobs[job_id]['returncode'] = -1
                _central_jobs[job_id]['output'] += '\n%s\n' % ex
        finally:
            with _central_jobs_lock:
                job = _central_jobs[job_id]
                # The playbooks keep the secret out of their output; this
                # is a second line of defence for anything unexpected.
                job['output'] = (job.get('output') or '').replace(creds['client_secret'], '********')
                order = {s['id']: i for i, s in enumerate(schema['sections'])}
                job['results'] = sorted((r for r in job.get('results') or [] if isinstance(r, dict)),
                                        key=lambda r: order.get(r.get('section'), 999))
                mark_finished(job)
                final = dict(job, results=[_slim(r) for r in job['results']])
            try:
                audit.record_finish(app, audit_id, final)
            except Exception:                       # noqa: BLE001
                pass    # never let the log take down the job's own result

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify(ok=True, job_id=job_id)


@login_required
def central_run_status(project_name, job_id):
    with _central_jobs_lock:
        snapshot = visible_job(_central_jobs.get(job_id), current_user.username, _key(project_name))
    if not snapshot:
        abort(404)
    return jsonify(snapshot)
