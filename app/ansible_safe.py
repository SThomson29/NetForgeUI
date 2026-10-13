"""
Write data for Ansible without letting it be evaluated as a template.

Ansible templates variable values: a string containing {{ ... }} is
evaluated when the variable is used, and lookup plugins can run commands on
the controller from there. Values that reach the deploy and firmware
playbooks come from the request — switch credentials, addresses and names —
so every string is written with Ansible's !unsafe tag, which keeps it
literal while behaving as an ordinary string everywhere it is used.

Only strings are tagged; numbers, booleans and None are left as they are.
"""

import yaml


class _UnsafeDumper(yaml.SafeDumper):
    """SafeDumper that emits every string scalar as !unsafe."""


def _represent_unsafe_str(dumper, value):
    return dumper.represent_scalar('!unsafe', value, style="'")


_UnsafeDumper.add_representer(str, _represent_unsafe_str)


def dump_unsafe(data, stream):
    """yaml.dump, but every string value is tagged !unsafe.

    Mapping keys are emitted plainly: YAML keys are not templated by
    Ansible, and the hostnames used as inventory keys are validated against
    a strict pattern before they get here.
    """
    yaml.dump(_tag_values(data), stream, Dumper=_UnsafeDumper,
              default_flow_style=False, sort_keys=False)


class _PlainKey(str):
    """A string that must be written untagged — used for mapping keys."""


def _represent_plain_key(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', str(value))


_UnsafeDumper.add_representer(_PlainKey, _represent_plain_key)


def _tag_values(obj):
    if isinstance(obj, dict):
        return {_PlainKey(k) if isinstance(k, str) else k: _tag_values(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_tag_values(v) for v in obj]
    return obj


# Hostnames become inventory keys and are joined into --limit, where a comma,
# colon or '!' would change which hosts a play targets. The same pattern the
# rest of the app uses for host names.
import re as _re

_HOSTNAME = _re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
# A management address: an IPv4/IPv6 literal or a resolvable name.
_ADDRESS = _re.compile(r'^[A-Za-z0-9.:_\-]{1,253}$')


def validate_targets(hosts):
    """Return a list of problems with a request's host list, empty if fine."""
    problems = []
    if not isinstance(hosts, list):
        return ['hosts must be a list']
    for i, h in enumerate(hosts):
        if not isinstance(h, dict):
            problems.append('host %d is not an object' % i)
            continue
        name = str(h.get('hostname') or '')
        addr = str(h.get('mgmt_ip') or '')
        if not _HOSTNAME.match(name):
            problems.append('invalid hostname %r' % name[:64])
        if addr and not _ADDRESS.match(addr):
            problems.append('invalid management address for %s' % name[:64])
    return problems
