"""
Audit log for actions that touch live switches: deploy dry runs, pushes and
firmware upgrades.

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
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from .models import get_db


ACTIONS = {
    'deploy_dryrun': 'Dry run',
    'deploy_push':   'Push',
    'firmware':      'Firmware',
}

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


def record_start(app, *, username, project, action, switch_user, targets, params):
    """Open an entry for a run that is starting. Returns its id."""
    with get_db(app) as db:
        cur = db.execute(
            'INSERT INTO audit_log (started_at, username, project, action, '
            'switch_user, targets, params, status) VALUES (?,?,?,?,?,?,?,?)',
            (_now(), username, project, action, switch_user,
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


def entries(app, *, username=None, project=None, limit=200):
    """Newest first, optionally narrowed to one user and/or project."""
    where, args = [], []
    if username is not None:
        where.append('username = ?')
        args.append(username)
    if project is not None:
        where.append('project = ?')
        args.append(project)
    sql = 'SELECT * FROM audit_log'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(int(limit))
    with get_db(app) as db:
        return [_row(r) for r in db.execute(sql, args).fetchall()]
