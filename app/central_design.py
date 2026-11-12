"""
A Central project's design on disk.

    <project>/design/<section>.yml     one file per section of the schema,
                                       holding {var: [rows]} for its lists

One file per section so that two people editing different sections never
collide, and a save is refused only when that same section changed since
it was opened. Files are written atomically under the project lock, the
same way a config project's files are.
"""

import contextlib
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone

import yaml

from .project import project_dir, project_lock, _load_config, _save_config

_SECTION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$')


class DesignConflict(Exception):
    """The section was saved by someone else since it was opened."""


def design_dir(app, owner, project):
    return os.path.join(project_dir(app, owner, project), 'design')


def _path(app, owner, project, section_id):
    if not _SECTION.match(section_id):
        raise ValueError('Not a section name.')
    return os.path.join(design_dir(app, owner, project), '%s.yml' % section_id)


def _text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ''


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest() if text else ''


def dump(data):
    """A section as YAML, in the order it was given."""
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)


def read_section(app, owner, project, sect):
    """({var: rows} for the section's lists, fingerprint of its file)"""
    text = _text(_path(app, owner, project, sect['id']))
    try:
        data = yaml.safe_load(text) if text else {}
    except yaml.YAMLError:
        data = {}
    data = data if isinstance(data, dict) else {}
    out = {}
    for part in sect['lists']:
        rows = data.get(part['var'])
        out[part['var']] = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return out, fingerprint(text)


def read_design(app, owner, project, schema):
    """The whole design: {var: rows} across every section."""
    design = {}
    for sect in schema['sections']:
        design.update(read_section(app, owner, project, sect)[0])
    return design


def counts(app, owner, project, schema):
    """{section id: number of rows}"""
    return {sect['id']: sum(len(rows) for rows in read_section(app, owner, project, sect)[0].values())
            for sect in schema['sections']}


def save_section(app, owner, project, sect, data, base, by):
    """Write a section. base is the fingerprint it was opened with; if the
    file has changed since, DesignConflict. Returns the new fingerprint."""
    path = _path(app, owner, project, sect['id'])
    with project_lock(app, owner, project):
        if fingerprint(_text(path)) != (base or ''):
            raise DesignConflict()
        ordered = {part['var']: data.get(part['var']) or [] for part in sect['lists']}
        text = dump(ordered) if any(ordered.values()) else ''
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if text:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.tmp-', suffix='.yml')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
                raise
        else:
            with contextlib.suppress(OSError):
                os.remove(path)
        cfg = _load_config(app, owner, project)
        saved = cfg.setdefault('design_saved', {})
        saved[sect['id']] = {'at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'by': by}
        _save_config(app, owner, project, cfg)
        return fingerprint(text)


def saved_info(app, owner, project):
    """{section id: {at, by}} for sections that have been saved."""
    return dict(_load_config(app, owner, project).get('design_saved') or {})
