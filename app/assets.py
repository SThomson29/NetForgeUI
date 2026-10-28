"""
Static files with a content fingerprint in the URL.

    {{ static_url('js/editor.js') }}  ->  /static/js/editor.js?v=3f9a1c2e4b

The fingerprint changes whenever the file does, so after an upgrade no
browser or proxy keeps running an old script against a new server. It is
taken from the file itself, so containers, portable copies and dev all
agree without a version number to maintain.
"""

import hashlib
import os
import threading

from flask import url_for

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

_cache = {}         # filename -> (mtime, fingerprint)
_lock = threading.Lock()


def fingerprint(filename):
    """First 10 hex characters of the file's sha256, or None if missing.
    Recomputed only when the file's modification time changes."""
    path = os.path.join(STATIC_DIR, filename)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    with _lock:
        hit = _cache.get(filename)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(path, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:10]
    with _lock:
        _cache[filename] = (mtime, digest)
    return digest


def static_url(filename):
    v = fingerprint(filename)
    return url_for('static', filename=filename, **({'v': v} if v else {}))
