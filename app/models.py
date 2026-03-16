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
                is_admin INTEGER NOT NULL DEFAULT 0
            )
        ''')
        db.commit()


class User(UserMixin):
    def __init__(self, id, username, is_admin=False):
        self.id       = id
        self.username = username
        self.is_admin = bool(is_admin)

    @staticmethod
    def get(user_id, app):
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if row:
            return User(row['id'], row['username'], row['is_admin'])
        return None

    @staticmethod
    def get_by_username(username, app):
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if row:
            return User(row['id'], row['username'], row['is_admin'])
        return None

    @staticmethod
    def authenticate(username, password, app):
        with get_db(app) as db:
            row = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if row and check_password_hash(row['password'], password):
            return User(row['id'], row['username'], row['is_admin'])
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
            rows = db.execute('SELECT id, username, is_admin FROM users ORDER BY username').fetchall()
        return [User(r['id'], r['username'], r['is_admin']) for r in rows]

    @staticmethod
    def delete(user_id, app):
        with get_db(app) as db:
            db.execute('DELETE FROM users WHERE id = ?', (user_id,))
            db.commit()

    @staticmethod
    def change_password(user_id, new_password, app):
        hashed = generate_password_hash(new_password)
        with get_db(app) as db:
            db.execute('UPDATE users SET password = ? WHERE id = ?', (hashed, user_id))
            db.commit()
