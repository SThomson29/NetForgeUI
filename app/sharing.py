"""
Shared projects.

A project still lives under its owner (DATA_DIR/<owner>/projects/<name>);
sharing grants other people access to it, at one of three levels:

    view    read everything, download configs
    edit    also save switches, change Resources, generate
    deploy  also dry runs, pushes, firmware and reachability

Only the owner shares, transfers or deletes it. Admins can view any
project. A share names a user or, on an SSO instance, an identity
provider group — membership is read from the groups recorded at the
person's last SSO sign-in, so it follows people joining and leaving.

Shares are kept in users.db, not in the project, so they don't travel
with an export: an imported project belongs to whoever imports it.
"""

import json
import os
import shutil
from datetime import datetime, timezone

from .models import get_db


VIEW, EDIT, DEPLOY, OWNER = 'view', 'edit', 'deploy', 'owner'
LEVELS = [VIEW, EDIT, DEPLOY]
_RANK = {None: 0, VIEW: 1, EDIT: 2, DEPLOY: 3, OWNER: 4}

LEVEL_LABELS = {
    VIEW:   'View',
    EDIT:   'Edit',
    DEPLOY: 'Deploy',
    OWNER:  'Owner',
}


class SharingError(ValueError):
    """A sharing change that can't be made, with a message saying why."""


def init_sharing(app):
    with get_db(app) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS project_shares (
                owner      TEXT NOT NULL,
                project    TEXT NOT NULL,
                kind       TEXT NOT NULL,      -- 'user' | 'group'
                principal  TEXT NOT NULL,
                level      TEXT NOT NULL,
                created_by TEXT,
                created_at TEXT,
                PRIMARY KEY (owner, project, kind, principal)
            )
        ''')
        db.execute('CREATE INDEX IF NOT EXISTS shares_by_principal '
                   'ON project_shares (kind, principal)')


def at_least(level, wanted):
    return _RANK.get(level, 0) >= _RANK[wanted]


def user_groups(user):
    return set(getattr(user, 'groups', None) or [])


def level_for(app, user, owner, project):
    """The best access user has to owner's project, or None."""
    if user.username == owner:
        return OWNER
    groups = sorted(user_groups(user))
    marks = ','.join('?' * len(groups))
    sql = ("SELECT level FROM project_shares WHERE owner = ? AND project = ? AND "
           "((kind = 'user' AND principal = ?)")
    args = [owner, project, user.username]
    if groups:
        sql += " OR (kind = 'group' AND principal IN (%s))" % marks
        args += groups
    sql += ')'
    with get_db(app) as db:
        levels = [r['level'] for r in db.execute(sql, args).fetchall()]
    best = max(levels, key=lambda lv: _RANK.get(lv, 0), default=None)
    if best is None and user.is_admin:
        return VIEW
    return best


def shares_for(app, owner, project):
    with get_db(app) as db:
        rows = db.execute('SELECT * FROM project_shares WHERE owner = ? AND project = ? '
                          'ORDER BY kind DESC, principal', (owner, project)).fetchall()
    return [dict(r) for r in rows]


def shared_with(app, user):
    """[{owner, project, level}] shared with user, directly or by group,
    best level per project."""
    groups = sorted(user_groups(user))
    sql = ("SELECT owner, project, level FROM project_shares WHERE "
           "((kind = 'user' AND principal = ?)")
    args = [user.username]
    if groups:
        sql += " OR (kind = 'group' AND principal IN (%s))" % ','.join('?' * len(groups))
        args += groups
    sql += ') AND owner != ?'
    args.append(user.username)
    best = {}
    with get_db(app) as db:
        for r in db.execute(sql, args).fetchall():
            key = (r['owner'], r['project'])
            if _RANK[r['level']] > _RANK.get(best.get(key), 0):
                best[key] = r['level']
    return [{'owner': o, 'project': p, 'level': lv}
            for (o, p), lv in sorted(best.items())]


def known_groups(app):
    """Every group seen on an SSO sign-in, for the share picker."""
    with get_db(app) as db:
        rows = db.execute('SELECT groups FROM users WHERE groups IS NOT NULL').fetchall()
    found = set()
    for r in rows:
        try:
            found.update(json.loads(r['groups']))
        except ValueError:
            pass
    return sorted(found)


def set_share(app, owner, project, kind, principal, level, by):
    from .models import User
    principal = (principal or '').strip()
    if kind not in ('user', 'group'):
        raise SharingError('Share with a user or a group.')
    if level not in LEVELS:
        raise SharingError('Choose view, edit or deploy.')
    if not principal or len(principal) > 256:
        raise SharingError('Name the user or group to share with.')
    if kind == 'user':
        principal = principal.lower()
        if principal == owner:
            raise SharingError('The owner already has full access.')
        if User.get_by_username(principal, app) is None:
            raise SharingError('There is no user called %s.' % principal)
    with get_db(app) as db:
        db.execute('INSERT INTO project_shares (owner, project, kind, principal, level, '
                   'created_by, created_at) VALUES (?,?,?,?,?,?,?) '
                   'ON CONFLICT (owner, project, kind, principal) DO UPDATE SET '
                   'level = excluded.level',
                   (owner, project, kind, principal, level, by,
                    datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')))


def remove_share(app, owner, project, kind, principal):
    with get_db(app) as db:
        db.execute('DELETE FROM project_shares WHERE owner = ? AND project = ? '
                   'AND kind = ? AND principal = ?', (owner, project, kind, principal))


def forget_project(app, owner, project):
    """A deleted project's shares go with it."""
    with get_db(app) as db:
        db.execute('DELETE FROM project_shares WHERE owner = ? AND project = ?',
                   (owner, project))


def forget_user(app, username):
    """A deleted user's shares in other people's projects go with them."""
    with get_db(app) as db:
        db.execute("DELETE FROM project_shares WHERE kind = 'user' AND principal = ?",
                   (username,))


def shared_projects_owned_by(app, username):
    """Names of username's projects that are shared with anyone."""
    with get_db(app) as db:
        rows = db.execute('SELECT DISTINCT project FROM project_shares WHERE owner = ? '
                          'ORDER BY project', (username,)).fetchall()
    return [r['project'] for r in rows]


def transfer(app, owner, project, new_owner, by):
    """Give owner's project to new_owner.

    The folder moves under the new owner, its shares and audit history
    follow it, the new owner's own share is dropped (they now own it), and
    the previous owner keeps edit access if they still exist.
    """
    from .models import User
    from .project import project_dir, project_lock
    new_owner = (new_owner or '').strip().lower()
    if new_owner == owner:
        raise SharingError('%s already owns it.' % owner)
    if User.get_by_username(new_owner, app) is None:
        raise SharingError('There is no user called %s.' % new_owner)
    src = project_dir(app, owner, project)
    dst = project_dir(app, new_owner, project)
    if not os.path.isdir(src):
        raise SharingError('Project not found.')
    if os.path.exists(dst):
        raise SharingError('%s already has a project called %s.' % (new_owner, project))

    with project_lock(app, owner, project):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    # The old lock file lived beside the project; it is no longer needed.
    old_lock = os.path.join(os.path.dirname(src), '.%s.lock' % project)
    if os.path.exists(old_lock):
        os.remove(old_lock)

    with get_db(app) as db:
        db.execute("DELETE FROM project_shares WHERE owner = ? AND project = ? "
                   "AND kind = 'user' AND principal = ?", (owner, project, new_owner))
        db.execute('UPDATE project_shares SET owner = ? WHERE owner = ? AND project = ?',
                   (new_owner, owner, project))
        db.execute('UPDATE audit_log SET owner = ? WHERE owner = ? AND project = ?',
                   (new_owner, owner, project))
    if User.get_by_username(owner, app) is not None:
        set_share(app, new_owner, project, 'user', owner, EDIT, by)
