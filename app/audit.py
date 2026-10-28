"""
Audit log: config generation, and the actions that touch live switches —
deploy dry runs, pushes and firmware upgrades.

Job state lives in memory and is dropped an hour after a run ends, so
without this nothing recorded who pushed what to which switch, or how it
went. Entries are kept in users.db rather than the project, so they survive
the project being deleted and an admin can see every user's.

An entry is written when a run starts (status 'running') and completed when
it ends. One still 'running' at startup was cut off by a restart and is
marked 'interrupted'.

Commands and log output can carry secrets from the generated config —
RADIUS and TACACS keys, SNMP communities, local passwords — so those values
are masked before anything is stored. Switch passwords never reach here:
they are not in the results or the command line.

Generations record a SHA-256 for each FULL/PARTIAL config they leave
behind, and deploys record the SHA-256 of the file they send, so a push can
be traced to the generation that first produced its config.

After AUDIT_DETAIL_DAYS the bulky detail — log tail and pushed commands —
is dropped. Who, what, when, which switches, file hashes and the outcome
are kept for good.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from .models import get_db


ACTIONS = {
    'generate':      'Generate',
    'deploy_dryrun': 'Dry run',
    'deploy_push':   'Push',
    'firmware':      'Firmware',
}

DEPLOY_ACTIONS = ('deploy_dryrun', 'deploy_push')

# 0 keeps details forever.
AUDIT_DETAIL_DAYS = int(os.environ.get('AUDIT_DETAIL_DAYS', '90'))

# Enough of the run's log to diagnose a failure; the start is mostly
# Ansible's play banner, the end has the errors and the recap.
OUTPUT_TAIL_CHARS = 20000

MASK = '********'

# AOS-CX: "... key plaintext X", "password ciphertext X", etc.
_AFTER_FORM = re.compile(r'\b(plaintext|ciphertext)\s+\S+', re.I)
# "community X", "key X", "auth-key X" where the form keyword is absent.
_AFTER_KEYWORD = re.compile(
    r'\b(community|key|secret|password|auth-key|authentication-key|'
    r'md5|psk)\s+(?!plaintext\b|ciphertext\b)(\S+)', re.I)


def mask_secrets(text):
    """Mask values that follow secret-bearing keywords in config text."""
    if not text:
        return text
    text = _AFTER_FORM.sub(lambda m: '%s %s' % (m.group(1), MASK), text)
    return _AFTER_KEYWORD.sub(lambda m: '%s %s' % (m.group(1), MASK), text)


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def init_audit(app):
    with get_db(app) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                username    TEXT NOT NULL,
                project     TEXT NOT NULL,
                action      TEXT NOT NULL,
                switch_user TEXT,
                targets     TEXT NOT NULL,
                params      TEXT NOT NULL,
                status      TEXT NOT NULL,
                returncode  INTEGER,
                results     TEXT,
                output_tail TEXT
            )
        ''')
        db.execute('CREATE INDEX IF NOT EXISTS audit_by_project '
                   'ON audit_log (username, project, id)')
        cols = {r['name'] for r in db.execute('PRAGMA table_info(audit_log)')}
        if 'details_trimmed' not in cols:
            db.execute('ALTER TABLE audit_log ADD COLUMN '
                       'details_trimmed INTEGER NOT NULL DEFAULT 0')
        # Whose project an entry belongs to, as distinct from who acted —
        # they differ once projects are shared. Older entries were always
        # the actor's own project.
        if 'owner' not in cols:
            db.execute('ALTER TABLE audit_log ADD COLUMN owner TEXT')
        db.execute('UPDATE audit_log SET owner = username WHERE owner IS NULL')
        db.execute('CREATE INDEX IF NOT EXISTS audit_by_owner_project '
                   'ON audit_log (owner, project, id)')


def file_fingerprint(path):
    """sha256 of a config file, so an entry pins exactly what was sent even
    after the file is regenerated. None if it is missing."""
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(65536), b''):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def deploy_targets(hosts, config_dir):
    """Per-switch target records for a deploy: the file it will receive."""
    targets = []
    for h in hosts:
        path = h.get('config_file') or os.path.join(
            config_dir, '%s_FULL.ios' % h['hostname'])
        targets.append({
            'hostname':    h['hostname'],
            'mgmt_ip':     h.get('mgmt_ip', ''),
            'config_file': os.path.basename(path),
            'sha256':      file_fingerprint(path),
        })
    return targets


_GENERATED_CONFIG = re.compile(r'_(FULL|PARTIAL_.+)\.ios$')


def config_hashes(config_dir):
    """{file name: sha256} for the FULL/PARTIAL configs in a directory.

    The per-section files are left out: they are intermediate, and the
    assembled file is what gets deployed.
    """
    try:
        names = os.listdir(config_dir)
    except OSError:
        return {}
    return {n: file_fingerprint(os.path.join(config_dir, n))
            for n in sorted(names) if _GENERATED_CONFIG.search(n)}


def generation_results(before, after):
    """Per-file results for a generation: hash and whether it changed."""
    results = []
    for name, digest in sorted(after.items()):
        change = ('new' if name not in before
                  else 'changed' if before[name] != digest else 'unchanged')
        results.append({'file': name, 'sha256': digest, 'change': change})
    return results


def record_start(app, *, username, project, action, switch_user, targets, params,
                 owner=None):
    """Open an entry for a run that is starting. Returns its id.

    username is who acted; owner whose project it is (the actor's own if
    not given).
    """
    trim_old(app)
    with get_db(app) as db:
        cur = db.execute(
            'INSERT INTO audit_log (started_at, username, owner, project, action, '
            'switch_user, targets, params, status) VALUES (?,?,?,?,?,?,?,?,?)',
            (_now(), username, owner or username, project, action, switch_user,
             json.dumps(targets), json.dumps(params), 'running'))
        return cur.lastrowid


def _clean_result(r):
    """A per-switch result as stored: commands masked, the diff dropped.

    The diff can be the whole candidate config; the commands already say
    what changed.
    """
    if not isinstance(r, dict):
        return r
    out = {k: v for k, v in r.items() if k != 'diff'}
    if isinstance(out.get('commands'), list):
        out['commands'] = [mask_secrets(str(c)) for c in out['commands']]
    if 'unsafe_command' in out:
        out['unsafe_command'] = mask_secrets(str(out['unsafe_command']))
    return out


def record_finish(app, entry_id, job):
    """Complete an entry from the job's final state."""
    if entry_id is None:
        return
    output = job.get('output') or ''
    results = [_clean_result(r) for r in (job.get('results') or [])]
    status = job.get('status')
    if status not in ('done', 'failed'):
        status = 'failed'
    with get_db(app) as db:
        db.execute(
            'UPDATE audit_log SET finished_at=?, status=?, returncode=?, '
            'results=?, output_tail=? WHERE id=?',
            (_now(), status, job.get('returncode'), json.dumps(results),
             mask_secrets(output[-OUTPUT_TAIL_CHARS:]), entry_id))


def trim_old(app, now=None):
    """Drop the log tail and pushed commands from entries older than
    AUDIT_DETAIL_DAYS, keeping everything else."""
    if AUDIT_DETAIL_DAYS <= 0:
        return
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=AUDIT_DETAIL_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with get_db(app) as db:
        rows = db.execute(
            "SELECT id, results FROM audit_log WHERE details_trimmed = 0 "
            "AND status != 'running' AND started_at < ?", (cutoff,)).fetchall()
        for r in rows:
            results = json.loads(r['results']) if r['results'] else []
            for item in results:
                if isinstance(item, dict):
                    item.pop('commands', None)
            db.execute('UPDATE audit_log SET output_tail = NULL, results = ?, '
                       'details_trimmed = 1 WHERE id = ?',
                       (json.dumps(results), r['id']))


def mark_interrupted(app):
    """Close entries left 'running' by a restart. Called at startup."""
    with get_db(app) as db:
        db.execute("UPDATE audit_log SET status='interrupted', finished_at=? "
                   "WHERE status='running'", (_now(),))


def _row(r):
    e = dict(r)
    for k in ('targets', 'params', 'results'):
        e[k] = json.loads(e[k]) if e.get(k) else ([] if k != 'params' else {})
    e['action_label'] = ACTIONS.get(e['action'], e['action'])
    e['outcome'] = _outcome(e)
    return e


def _outcome(e):
    """A one-line summary and a badge colour for the list view."""
    status, results = e['status'], e['results']
    if status == 'running':
        return ('Running', 'blue')
    if status == 'interrupted':
        return ('Interrupted by restart', 'yellow')
    n = len(e['targets'])
    if e['action'] == 'generate':
        if status != 'done':
            return ('Failed (exit %s)' % e['returncode'], 'red')
        changed = sum(1 for r in results if r.get('change') in ('new', 'changed'))
        return ('%d config%s, %d changed' % (len(results),
                '' if len(results) == 1 else 's', changed), 'green')
    if e['action'] == 'deploy_push' and results:
        ok = sum(1 for r in results if r.get('confirmed') in (True, 'True', 'true'))
        pending = sum(1 for r in results
                      if r.get('rollback_pending') in (True, 'True', 'true'))
        if pending:
            return ('%d of %d confirmed, %d rollback pending' % (ok, n, pending), 'red')
        return ('%d of %d confirmed' % (ok, n), 'green' if ok == n else 'yellow')
    if e['action'] == 'deploy_dryrun' and results:
        changed = sum(1 for r in results if r.get('changed') in (True, 'True', 'true'))
        return ('%d of %d with changes' % (changed, n),
                'neutral' if status == 'done' else 'yellow')
    if e['action'] == 'firmware' and results:
        back = sum(1 for r in results
                   if r.get('reachable_after_boot') in (True, 'True', 'true'))
        return ('%d of %d back online' % (back, n), 'green' if back == n else 'red')
    return ('Succeeded' if status == 'done' else 'Failed (exit %s)' % e['returncode'],
            'green' if status == 'done' else 'red')


def entries(app, *, username=None, project=None, owner=None, limit=200,
            include_generations=True):
    """Newest first, optionally narrowed to who acted (username), whose
    project (owner) and/or the project name.

    Each deploy target whose file hash matches a logged generation gains
    'generated_by': the entry that first produced that exact content.
    """
    where, args = [], []
    if not include_generations:
        where.append("action != 'generate'")
    if username is not None:
        where.append('username = ?')
        args.append(username)
    if owner is not None:
        where.append('owner = ?')
        args.append(owner)
    if project is not None:
        where.append('project = ?')
        args.append(project)
    sql = 'SELECT * FROM audit_log'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(int(limit))
    with get_db(app) as db:
        rows = [_row(r) for r in db.execute(sql, args).fetchall()]
        _link_generations(db, rows)
    return rows


def entry(app, entry_id):
    """One entry by id, as entries() returns them, or None."""
    with get_db(app) as db:
        r = db.execute('SELECT * FROM audit_log WHERE id = ?', (entry_id,)).fetchone()
        if r is None:
            return None
        rows = [_row(r)]
        _link_generations(db, rows)
    return rows[0]


def _truthy(v):
    return v in (True, 'True', 'true')


def last_pushed(app, owner, project):
    """{hostname: {sha256, config_file, at, by}} for the newest push each
    switch confirmed, so the Deploy page can tell which configs have
    changed since. A switch that rolled back or failed doesn't count."""
    with get_db(app) as db:
        rows = db.execute(
            "SELECT targets, results, started_at, username FROM audit_log "
            "WHERE action = 'deploy_push' AND owner = ? AND project = ? "
            "ORDER BY id DESC", (owner, project)).fetchall()
    found = {}
    for r in rows:
        try:
            targets = json.loads(r['targets'] or '[]')
            results = json.loads(r['results'] or '[]')
        except ValueError:
            continue
        confirmed = {str(x.get('hostname')) for x in results
                     if isinstance(x, dict) and _truthy(x.get('confirmed'))}
        for t in targets:
            host = t.get('hostname') if isinstance(t, dict) else None
            if host in confirmed and host not in found and t.get('sha256'):
                found[host] = {'sha256': t['sha256'], 'config_file': t.get('config_file'),
                               'at': r['started_at'], 'by': r['username']}
    return found


def _link_generations(db, rows):
    cache = {}
    for e in rows:
        if e['action'] not in DEPLOY_ACTIONS:
            continue
        for t in e['targets']:
            digest = t.get('sha256')
            if not digest:
                continue
            key = (e['owner'], e['project'], digest)
            if key not in cache:
                # Hashes are hex, so a substring match on the stored JSON is
                # exact enough; the earliest entry first produced the content.
                g = db.execute(
                    "SELECT id, started_at, username FROM audit_log "
                    "WHERE action = 'generate' AND status = 'done' "
                    "AND owner = ? AND project = ? AND results LIKE ? "
                    "ORDER BY id LIMIT 1",
                    (e['owner'], e['project'], '%' + digest + '%')).fetchone()
                cache[key] = dict(g) if g else None
            g = cache[key]
            # Only a generation that ran before this deploy can have made it.
            t['generated_by'] = g if g and g['id'] < e['id'] else None
