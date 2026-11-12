"""
What a Central project's design can contain, read from the collection.

The samthomson.new_central collection (cloned to CENTRAL_REPO) carries the
tenant build: playbooks/tenant_build/schema.yml describes each section of
a design and names the playbook that applies it, and profiles/ describes
every configuration profile type the API has. The forms are drawn from
those files and a saved design is checked against them, so a section added
to the collection appears here when the collection is updated, with no
change to this app.

    load(app)                 the schema, with each section's fields flattened
    catalogue(app)            the profile types the `profiles` section takes
    profile_type(app, name)   one type's fields
    check(app, section, data, design)   -> (clean data, problems)
    mask / unmask             keep secrets out of what the browser is sent

A secret is sent to the browser as MASK. Saving MASK back means "leave it
as it is", so a secret is typed once and never shown again.
"""

import os
import re
import threading

import yaml

try:
    from yaml import CSafeLoader as _Loader
except ImportError:                                  # pragma: no cover
    from yaml import SafeLoader as _Loader

# Designs written for a newer schema_version would be read wrongly here.
SUPPORTED_VERSIONS = (1,)

MASK = '********'
STAGES = [('foundation', 'Foundation'), ('configuration', 'Configuration'), ('devices', 'Devices')]

_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$')
_cache = {}
_lock = threading.Lock()


class CentralError(Exception):
    """Something about the collection that stops Central projects working,
    with a message for the page."""


def build_dir(app):
    return os.path.join(app.config.get('CENTRAL_REPO') or '', 'playbooks', 'tenant_build')


def _read(path):
    """A YAML file's content, re-read only when the file changes."""
    try:
        stamp = os.stat(path)
    except OSError:
        raise CentralError('%s is missing from the Central collection.' % os.path.basename(path))
    key = (stamp.st_mtime_ns, stamp.st_size)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    try:
        with open(path) as f:
            data = yaml.load(f, Loader=_Loader)
    except (OSError, yaml.YAMLError) as e:
        raise CentralError('%s could not be read: %s' % (os.path.basename(path), e))
    with _lock:
        _cache[path] = (key, data)
    return data


def flatten(fields):
    """A list inside a list of fields is spliced in where it stands."""
    out = []
    for field in fields or []:
        if isinstance(field, list):
            out.extend(flatten(field))
        elif isinstance(field, dict):
            out.append(field)
    return out


def lists(section):
    """The lists of rows a section holds: itself, then any under `also`.
    Each is {var, key, fields, kind, label, profile_type}."""
    out = []
    for part in [section] + list(section.get('also') or []):
        out.append({
            'var': part['var'],
            'key': part.get('key'),
            'label': part.get('label') or section.get('label'),
            'kind': part.get('kind') or section.get('kind') or 'rows',
            'profile_type': part.get('profile_type'),
            'fields': flatten(part.get('fields')),
        })
    return out


def load(app):
    """The schema: {schema_version, label, sections: [...]}, each section
    with `lists` (see lists()). Raises CentralError if the collection isn't
    there or its schema is one this version can't read."""
    if not (app.config.get('CENTRAL_REPO') and os.path.isdir(build_dir(app))):
        raise CentralError('The Central collection is not cloned, or has no tenant build. '
                           'An admin can check Admin → Config repo.')
    raw = _read(os.path.join(build_dir(app), 'schema.yml'))
    if not isinstance(raw, dict) or not isinstance(raw.get('sections'), list):
        raise CentralError('The collection\'s schema.yml is not a schema.')
    version = raw.get('schema_version')
    if version not in SUPPORTED_VERSIONS:
        raise CentralError(
            'The Central collection uses design schema version %s; this version of NetForgeUI reads %s. '
            'Update NetForgeUI, or pin the collection to an older release.'
            % (version, ', '.join(str(v) for v in SUPPORTED_VERSIONS)))
    sections = []
    for section in raw['sections']:
        if not isinstance(section, dict) or not _NAME.match(str(section.get('id', ''))):
            raise CentralError('The collection\'s schema.yml has a section without a usable id.')
        sections.append({
            'id': section['id'],
            'label': section.get('label') or section['id'],
            'help': section.get('help') or '',
            'stage': section.get('stage') or 'configuration',
            'optional': bool(section.get('optional')),
            'playbook': section.get('playbook'),
            'needs': list(section.get('needs') or []),
            'kind': section.get('kind') or 'rows',
            'lists': lists(section),
        })
    return {'schema_version': version, 'label': raw.get('label') or 'Tenant build', 'sections': sections}


def section(app, section_id):
    for s in load(app)['sections']:
        if s['id'] == section_id:
            return s
    raise CentralError('The design has no section called %s.' % section_id)


def catalogue(app):
    """[{type, label, category, module, named, secret_keys, fields}]"""
    data = _read(os.path.join(build_dir(app), 'profiles', 'index.yml'))
    return list((data or {}).get('profiles') or [])


def profile_type(app, name):
    """One profile type's description, with its `fields`."""
    if not _NAME.match(str(name or '')) or name not in {p['type'] for p in catalogue(app)}:
        raise CentralError('%s is not a profile type.' % str(name)[:64])
    return _read(os.path.join(build_dir(app), 'profiles', '%s.yml' % name))


# ---------------------------------------------------------------------------
# Checking a section's data
# ---------------------------------------------------------------------------

def _empty(value):
    return value is None or value == '' or value == [] or value == {}


def _scalar(field, value, where, problems):
    """value as the field's type, or None with a problem recorded."""
    kind = field.get('type')
    label = field.get('label') or field['name']
    if kind == 'toggle':
        if isinstance(value, bool):
            return value
        problems.append('%s: %s must be on or off.' % (where, label))
        return None
    if kind == 'number':
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
                value = int(value)
            else:
                problems.append('%s: %s must be a whole number.' % (where, label))
                return None
        low, high = field.get('min'), field.get('max')
        if (low is not None and value < low) or (high is not None and value > high):
            problems.append('%s: %s must be between %s and %s.' % (where, label, low, high))
            return None
        return value
    if kind == 'choice' and field.get('choices'):
        if str(value) not in [str(c) for c in field['choices']]:
            problems.append('%s: %s must be one of %s.' % (where, label, ', '.join(map(str, field['choices']))))
            return None
        return next(c for c in field['choices'] if str(c) == str(value))
    if isinstance(value, (dict, list)):
        problems.append('%s: %s must be a single value.' % (where, label))
        return None
    text = str(value)
    limit = field.get('max_length')
    if limit and len(text) > limit:
        problems.append('%s: %s is longer than %s characters.' % (where, label, limit))
        return None
    return text


def _check_config(fields, value, where, problems):
    """A profile's settings (or a group within them) against its fields."""
    if not isinstance(value, dict):
        problems.append('%s must be a set of settings.' % where)
        return {}
    by_name = {f['name']: f for f in fields}
    out = {}
    for name, item in value.items():
        field = by_name.get(name)
        if field is None:
            problems.append('%s has no setting called %s.' % (where, str(name)[:64]))
            continue
        if _empty(item):
            continue
        kind = field.get('type')
        here = '%s › %s' % (where, field.get('label') or name)
        if kind == 'group':
            clean = _check_config(field.get('fields') or [], item, here, problems)
        elif kind == 'rows':
            if not isinstance(item, list):
                problems.append('%s must be a list.' % here)
                continue
            # An entry added and left blank is dropped, not complained about.
            clean = [_check_config(field.get('fields') or [], row, '%s %d' % (here, i + 1), problems)
                     for i, row in enumerate(item) if not _empty(row)]
            clean = [row for row in clean if row]
        elif kind in ('list', 'choices'):
            if not isinstance(item, list):
                problems.append('%s must be a list.' % here)
                continue
            one = dict(field, type='choice' if kind == 'choices' else ('number' if field.get('of') == 'number' else 'text'))
            clean = [_scalar(one, v, where, problems) for v in item if not _empty(v)]
            clean = [v for v in clean if v is not None]
        else:
            clean = _scalar(field, item, where, problems)
        if not _empty(clean):
            out[name] = clean
    for field in fields:
        if field.get('required') and field['name'] not in out:
            problems.append('%s needs %s.' % (where, field.get('label') or field['name']))
    return out


def _hidden(field, row, by_name):
    """A field shown only when another has a given value, and it hasn't."""
    for other, wanted in (field.get('show_if') or {}).items():
        current = row.get(other, (by_name.get(other) or {}).get('default'))
        if current != wanted:
            return True
    return False


def _row_keys(design, var, key):
    return {str(row.get(key)) for row in design.get(var) or [] if isinstance(row, dict) and not _empty(row.get(key))}


def check(app, sect, data, design):
    """Check a section's data: {var: [rows]} for each of its lists.

    design is the rest of the design ({var: rows} across every section),
    which `ref` fields are checked against; this section's own lists are
    taken from data. Returns (clean, problems): clean has unset values
    dropped and numbers as numbers, problems is a list of sentences.
    """
    problems, clean = [], {}
    if not isinstance(data, dict):
        return {}, ['Expected the section\'s lists.']
    known = {part['var'] for part in sect['lists']}
    for var in data:
        if var not in known:
            problems.append('%s is not part of %s.' % (str(var)[:64], sect['label']))
    merged = dict(design or {})
    for part in sect['lists']:
        rows = data.get(part['var'])
        merged[part['var']] = rows if isinstance(rows, list) else []

    for part in sect['lists']:
        rows = data.get(part['var']) or []
        if not isinstance(rows, list):
            problems.append('%s must be a list.' % part['label'])
            continue
        by_name = {f['name']: f for f in part['fields']}
        out, seen = [], set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                problems.append('%s %d is not a row.' % (part['label'], index + 1))
                continue
            name = row.get(part['key']) if part['key'] else row.get('name') or row.get('profile_type')
            where = '%s %s' % (part['label'], name if not _empty(name) else index + 1)
            new = {}
            for key in row:
                if key not in by_name:
                    problems.append('%s has no field called %s.' % (where, str(key)[:64]))
            for field in part['fields']:
                value = row.get(field['name'])
                kind = field.get('type')
                if _empty(value) or _hidden(field, row, by_name):
                    continue
                if kind == 'profile':
                    continue                                    # after profile_type is known
                if kind == 'ref':
                    wanted = value if isinstance(value, list) else [value]
                    target = next((p for s in load(app)['sections'] for p in s['lists']
                                   if p['var'] == field.get('to')), None)
                    exists = _row_keys(merged, field.get('to'), target['key']) if target else set()
                    missing = [str(v) for v in wanted if str(v) not in exists]
                    if missing:
                        problems.append('%s: %s refers to %s, which %s not in the design.' % (
                            where, field.get('label') or field['name'], ', '.join(missing),
                            'is' if len(missing) == 1 else 'are'))
                    new[field['name']] = [str(v) for v in wanted] if field.get('multiple') else str(wanted[0])
                elif kind in ('list', 'choices'):
                    if not isinstance(value, list):
                        problems.append('%s: %s must be a list.' % (where, field.get('label') or field['name']))
                        continue
                    one = dict(field, type='choice' if kind == 'choices' else 'text')
                    items = [_scalar(one, v, where, problems) for v in value if not _empty(v)]
                    items = [v for v in items if v is not None]
                    if items:
                        new[field['name']] = items
                elif field.get('from') == 'catalogue':
                    if str(value) not in {p['type'] for p in catalogue(app)}:
                        problems.append('%s: %s is not a profile type.' % (where, str(value)[:64]))
                    else:
                        new[field['name']] = str(value)
                else:
                    item = _scalar(field, value, where, problems)
                    if item is not None:
                        new[field['name']] = item

            if part['kind'] == 'profiles' and new.get('profile_type'):
                described = profile_type(app, new['profile_type'])
                where = '%s %s' % (described.get('label') or new['profile_type'], new.get('name') or '')
                config = _check_config(described.get('fields') or [], row.get('config') or {}, where.strip(), problems)
                if config:
                    new['config'] = config
                if described.get('named') and _empty(new.get('name')):
                    problems.append('%s needs a profile name.' % where.strip())

            for field in part['fields']:
                if (field.get('required') and field['name'] not in new and 'default' not in field
                        and not _hidden(field, row, by_name) and field.get('type') != 'profile'):
                    problems.append('%s needs %s.' % (where, field.get('label') or field['name']))

            identity = (new.get('profile_type'), new.get('name')) if part['kind'] == 'profiles' \
                else str(new.get(part['key']))
            if identity in seen:
                problems.append('%s is listed twice.' % where)
            seen.add(identity)
            out.append(new)
        clean[part['var']] = out
    return clean, problems


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def _secret_names(part, row, app):
    names = {f['name'] for f in part['fields'] if f.get('type') == 'secret'}
    nested = set()
    if part['kind'] == 'profiles' and row.get('profile_type'):
        entry = next((p for p in catalogue(app) if p['type'] == row['profile_type']), None)
        nested = set((entry or {}).get('secret_keys') or [])
    return names, nested


def _mask_nested(value, names):
    if isinstance(value, dict):
        return {k: (MASK if k in names and isinstance(v, str) and v else _mask_nested(v, names))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_nested(v, names) for v in value]
    return value


def mask(app, sect, data):
    """data with every secret replaced by MASK, for sending to the browser."""
    out = {}
    for part in sect['lists']:
        rows = []
        for row in data.get(part['var']) or []:
            names, nested = _secret_names(part, row, app)
            new = {k: (MASK if k in names and v else v) for k, v in row.items()}
            if nested and isinstance(new.get('config'), dict):
                new['config'] = _mask_nested(new['config'], nested)
            rows.append(new)
        out[part['var']] = rows
    return out


def _unmask_nested(value, old):
    """MASK in value means "as it was": take it from the same place in old."""
    if value == MASK:
        return old if isinstance(old, str) else None
    if isinstance(value, dict):
        old = old if isinstance(old, dict) else {}
        return {k: _unmask_nested(v, old.get(k)) for k, v in value.items()}
    if isinstance(value, list):
        old = old if isinstance(old, list) else []
        return [_unmask_nested(v, old[i] if i < len(old) else None) for i, v in enumerate(value)]
    return value


def unmask(sect, data, saved):
    """data as the browser sent it, with each MASK replaced by the saved
    secret of the same row. A row is matched by its key (type and name for
    a profile), so renaming a row means typing its secrets again."""
    out = {}
    for part in sect['lists']:
        def identity(row):
            if part['kind'] == 'profiles':
                return (row.get('profile_type'), row.get('name'))
            return str(row.get(part['key']))
        before = {identity(r): r for r in (saved or {}).get(part['var']) or [] if isinstance(r, dict)}
        rows = []
        for row in (data or {}).get(part['var']) or []:
            if isinstance(row, dict):
                row = _unmask_nested(row, before.get(identity(row)) or {})
            rows.append(row)
        if isinstance((data or {}).get(part['var']), list):
            out[part['var']] = rows
    for var, value in (data or {}).items():
        out.setdefault(var, value)
    return out
