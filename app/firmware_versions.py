"""
What each switch is running, read over the AOS-CX REST API, and how that
compares with the image chosen on the Firmware page.

One short session per switch (see aoscx_rest): read /firmware and the
platform name. Credentials are used for the call and not kept.
"""

import concurrent.futures

from .aoscx_rest import SwitchError, run
from .firmware_images import parse_version

MAX_PARALLEL = 16


def read_versions(ip, username, password):
    """{current, primary, secondary, booted, platform_name} for one switch.
    Raises SwitchError."""
    def reads(get):
        fw = get('/firmware')
        sysinfo = get('/system', params={'attributes': 'platform_name'}, optional=True)
        return fw, sysinfo

    fw, sysinfo = run(ip, username, password, reads)
    if not isinstance(fw, dict):
        raise SwitchError('unexpected response from the switch')
    return {
        'current':   fw.get('current_version'),
        'primary':   fw.get('primary_version'),
        'secondary': fw.get('secondary_version'),
        'booted':    fw.get('booted_image'),
        'platform_name': (sysinfo or {}).get('platform_name') if isinstance(sysinfo, dict) else None,
    }


def assess(sw, image):
    """How a switch stands against the chosen image.

    sw: read_versions() output; image: {platform, version} (either may be
    None). Returns {verdict, tick, note, other_partition} where verdict is
    'current', 'staged', 'wrong_platform', 'upgrade' or 'unknown'.
    """
    booted = sw.get('booted')
    other = {'primary': 'secondary', 'secondary': 'primary'}.get(booted)
    plat, running = parse_version(sw.get('current'))
    img_plat, img_ver = (image or {}).get('platform'), (image or {}).get('version')
    out = {'other_partition': other}
    if not img_ver:
        return dict(out, verdict='unknown', tick=None,
                    note="the image's version isn't known, so it can't be compared")
    if img_plat and plat and img_plat != plat:
        return dict(out, verdict='wrong_platform', tick=False,
                    note='image is for %s switches; this one runs %s' % (img_plat, plat))
    if running == img_ver:
        return dict(out, verdict='current', tick=False, note='already running %s' % img_ver)
    staged = sw.get(other) if other else None
    if parse_version(staged)[1] == img_ver:
        return dict(out, verdict='staged', tick=True,
                    note='%s is already on %s but not booted' % (img_ver, other))
    return dict(out, verdict='upgrade', tick=True,
                note='%s → %s' % (running or sw.get('current') or '?', img_ver))


def check(hosts, username, password, image):
    """[{hostname, mgmt_ip, ok, error?, versions?, ...assess()}] for each
    {hostname, mgmt_ip}, read in parallel."""
    def one(h):
        row = {'hostname': h['hostname'], 'mgmt_ip': h['mgmt_ip']}
        try:
            versions = read_versions(h['mgmt_ip'], username, password)
        except SwitchError as e:
            return dict(row, ok=False, error=str(e))
        except Exception as e:                      # noqa: BLE001
            return dict(row, ok=False, error='failed (%s)' % e.__class__.__name__)
        return dict(row, ok=True, versions=versions, **assess(versions, image))

    if not hosts:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(hosts))) as ex:
        return list(ex.map(one, hosts))
