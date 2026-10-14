import contextlib
import sqlite3
import os
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


@contextlib.contextmanager
def get_db(app):
    """A connection that commits on success, rolls back on error, and is
    always closed. sqlite3's own `with connection:` only does the first
    two, leaving every connection open until garbage collection."""
    db = sqlite3.connect(app.config['DATABASE'])
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


# Added after the users table first shipped.
#   source      'local' (created in Admin, signs in with a password) or 'sso'
#   oidc_sub    the identity provider's stable subject id, once linked
#   email       as the identity provider last reported it
#   link_email  set by an admin on a local account: the next SSO sign-in
#               with this email attaches to the account
#   sso_admin   admin through the identity provider's admin group,
#               re-evaluated at every SSO sign-in. Kept apart from is_admin,
#               which an admin sets by hand, so an SSO sign-in never takes
#               away an admin role granted locally.
_ADDED_COLUMNS = [
    ('session_version', 'INTEGER NOT NULL DEFAULT 0'),
    ('source',          "TEXT NOT NULL DEFAULT 'local'"),
    ('oidc_sub',        'TEXT'),
    ('email',           'TEXT'),
    ('link_email',      'TEXT'),
    ('sso_admin',       'INTEGER NOT NULL DEFAULT 0'),
    ('groups',          'TEXT'),
    ('last_sso_login',  'TEXT'),
]


def init_db(app):
    os.makedirs(os.path.dirname(app.config['DATABASE']), exist_ok=True)
    with get_db(app) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                session_version INTEGER NOT NULL DEFAULT 0
            )
        ''')
        # Columns added after the table first shipped, for older databases.
        cols = {r['name'] for r in db.execute('PRAGMA table_info(users)')}
        for name, decl in _ADDED_COLUMNS:
            if name not in cols:
                db.execute('ALTER TABLE users ADD COLUMN %s %s' % (name, decl))
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_by_sub '
                   'ON users (oidc_sub) WHERE oidc_sub IS NOT NULL')
        db.commit()

    from .audit import init_audit
    init_audit(app)


class User(UserMixin):
    """A login.

    The session cookie stores get_id(), which carries session_version as
    well as the id. Changing the password bumps the version, so every
    session issued before the change stops loading — a stolen cookie does
    not outlive a password reset.
    """

    def __init__(self, id, username, is_admin=False, session_version=0,
                 source='local', oidc_sub=None, email=None, link_email=None,
                 sso_admin=False):
        self.id       = id
        self.username = username
        self.local_admin = bool(is_admin)
        self.sso_admin = bool(sso_admin)
        self.session_version = session_version or 0
        self.source = source or 'local'
        self.oidc_sub = oidc_sub
        self.email = email
        self.link_email = link_email

    @property
    def is_admin(self):
        return self.local_admin or self.sso_admin

    @property
    def has_password(self):
        """Whether the account signs in with a password it knows. Accounts
        created by SSO have a random one nobody holds."""
        return self.source == 'local'

    @staticmethod
    def _from_row(row):
        keys = row.keys()

        def col(name, default=None):
            return row[name] if name in keys else default

        return User(row['id'], row['username'], row['is_admin'],
                    col('session_version', 0), col('source', 'local'),
                    col('oidc_sub'), col('email'), col('link_email'),
                    col('sso_admin', 0))

    def get_id(self):
        return '%d:%d' % (self.id, self.session_version)

    @staticmethod
    def from_session_id(value, app):
        """Flask-Login user_loader: the user, or None if the session is
        stale. Sessions from before versioning carry a bare id and count
        as version 0, so upgrading does not log anyone out."""
        try:
            uid, _, ver = str(value).partition(':')
            uid, ver = int(uid), int(ver or 0)
        except ValueError:
            return None
        user = User.get(uid, app)
        if user is None or user.session_version != ver:
            return None
        return user

    @staticmethod
    def get(user_id, app):
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if row:
            return User._from_row(row)
        return None

    @staticmethod
    def get_by_username(username, app):
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if row:
            return User._from_row(row)
        return None

    @staticmethod
    def authenticate(username, password, app):
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if row and check_password_hash(row['password'], password):
            return User._from_row(row)
        return None

    @staticmethod
    def create(username, password, app, is_admin=False):
        hashed = generate_password_hash(password)
        with get_db(app) as db:
            db.execute(
                'INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)',
                (username, hashed, int(is_admin))
            )
            db.commit()

    @staticmethod
    def all_users(app):
        with get_db(app) as db:
            rows = db.execute('SELECT * FROM users ORDER BY username').fetchall()
        return [User._from_row(r) for r in rows]

    @staticmethod
    def delete(user_id, app):
        with get_db(app) as db:
            db.execute('DELETE FROM users WHERE id = ?', (user_id,))
            db.commit()

    @staticmethod
    def change_password(user_id, new_password, app):
        hashed = generate_password_hash(new_password)
        with get_db(app) as db:
            db.execute('UPDATE users SET password = ?, '
                       'session_version = session_version + 1 WHERE id = ?',
                       (hashed, user_id))
            db.commit()

    # ── SSO ────────────────────────────────────────────────

    @staticmethod
    def _one(app, sql, args):
        with get_db(app) as db:
            row = db.execute(sql, args).fetchone()
        return User._from_row(row) if row else None

    @staticmethod
    def get_by_sub(sub, app):
        return User._one(app, 'SELECT * FROM users WHERE oidc_sub = ?', (sub,))

    @staticmethod
    def get_by_link_email(email, app):
        return User._one(app, 'SELECT * FROM users WHERE link_email = ? '
                              'AND oidc_sub IS NULL', (email.lower(),))

    @staticmethod
    def get_sso_by_email(email, app):
        return User._one(app, "SELECT * FROM users WHERE source = 'sso' "
                              'AND lower(email) = ?', (email.lower(),))

    @staticmethod
    def create_sso(username, sub, app):
        """An account for a first SSO sign-in. Its password is random and
        never shown, so it can only be used through SSO."""
        import secrets
        with get_db(app) as db:
            db.execute("INSERT INTO users (username, password, is_admin, source, "
                       "oidc_sub) VALUES (?, ?, 0, 'sso', ?)",
                       (username, generate_password_hash(secrets.token_urlsafe(32)), sub))
        return User.get_by_sub(sub, app)

    @staticmethod
    def record_sso_login(user_id, *, sub, email, groups, sso_admin, when, app):
        """Refresh what the identity provider says at each sign-in. Linking
        a pending local account happens here too: it gains the subject id
        and stops waiting for the email."""
        import json
        with get_db(app) as db:
            db.execute('UPDATE users SET oidc_sub = ?, email = ?, groups = ?, '
                       'sso_admin = ?, last_sso_login = ?, link_email = NULL '
                       'WHERE id = ?',
                       (sub, email, json.dumps(sorted(groups)), int(sso_admin),
                        when, user_id))

    @staticmethod
    def set_link_email(user_id, email, app):
        with get_db(app) as db:
            db.execute('UPDATE users SET link_email = ? WHERE id = ?',
                       (email.lower() if email else None, user_id))

    @staticmethod
    def unlink_sso(user_id, app):
        """Detach a local account from its SSO identity. Sessions end, as an
        SSO-only admin role goes with it."""
        with get_db(app) as db:
            db.execute('UPDATE users SET oidc_sub = NULL, link_email = NULL, '
                       'sso_admin = 0, session_version = session_version + 1 '
                       'WHERE id = ?', (user_id,))

    @staticmethod
    def move_sso_identity(from_id, to_id, app):
        """Give to_id the SSO identity from_id holds, and delete from_id.
        Used when someone signed in by SSO before an admin linked them to
        their local account."""
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE id = ?', (from_id,)).fetchone()
            db.execute('DELETE FROM users WHERE id = ?', (from_id,))
            db.execute('UPDATE users SET oidc_sub = ?, email = ?, groups = ?, '
                       'sso_admin = ?, last_sso_login = ?, link_email = NULL, '
                       'session_version = session_version + 1 WHERE id = ?',
                       (row['oidc_sub'], row['email'], row['groups'], row['sso_admin'],
                        row['last_sso_login'], to_id))
