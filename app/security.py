"""
Application-wide hardening that does not belong to any one blueprint.

    check_secret_key      refuse to start with a known or trivial SECRET_KEY
    install_origin_check  reject cross-site state-changing requests (CSRF)
    LoginThrottle         slow down password guessing

Each piece is separate so it can be tested without building the whole app.
"""

import threading
import time
from urllib.parse import urlsplit

from flask import abort, request


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------

# Flask signs the session cookie with SECRET_KEY, so anyone who knows it can
# forge a logged-in session as any user, admins included. These are the
# values that have shipped in this repo's docs and compose files — the last
# one was committed as a real key and is in the public history.
KNOWN_WEAK_KEYS = frozenset({
    '',
    'change-me',
    'change-me-in-production',
    'change-me-to-a-long-random-string',
    'XdfLjM@Ug%x69q7l81c1',
})

MIN_SECRET_KEY_LENGTH = 16


class InsecureSecretKey(RuntimeError):
    pass


def check_secret_key(key):
    """Raise InsecureSecretKey if the key is a known value or too short."""
    key = key or ''
    if key in KNOWN_WEAK_KEYS:
        raise InsecureSecretKey(
            'SECRET_KEY is a published default. Anyone who knows it can forge '
            'a login as any user. Set SECRET_KEY to a long random string, '
            'e.g. python -c "import secrets; print(secrets.token_urlsafe(48))"')
    if len(key) < MIN_SECRET_KEY_LENGTH:
        raise InsecureSecretKey(
            'SECRET_KEY is shorter than %d characters. Set it to a long random '
            'string.' % MIN_SECRET_KEY_LENGTH)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

_STATE_CHANGING = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def _host_of(url):
    try:
        return (urlsplit(url).netloc or '').lower()
    except ValueError:
        return ''


def origin_allowed(origin_header, referer_header, own_hosts, trusted):
    """Decide whether a state-changing request came from this site.

    Browsers send Origin on cross-site POSTs and cannot be made to forge it,
    so when it is present it must name this host. Older browsers may send
    only Referer; it is checked the same way. With neither header the
    request is not from a browser page at all — curl, scripts — and CSRF
    does not apply, so it is let through to normal authentication.
    """
    own = {h.lower() for h in own_hosts if h}
    allowed = own | {_host_of(t) or t.lower() for t in trusted}

    if origin_header:
        # 'null' is sent by sandboxed frames and some redirects; never ours.
        return origin_header != 'null' and _host_of(origin_header) in allowed
    if referer_header:
        return _host_of(referer_header) in allowed
    return True


def install_origin_check(app):
    @app.before_request
    def _reject_cross_site_writes():
        if request.method not in _STATE_CHANGING:
            return None
        own = [request.host, request.headers.get('X-Forwarded-Host', '')]
        if not origin_allowed(request.headers.get('Origin'),
                              request.headers.get('Referer'),
                              own, app.config.get('TRUSTED_ORIGINS', [])):
            abort(403)
        return None


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

class LoginThrottle:
    """Count recent failed logins and refuse further attempts past a limit.

    Tracked per (client address, username) and per client address alone, so
    guessing one account and spraying many are both limited. Held in memory:
    gunicorn runs a single worker (see the Dockerfile), so one process sees
    every attempt. A restart clears it, which is acceptable for a throttle.
    """

    def __init__(self, max_failures=5, window_seconds=900, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window_seconds
        self.clock = clock
        self._failures = {}
        self._lock = threading.Lock()

    def _keys(self, addr, username):
        return [('acct', addr, (username or '').lower()), ('addr', addr, '')]

    def _recent(self, key, now):
        stamps = [t for t in self._failures.get(key, []) if now - t < self.window]
        if stamps:
            self._failures[key] = stamps
        else:
            self._failures.pop(key, None)
        return stamps

    def blocked(self, addr, username):
        now = self.clock()
        with self._lock:
            acct, by_addr = self._keys(addr, username)
            return (len(self._recent(acct, now)) >= self.max_failures
                    # spraying many usernames from one address
                    or len(self._recent(by_addr, now)) >= self.max_failures * 4)

    def record_failure(self, addr, username):
        now = self.clock()
        with self._lock:
            for key in self._keys(addr, username):
                self._failures.setdefault(key, []).append(now)

    def record_success(self, addr, username):
        with self._lock:
            self._failures.pop(self._keys(addr, username)[0], None)
