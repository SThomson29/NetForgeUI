import sqlite3
import os
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


def get_db(app):
    db = sqlite3.connect(app.config['DATABASE'])
    db.row_factory = sqlite3.Row
    return db


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
        # Databases created before session_version existed.
        cols = {r['name'] for r in db.execute('PRAGMA table_info(users)')}
        if 'session_version' not in cols:
            db.execute('ALTER TABLE users ADD COLUMN '
                       'session_version INTEGER NOT NULL DEFAULT 0')
        db.commit()


class User(UserMixin):
    """A login.

    The session cookie stores get_id(), which carries session_version as
    well as the id. Changing the password bumps the version, so every
    session issued before the change stops loading — a stolen cookie does
    not outlive a password reset.
    """

    def __init__(self, id, username, is_admin=False, session_version=0):
        self.id       = id
        self.username = username
        self.is_admin = bool(is_admin)
        self.session_version = session_version or 0

    @staticmethod
    def _from_row(row):
        return User(row['id'], row['username'], row['is_admin'],
                    row['session_version'])

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
            rows = db.execute('SELECT id, username, is_admin, session_version '
                              'FROM users ORDER BY username').fetchall()
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
