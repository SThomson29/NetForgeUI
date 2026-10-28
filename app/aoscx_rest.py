"""
A short AOS-CX REST session: discover the API version (GET /rest needs no
login), log in, run some reads, always log out.

AOS-CX allows only a few REST sessions per user, so logout runs whatever
happens. The certificate isn't verified: switches serve self-signed ones,
the same trust the deploy and firmware playbooks already extend.
Credentials are used for the session and not kept.
"""

import warnings
from urllib.parse import quote

TIMEOUT = (5, 15)          # connect, read
FALLBACK_PREFIX = '/rest/v10.04'


class SwitchError(Exception):
    """A switch that couldn't be read, with a short reason."""


def _base(ip):
    return 'https://[%s]' % ip if ':' in ip else 'https://%s' % ip


def _prefix(session, base):
    """The newest REST API the switch offers, e.g. /rest/v10.13."""
    try:
        res = session.get(base + '/rest', timeout=TIMEOUT)
        data = res.json() if res.ok else {}
    except ValueError:
        data = {}
    latest = data.get('latest') if isinstance(data, dict) else None
    if isinstance(latest, dict) and str(latest.get('prefix', '')).startswith('/rest/'):
        return latest['prefix']
    versions = sorted(k for k in (data or {}) if str(k).startswith('v10.'))
    return '/rest/' + versions[-1] if versions else FALLBACK_PREFIX


def encode(name):
    """An interface name as a URL path segment: 1/1/1 -> 1%2F1%2F1."""
    return quote(str(name), safe='')


def run(ip, username, password, reads):
    """Log in to the switch at ip, call reads(get) and return its result.

    get(path, params=None, optional=False) returns the JSON at the API
    prefix + path. A failed read raises SwitchError, or returns None when
    optional. Any failure becomes a SwitchError with a short reason.
    """
    import requests
    import urllib3
    base = _base(ip)
    session = requests.Session()
    session.verify = False
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
        try:
            prefix = _prefix(session, base)
            res = session.post(base + prefix + '/login', timeout=TIMEOUT,
                               data={'username': username, 'password': password})
            if res.status_code in (401, 403):
                raise SwitchError('the switch refused the username or password')
            if not res.ok:
                raise SwitchError('login failed (HTTP %d)' % res.status_code)

            def get(path, params=None, optional=False):
                try:
                    r = session.get(base + prefix + path, params=params, timeout=TIMEOUT)
                    if not r.ok:
                        if optional:
                            return None
                        raise SwitchError('could not read %s (HTTP %d)' % (path, r.status_code))
                    return r.json()
                except (requests.RequestException, ValueError):
                    if optional:
                        return None
                    raise

            try:
                return reads(get)
            finally:
                try:
                    session.post(base + prefix + '/logout', timeout=TIMEOUT)
                except requests.RequestException:
                    pass
        except requests.exceptions.SSLError:
            raise SwitchError('TLS handshake failed')
        except requests.exceptions.Timeout:
            raise SwitchError('timed out')
        except requests.exceptions.ConnectionError:
            raise SwitchError("can't connect on port 443; is the REST API (https-server) enabled?")
        except ValueError:
            raise SwitchError('unexpected response from the switch')
        finally:
            session.close()
