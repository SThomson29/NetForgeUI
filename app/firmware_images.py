"""
The firmware image catalogue: the .swi files in FIRMWARE_DIR, with each
one's SHA-256, AOS-CX version and who uploaded it.

Images can arrive two ways: uploaded by an admin (Admin → Firmware images),
or copied into the directory by hand. Either way they're listed; a copied
one is hashed the first time it's seen, and again only if it changes.

The version comes from the image itself where it can be read (AOS-CX images
carry it, e.g. FL.10.13.1005, near the start of the file), else from the
file name. The two-letter prefix is the platform family, which is what the
firmware page compares against each switch's running version.
"""

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone

from .models import get_db

CHUNK = 1024 * 1024
HEADER_BYTES = 4 * 1024 * 1024

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.swi$')

# FL.10.13.1005 in the image; FL_10_13_1005 or 10_13_1005 in a file name.
_IN_FILE = re.compile(rb'\b([A-Z]{2})\.(\d{2})\.(\d{2})\.(\d{4})\b')
_IN_NAME = re.compile(r'(?:\b|_)([A-Z]{2})[._](\d{2})[._](\d{2})[._](\d{4})(?!\d)')
_IN_NAME_NO_PLATFORM = re.compile(r'(?<!\d)(\d{2})[._](\d{2})[._](\d{4})(?!\d)')


class ImageError(ValueError):
    """An upload or delete that can't be done, with a message saying why."""


READ_ONLY = ('The firmware folder is read-only inside the container. Mount it '
             'read-write (drop ":ro" from the volume, or read_only in Terraform) '
             'to upload or delete images here.')


def writable(app):
    """Whether images can be uploaded and deleted from the browser."""
    fw_dir = app.config.get('FIRMWARE_DIR')
    if not fw_dir:
        return False
    if not os.path.isdir(fw_dir):
        parent = os.path.dirname(os.path.abspath(fw_dir))
        return os.access(parent, os.W_OK)
    return os.access(fw_dir, os.W_OK)


def _ensure_table(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS firmware_images (
            name        TEXT PRIMARY KEY,
            size        INTEGER NOT NULL,
            mtime_ns    INTEGER NOT NULL,
            sha256      TEXT NOT NULL,
            version     TEXT,
            platform    TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT
        )
    ''')


# ── Versions ───────────────────────────────────────────────

def parse_version(text):
    """'FL.10.13.1005' -> ('FL', '10.13.1005'); '10.13.1005' -> (None, ...)."""
    m = re.fullmatch(r'\s*(?:([A-Z]{2})\.)?(\d{2}\.\d{2}\.\d{4})\s*', str(text or ''))
    return (m.group(1), m.group(2)) if m else (None, None)


def version_from_name(name):
    m = _IN_NAME.search(name)
    if m:
        return m.group(1), '%s.%s.%s' % m.group(2, 3, 4)
    m = _IN_NAME_NO_PLATFORM.search(name)
    if m:
        return None, '%s.%s.%s' % m.groups()
    return None, None


def image_version(path, name):
    """(platform, version, source) for an image; source is 'image',
    'file name' or None when neither gives one."""
    try:
        with open(path, 'rb') as f:
            head = f.read(HEADER_BYTES)
        m = _IN_FILE.search(head)
        if m:
            return m.group(1).decode(), '%s.%s.%s' % tuple(g.decode() for g in m.groups()[1:]), 'image'
    except OSError:
        pass
    platform, version = version_from_name(name)
    return platform, version, ('file name' if version else None)


# ── Catalogue ──────────────────────────────────────────────

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def catalogue(app):
    """[{name, size, sha256, platform, version, uploaded_by, uploaded_at}]
    for every selectable image, by name."""
    from .firmware_routes import list_firmware_images, resolve_image_path
    names = list_firmware_images(app)
    out = []
    with get_db(app) as db:
        _ensure_table(db)
        known = {r['name']: dict(r) for r in db.execute('SELECT * FROM firmware_images')}
        for name in names:
            path = resolve_image_path(app, name)
            if not path:
                continue
            st = os.stat(path)
            row = known.get(name)
            if not row or row['size'] != st.st_size or row['mtime_ns'] != st.st_mtime_ns:
                platform, version, _src = image_version(path, name)
                row = {'name': name, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
                       'sha256': _sha256(path), 'version': version, 'platform': platform,
                       'uploaded_by': (row or {}).get('uploaded_by'),
                       'uploaded_at': (row or {}).get('uploaded_at')}
                db.execute('INSERT OR REPLACE INTO firmware_images (name, size, mtime_ns, '
                           'sha256, version, platform, uploaded_by, uploaded_at) '
                           'VALUES (:name, :size, :mtime_ns, :sha256, :version, :platform, '
                           ':uploaded_by, :uploaded_at)', row)
            out.append(row)
        # Forget files that have gone.
        for gone in set(known) - set(names):
            db.execute('DELETE FROM firmware_images WHERE name = ?', (gone,))
    return out


def save_upload(app, name, stream, *, length, by, max_bytes):
    """Stream an upload into FIRMWARE_DIR as name. The file only appears
    under its name once complete, so a half-finished upload is never
    listed or selectable. Returns its catalogue entry."""
    if not NAME.match(name or ''):
        raise ImageError('Give the image a plain .swi file name: letters, numbers, '
                         'dots, hyphens and underscores.')
    if length is not None and length > max_bytes:
        raise ImageError('That file is larger than the %d MB limit.' % (max_bytes // CHUNK))
    fw_dir = app.config.get('FIRMWARE_DIR')
    if not fw_dir:
        raise ImageError('FIRMWARE_DIR is not set.')
    if not writable(app):
        raise ImageError(READ_ONLY)
    os.makedirs(fw_dir, exist_ok=True)
    final = os.path.join(fw_dir, name)
    if os.path.lexists(final):
        raise ImageError('%s already exists. Delete it first to replace it.' % name)

    tmp = os.path.join(fw_dir, '.upload-%s.part' % uuid.uuid4().hex)
    h = hashlib.sha256()
    try:
        _write(tmp, final, stream, h, max_bytes, length, name)
    except OSError as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise ImageError(READ_ONLY if e.errno in (13, 30) else
                         'Could not save the image: %s' % e.strerror)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return _record(app, final, name, h.hexdigest(), by)


def _write(tmp, final, stream, h, max_bytes, length, name):
    """Copy stream to tmp in chunks, hashing as it goes, then move it to
    final. Raises ImageError for a bad upload, OSError for a disk problem."""
    size = 0
    with open(tmp, 'wb') as out:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                raise ImageError('That file is larger than the %d MB limit.'
                                 % (max_bytes // CHUNK))
            h.update(block)
            out.write(block)
    if size == 0:
        raise ImageError('The upload was empty.')
    if length is not None and size != length:
        raise ImageError('The upload was cut short (%d of %d bytes). Try again.'
                         % (size, length))
    if os.path.lexists(final):
        raise ImageError('%s already exists. Delete it first to replace it.' % name)
    os.replace(tmp, final)


def _record(app, final, name, sha256, by):
    st = os.stat(final)
    platform, version, _src = image_version(final, name)
    row = {'name': name, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns,
           'sha256': sha256, 'version': version, 'platform': platform,
           'uploaded_by': by,
           'uploaded_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    with get_db(app) as db:
        _ensure_table(db)
        db.execute('INSERT OR REPLACE INTO firmware_images (name, size, mtime_ns, sha256, '
                   'version, platform, uploaded_by, uploaded_at) VALUES (:name, :size, '
                   ':mtime_ns, :sha256, :version, :platform, :uploaded_by, :uploaded_at)', row)
    return row


def delete_image(app, name, in_use=()):
    from .firmware_routes import resolve_image_path
    path = resolve_image_path(app, name)
    if not path:
        raise ImageError('No image called %s.' % name)
    if name in in_use:
        raise ImageError('%s is being installed by a firmware run right now.' % name)
    try:
        os.remove(path)
    except OSError as e:
        raise ImageError(READ_ONLY if e.errno in (13, 30) else
                         'Could not delete it: %s' % e.strerror)
    with get_db(app) as db:
        _ensure_table(db)
        db.execute('DELETE FROM firmware_images WHERE name = ?', (name,))


def remove_stale_uploads(fw_dir):
    """Partial uploads left by a restart mid-upload."""
    try:
        for n in os.listdir(fw_dir):
            if n.startswith('.upload-') and n.endswith('.part'):
                os.remove(os.path.join(fw_dir, n))
    except OSError:
        pass
