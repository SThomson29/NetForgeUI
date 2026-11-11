"""
The project Files page: read any file in a project as text, and edit a
switch's host_vars as YAML.

    hosts.ini                 read-only; the Hosts page owns it
    host_vars/<switch>/*.yml  editable with edit access
    generated_configs/*.ios   read-only; Generate writes them

Editing here is the same power the switch editor gives, typed rather than
filled in, so it needs the same access and passes through the same gates:

    - refused if the switch was saved by anyone since the file was opened
    - written atomically, under the project lock
    - followed by the allocation sync, so Resources matches the addresses
    - recorded as that switch's last save

and its own, because free text can be wrong in ways a form can't:

    - it must parse as YAML, with settings at the top level (or be empty)
    - each setting must be the kind of thing it is in the NetForge skeleton
      for that file: a list where the skeleton has a list, nested settings
      where it has those, a single value where it has one. The templates
      loop over one and read names out of the other, so the wrong kind
      fails the generation or renders nonsense.
    - for a file the switch editor manages, that editor must still be able
      to read the switch afterwards

Only a file that already exists can be saved. New host_vars files come
from the NetForge skeleton, through "Check for missing files" on the Hosts
page, which knows which files suit which stacking type.

The switch editor rewrites each file it manages when the switch is saved
there: values come back, comments and keys it doesn't know do not. Files
it doesn't manage are never touched by it.
"""

import contextlib
import os
import re
import shutil
import tempfile

import yaml

from . import checks, hostvars
from .project import (project_dir, project_generated_configs_dir,
                      project_host_vars_dir, project_hosts_ini_path, project_lock,
                      record_host_save, sync_allocations)
from .utils import (HOSTVARS_FILES, _HOSTNAME_RE, list_project_generated_configs,
                    read_project_hosts)

MAX_EDIT_BYTES = 512 * 1024         # a host_vars file as saved from here
MAX_VIEW_BYTES = 5 * 1024 * 1024    # anything shown

_YML = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\.yml$')


class FileError(Exception):
    """A request that can't be met, with a message for the page."""
    status = 400


class NotFound(FileError):
    status = 404


class Conflict(FileError):
    status = 409


def _hosts(app, owner, project):
    return [h for h in read_project_hosts(app, owner, project)
            if _HOSTNAME_RE.match(h['hostname'])]


def _yml_files(hvdir):
    try:
        names = os.listdir(hvdir)
    except OSError:
        return []
    return sorted(n for n in names if _YML.match(n)
                  and os.path.isfile(os.path.join(hvdir, n))
                  and not os.path.islink(os.path.join(hvdir, n)))


def tree(app, owner, project):
    """What the page lists: {inventory, hosts: [{hostname, stacking,
    files: [{name, form}]}], configs}. form says the switch editor manages
    the file."""
    base = project_host_vars_dir(app, owner, project)
    return {
        'inventory': os.path.isfile(project_hosts_ini_path(app, owner, project)),
        'hosts': [{'hostname': h['hostname'], 'stacking': h['stacking'],
                   'files': [{'name': n, 'form': n in HOSTVARS_FILES}
                             for n in _yml_files(os.path.join(base, h['hostname']))]}
                  for h in _hosts(app, owner, project)],
        'configs': list_project_generated_configs(app, owner, project),
    }


def _hostvars_path(app, owner, project, host, name):
    """The path of an existing host_vars file, or NotFound. host and name
    come from the request, so both are matched against what the project
    actually holds before being joined into a path."""
    host, name = str(host or ''), str(name or '')
    if host not in {h['hostname'] for h in _hosts(app, owner, project)}:
        raise NotFound('No switch called %s in this project.' % host[:64])
    hvdir = os.path.join(project_host_vars_dir(app, owner, project), host)
    if name not in _yml_files(hvdir):
        raise NotFound('%s has no file called %s.' % (host, name[:64]))
    return hvdir, os.path.join(hvdir, name)


def _read_text(path):
    try:
        if os.path.getsize(path) > MAX_VIEW_BYTES:
            raise FileError('This file is too large to show here.')
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        raise NotFound('That file is no longer there.')


def read(app, owner, project, kind, host=None, name=None):
    """{kind, host, name, content, editable, form, fingerprint}. editable
    is whether this kind of file can be saved at all; whether this user
    may is the route's business."""
    if kind == 'hostvars':
        hvdir, path = _hostvars_path(app, owner, project, host, name)
        return {'kind': kind, 'host': host, 'name': name, 'content': _read_text(path),
                'editable': True, 'form': name in HOSTVARS_FILES,
                'fingerprint': checks.hostvars_fingerprint(hvdir) or ''}
    if kind == 'config':
        name = str(name or '')
        if name not in list_project_generated_configs(app, owner, project):
            raise NotFound('There is no generated config called %s.' % name[:64])
        path = os.path.join(project_generated_configs_dir(app, owner, project), name)
        if os.path.islink(path):
            raise NotFound('There is no generated config called %s.' % name[:64])
    elif kind == 'inventory':
        name, path = 'hosts.ini', project_hosts_ini_path(app, owner, project)
        if not os.path.isfile(path):
            raise NotFound('This project has no hosts.ini.')
    else:
        raise FileError('Unknown kind of file.')
    return {'kind': kind, 'host': None, 'name': name, 'content': _read_text(path),
            'editable': False, 'form': False, 'fingerprint': ''}


def _yaml_problem(e):
    mark = getattr(e, 'problem_mark', None)
    what = getattr(e, 'problem', None) or str(e).splitlines()[0]
    if mark is not None:
        return 'line %d, column %d: %s' % (mark.line + 1, mark.column + 1, what)
    return what


_KIND_WORDS = {'list': 'a list', 'dict': 'nested settings', 'value': 'a single value'}


def _kind(value):
    """'list', 'dict', 'value', or None for unset (empty or null), which
    says nothing about what belongs there."""
    if isinstance(value, list):
        return 'list'
    if isinstance(value, dict):
        return 'dict'
    return None if value in (None, '') else 'value'


def shape_problems(data, skeleton, trail=''):
    """Settings in data that are a different kind of thing from the same
    setting in skeleton. Only settings both have are compared, and only
    where both say something: an empty value on either side is skipped."""
    out = []
    for key, expected in skeleton.items():
        if key not in data:
            continue
        want, got = _kind(expected), _kind(data[key])
        where = '%s%s' % (trail, key)
        if want and got and want != got:
            out.append('%s must be %s, not %s' % (where, _KIND_WORDS[want], _KIND_WORDS[got]))
        elif want == 'dict' and got == 'dict':
            out.extend(shape_problems(data[key], expected, where + '.'))
    return out


def _skeleton(app, name):
    """The NetForge skeleton's version of a host_vars file, parsed, or {}."""
    path = os.path.join(app.config.get('SKELETON_DIR') or '', 'aoscx', name)
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.load(f, Loader=hostvars.BASE_LOADER)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def check_content(name, content, hvdir, app=None):
    """Raise FileError if content must not be saved as hvdir/name."""
    if not isinstance(content, str):
        raise FileError('The file content must be text.')
    if len(content.encode('utf-8')) > MAX_EDIT_BYTES:
        raise FileError('That is larger than a host_vars file can be (%d KB).'
                        % (MAX_EDIT_BYTES // 1024))
    if '\x00' in content:
        raise FileError('The file contains a character that can\'t be saved.')
    try:
        data = yaml.load(content, Loader=hostvars.BASE_LOADER)
    except yaml.YAMLError as e:
        raise FileError('Not valid YAML (%s). Nothing was saved.' % _yaml_problem(e))
    # Ansible reads host_vars as named settings; a bare list or value at
    # the top fails the whole generation for this switch.
    if data is not None and not isinstance(data, dict):
        raise FileError('The top level must be settings written as "name: value", '
                        'not a %s. Nothing was saved.'
                        % ('list' if isinstance(data, list) else 'single value'))
    if app is not None and isinstance(data, dict):
        problems = shape_problems(data, _skeleton(app, name))
        if problems:
            raise FileError('%s (going by the NetForge skeleton for %s). Nothing was saved.'
                            % ('; '.join(problems[:5]), name))
    if name in HOSTVARS_FILES:
        # As the switch editor would find the switch after this save.
        with tempfile.TemporaryDirectory() as tmp:
            for other in _yml_files(hvdir):
                if other != name:
                    shutil.copyfile(os.path.join(hvdir, other), os.path.join(tmp, other))
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                f.write(content)
            try:
                hostvars._parse_state(tmp)
            except Exception as e:                  # noqa: BLE001
                raise FileError('The switch editor couldn\'t read %s with this in it '
                                '(%s). Nothing was saved.'
                                % (name, str(e).splitlines()[0][:200] or e.__class__.__name__))


def _write_atomic(path, content):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.tmp-', suffix='.yml')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def save(app, owner, project, host, name, content, base, by):
    """Save an existing host_vars file. base is the fingerprint the page
    read with it; if the switch has been saved since, by anyone and by
    either editor, the save is refused. Returns the new fingerprint."""
    with project_lock(app, owner, project):
        if not os.path.isdir(project_dir(app, owner, project)):
            raise NotFound('That project is no longer there.')
        hvdir, path = _hostvars_path(app, owner, project, host, name)
        current = checks.hostvars_fingerprint(hvdir)
        if not base or base != current:
            from .project import get_project_config
            last = (get_project_config(app, owner, project).get('host_saves') or {}).get(host) or {}
            who = ' by %s at %s' % (last['by'], last['at'].replace('T', ' ').replace('Z', ' UTC')) \
                if last.get('by') else ''
            raise Conflict('%s was changed%s since you opened this file. Reload it to see '
                           'their changes; saving now would overwrite them.' % (host, who))
        if isinstance(content, str):
            content = content.replace('\r\n', '\n')
        check_content(name, content, hvdir, app)
        _write_atomic(path, content)
        record_host_save(app, owner, project, host, by)
        try:
            sync_allocations(app, owner, project, host)
        except Exception:                           # noqa: BLE001
            pass    # as the switch editor: the save stands if the sync fails
        return checks.hostvars_fingerprint(hvdir) or ''
