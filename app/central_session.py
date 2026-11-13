"""
A Central tenant's API client, held for the session and never stored.

Applying a Central project needs an API client (client ID and secret) for
the tenant. Like a switch password on the Deploy page, it is typed in by
the person running the job and is not written to disk, the database or the
audit log. Unlike a switch password it is needed for several runs in a row
(a plan, then an apply, then the next section), so once entered it is kept
in this process's memory for that user and project until it has gone
unused for CENTRAL_UNLOCK_MINUTES, they lock it, or the container restarts.

Gunicorn runs a single worker (see the Dockerfile), so one process sees
every request; a restart clears everything, which is the point.
"""

import os
import re
import threading
import time

TOKEN_URL = 'https://sso.common.cloud.hpe.com/as/token.oauth2'
IDLE_SECS = int(os.environ.get('CENTRAL_UNLOCK_MINUTES', '60')) * 60

# The region's API address is used by the server, so it has to be one of
# Central's own: anything else would let a request be aimed at another host.
_BASE_URL = re.compile(r'^https://[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)*$')
ALLOWED_SUFFIXES = tuple(s.strip().lower() for s in os.environ.get(
    'CENTRAL_API_SUFFIXES', '.api.central.arubanetworks.com,.api.central.hpe.com').split(',') if s.strip())

_store = {}
_lock = threading.Lock()


class UnlockError(ValueError):
    """Credentials that can't be used, with a message for the page."""


def clean_base_url(value):
    url = str(value or '').strip().rstrip('/').lower()
    if not _BASE_URL.match(url) or not url[len('https://'):].endswith(ALLOWED_SUFFIXES):
        raise UnlockError('The API address must be the https address of a Central region, '
                          'such as https://de1.api.central.arubanetworks.com.')
    return url


def verify(client_id, client_secret):
    """Ask HPE's sign-in service for a token, to check the client works.
    Raises UnlockError with the reason if it doesn't."""
    import requests
    try:
        res = requests.post(TOKEN_URL, auth=(client_id, client_secret),
                            data={'grant_type': 'client_credentials'}, timeout=20)
    except requests.RequestException as e:
        raise UnlockError('Could not reach HPE sign-in to check the API client: %s' % type(e).__name__)
    if res.status_code != 200:
        raise UnlockError('HPE sign-in refused the API client (HTTP %s). Check the client ID and secret.'
                          % res.status_code)


def _key(username, owner, project):
    return (username, owner, project)


def unlock(username, owner, project, base_url, client_id, client_secret, clock=time.monotonic):
    """Check and keep the API client. Returns status()."""
    base_url = clean_base_url(base_url)
    client_id, client_secret = str(client_id or '').strip(), str(client_secret or '')
    if not client_id or not client_secret:
        raise UnlockError('Enter the client ID and the client secret.')
    verify(client_id, client_secret)
    with _lock:
        _store[_key(username, owner, project)] = {
            'base_url': base_url, 'client_id': client_id, 'client_secret': client_secret,
            'used': clock()}
    return status(username, owner, project, clock=clock)


def get(username, owner, project, clock=time.monotonic):
    """The kept API client as {base_url, client_id, client_secret}, or
    None if there is none or it has gone unused too long. Using it keeps
    it for another idle period."""
    now = clock()
    with _lock:
        entry = _store.get(_key(username, owner, project))
        if entry is None:
            return None
        if now - entry['used'] > IDLE_SECS:
            del _store[_key(username, owner, project)]
            return None
        entry['used'] = now
        return {k: entry[k] for k in ('base_url', 'client_id', 'client_secret')}


def status(username, owner, project, clock=time.monotonic):
    """What the page may show: never the secret."""
    entry = get(username, owner, project, clock=clock)
    if entry is None:
        return {'unlocked': False}
    client = entry['client_id']
    return {'unlocked': True, 'base_url': entry['base_url'],
            'client_id': client[:4] + '…' + client[-4:] if len(client) > 10 else client,
            'idle_minutes': IDLE_SECS // 60}


def lock(username, owner, project):
    with _lock:
        _store.pop(_key(username, owner, project), None)


def forget_user(username):
    """Drop everything kept for a user, as on sign-out."""
    with _lock:
        for key in [k for k in _store if k[0] == username]:
            del _store[key]


def forget_all():
    with _lock:
        _store.clear()
