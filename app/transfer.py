"""
Project import / export.

A project is just JSON metadata plus a tree of host_vars YAML, so it can be
moved between NetForgeUI instances as a zip archive. Typical use is exporting
from a central instance and importing onto a laptop that can reach the kit.

Archive layout:

    manifest.json          what this archive is (see SCHEMA_VERSION)
    config.json            pools, common infrastructure, conventions
    allocations.json       IP allocations
    hosts.ini              inventory and stacking groups
    host_vars/<host>/*.yml the variables themselves

Deliberately NOT included:
  - generated_configs/  — re-derivable by running Generate
  - deploy_mapping.yml  — management IPs of live kit

Credentials are stripped on export. See SECRET_FIELDS.
"""

import io
import os
import json
import re
import zipfile
from datetime import datetime, timezone

import yaml


# Bump when the archive layout changes in a way older importers can't handle.
SCHEMA_VERSION = 1

MANIFEST_NAME = 'manifest.json'

# Top-level files carried in the archive.
PROJECT_FILES = ['config.json', 'allocations.json', 'hosts.ini']

# YAML keys whose values are removed on export.
#
# Matched by key name anywhere in the tree, not by file path, so a secret that
# moves between files is still caught. Blanking a field that happens to share
# one of these names is harmless — the importer repopulates them by hand.
#
# tests/test_transfer.py::test_skeleton_has_no_unlisted_secret_fields guards
# this list against new secret-bearing fields appearing in the NetForge
# skeleton.
SECRET_FIELDS = {
    'password',
    'auth_password',
    'priv_password',
    'community',
    'key',
    'radius_server_key',
    'auth_key',
    'secret',
}

# Only these paths are accepted from an uploaded archive. Anything else —
# absolute paths, traversal, unexpected files — is rejected rather than
# sanitised, so a malformed archive fails loudly instead of partially applying.
_NAME_RE = r'[A-Za-z0-9_.\-]+'
_ALLOWED_PATTERNS = [
    re.compile(r'^manifest\.json$'),
    re.compile(r'^config\.json$'),
    re.compile(r'^allocations\.json$'),
    re.compile(r'^hosts\.ini$'),
    re.compile(r'^host_vars/' + _NAME_RE + r'/' + _NAME_RE + r'\.yml$'),
]

# Guards against a zip bomb: refuse archives whose uncompressed size is
# implausible for a project.
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024


class TransferError(Exception):
    """Raised for any archive that cannot be safely imported."""


# ---------------------------------------------------------------------------
# Credential stripping
# ---------------------------------------------------------------------------

def redact_yaml_text(text):
    """Blank the value of any SECRET_FIELDS key in a YAML document.

    Works line by line rather than load-then-dump so comments, ordering and
    formatting survive — scaffolded host_vars files carry the skeleton's
    documentation and it would be lost by a round trip.

    Returns (redacted_text, count_of_fields_blanked).
    """
    keys = '|'.join(sorted(SECRET_FIELDS, key=len, reverse=True))
    # key: value  /  - key: value   — captures indent, optional dash, key
    pattern = re.compile(r'^(\s*-?\s*(?:' + keys + r')\s*:\s*)(\S.*)$')

    out, count = [], 0
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('#'):
            out.append(line)
            continue
        m = pattern.match(line)
        if m and m.group(2).strip() not in ('""', "''", '[]', '{}', '|', '>'):
            out.append(m.group(1) + '""')
            count += 1
        else:
            out.append(line)

    result = '\n'.join(out)
    if text.endswith('\n'):
        result += '\n'
    return result, count


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_project(app, username, project_name):
    """Pack a project into a zip archive. Returns (bytes, manifest_dict)."""
    from .project import project_dir, project_host_vars_dir

    pdir = project_dir(app, username, project_name)
    if not os.path.isdir(pdir):
        raise TransferError('Project %s does not exist.' % project_name)

    hvdir = project_host_vars_dir(app, username, project_name)
    hosts = sorted(
        h for h in os.listdir(hvdir) if os.path.isdir(os.path.join(hvdir, h))
    ) if os.path.isdir(hvdir) else []

    buf = io.BytesIO()
    redacted_total = 0

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in PROJECT_FILES:
            path = os.path.join(pdir, fname)
            if os.path.isfile(path):
                with open(path) as f:
                    zf.writestr(fname, f.read())

        for host in hosts:
            host_path = os.path.join(hvdir, host)
            for fname in sorted(os.listdir(host_path)):
                if not fname.endswith('.yml'):
                    continue
                with open(os.path.join(host_path, fname)) as f:
                    text = f.read()
                text, n = redact_yaml_text(text)
                redacted_total += n
                zf.writestr('host_vars/%s/%s' % (host, fname), text)

        manifest = {
            'schema_version': SCHEMA_VERSION,
            'exported': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source_project': project_name,
            'hosts': hosts,
            'credentials_stripped': True,
            'fields_redacted': redacted_total,
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))

    return buf.getvalue(), manifest


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _open_archive(fileobj):
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile:
        raise TransferError('That file is not a valid zip archive.')

    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise TransferError('Archive is too large to be a project export.')
        if not any(p.match(info.filename) for p in _ALLOWED_PATTERNS):
            raise TransferError(
                'Archive contains an unexpected entry: %s' % info.filename
            )
    return zf


def read_manifest(fileobj):
    """Read and validate just the manifest, without extracting anything."""
    zf = _open_archive(fileobj)
    try:
        raw = zf.read(MANIFEST_NAME)
    except KeyError:
        raise TransferError(
            'No manifest.json — this does not look like a NetForgeUI export.'
        )
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise TransferError('manifest.json is not valid JSON.')

    version = manifest.get('schema_version')
    if not isinstance(version, int):
        raise TransferError('manifest.json has no usable schema_version.')
    if version > SCHEMA_VERSION:
        raise TransferError(
            'This export was created by a newer version of NetForgeUI '
            '(format v%s, this instance understands v%s).'
            % (version, SCHEMA_VERSION)
        )
    return manifest


def import_project(app, username, target_name, fileobj):
    """Unpack an archive into a new project. Returns the manifest.

    Refuses to write over an existing project — the caller supplies the target
    name, so a collision is the user's to resolve.
    """
    from .project import project_dir

    if not re.match(r'^[A-Za-z0-9_\-]+$', target_name or ''):
        raise TransferError(
            'Project name may only contain letters, numbers, hyphens and '
            'underscores.'
        )

    manifest = read_manifest(fileobj)
    zf = _open_archive(fileobj)

    pdir = project_dir(app, username, target_name)
    if os.path.isdir(pdir):
        raise TransferError('Project %s already exists.' % target_name)

    # Validate payload before writing anything, so a bad archive cannot leave
    # a half-built project behind.
    payload = {}
    for info in zf.infolist():
        if info.is_dir() or info.filename == MANIFEST_NAME:
            continue
        data = zf.read(info.filename)
        if info.filename.endswith('.json'):
            try:
                json.loads(data)
            except ValueError:
                raise TransferError('%s is not valid JSON.' % info.filename)
        elif info.filename.endswith('.yml'):
            try:
                yaml.safe_load(data)
            except yaml.YAMLError:
                raise TransferError('%s is not valid YAML.' % info.filename)
        payload[info.filename] = data

    os.makedirs(os.path.join(pdir, 'host_vars'), exist_ok=True)
    os.makedirs(os.path.join(pdir, 'generated_configs'), exist_ok=True)

    for name, data in payload.items():
        dest = os.path.join(pdir, *name.split('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(data)

    # The project's own record of its name should match where it now lives.
    cfg_path = os.path.join(pdir, 'config.json')
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            cfg['name'] = target_name
            with open(cfg_path, 'w') as f:
                json.dump(cfg, f, indent=2)
        except (ValueError, OSError):
            pass

    return manifest
