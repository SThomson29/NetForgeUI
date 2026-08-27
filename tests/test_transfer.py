"""
Unit tests for app/transfer.py — project import / export.

Covers: credential stripping, manifest handling, archive validation,
        round-trip fidelity, and drift between the strip list and the
        NetForge skeleton.
"""

import io
import os
import json
import zipfile

import pytest
import yaml

from app.transfer import (
    export_project, import_project, read_manifest, redact_yaml_text,
    TransferError, SECRET_FIELDS, SCHEMA_VERSION, MANIFEST_NAME,
)
from app.project import create_project, project_dir, project_host_vars_dir


def _make_archive(entries, schema_version=SCHEMA_VERSION, with_manifest=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        if with_manifest:
            zf.writestr(MANIFEST_NAME, json.dumps({
                'schema_version': schema_version,
                'source_project': 'src',
                'hosts': [],
            }))
        for name, content in entries.items():
            zf.writestr(name, content)
    buf.seek(0)
    return buf


@pytest.fixture
def project(app):
    """A project with one host carrying secrets in several files."""
    with app.app_context():
        create_project(app, 'admin', 'src-proj')
        hv = os.path.join(project_host_vars_dir(app, 'admin', 'src-proj'), 'core-01')
        os.makedirs(hv, exist_ok=True)
        with open(os.path.join(hv, 'aaa.yml'), 'w') as f:
            f.write('---\n# keep me\nradius_server_key: "RADSECRET"\n'
                    'radius_group_name: "CORP"\nradius_servers:\n'
                    '  - address: "10.0.0.1"\n    key: "PERSERVER"\n')
        with open(os.path.join(hv, 'snmp.yml'), 'w') as f:
            f.write('---\nsnmp:\n  community: "COMMSECRET"\n  v3_users:\n'
                    '    - username: mon\n      auth_password: "AUTHSEC"\n'
                    '      priv_password: "PRIVSEC"\n')
        with open(os.path.join(hv, 'management.yml'), 'w') as f:
            f.write('---\nlocal_users:\n  - username: admin\n'
                    '    group: administrators\n    password: "ADMINPW"\n')
        with open(os.path.join(hv, 'general.yml'), 'w') as f:
            f.write('---\nhostname: core-01\n')
    return 'src-proj'


class TestRedaction:

    def test_secrets_are_blanked(self):
        out, n = redact_yaml_text('radius_server_key: "S3CR3T"\n')
        assert 'S3CR3T' not in out
        assert out.strip() == 'radius_server_key: ""'
        assert n == 1

    def test_non_secret_values_untouched(self):
        src = 'radius_group_name: "CORP"\nhostname: core-01\n'
        out, n = redact_yaml_text(src)
        assert out == src
        assert n == 0

    def test_comments_and_structure_preserved(self):
        src = '---\n# an important comment\nsnmp:\n  community: "public"\n'
        out, _ = redact_yaml_text(src)
        assert '# an important comment' in out
        assert yaml.safe_load(out)['snmp']['community'] == ''

    def test_list_item_secrets_are_blanked(self):
        src = 'radius_servers:\n  - address: "10.0.0.1"\n    key: "SEC"\n'
        out, n = redact_yaml_text(src)
        assert 'SEC' not in out
        assert yaml.safe_load(out)['radius_servers'][0]['address'] == '10.0.0.1'
        assert n == 1

    def test_commented_out_secret_is_left_alone(self):
        src = '#   key: "example"\n'
        out, n = redact_yaml_text(src)
        assert out == src
        assert n == 0

    def test_output_is_still_valid_yaml(self):
        src = ('radius_server_key: "A"\nsnmp:\n  community: "B"\n'
               '  v3_users:\n    - username: u\n      auth_password: "C"\n')
        out, _ = redact_yaml_text(src)
        parsed = yaml.safe_load(out)
        assert parsed['snmp']['v3_users'][0]['auth_password'] == ''


class TestExport:

    def test_archive_contains_no_secrets(self, app, project):
        with app.app_context():
            data, _ = export_project(app, 'admin', project)
        for secret in ('RADSECRET', 'PERSERVER', 'COMMSECRET',
                       'AUTHSEC', 'PRIVSEC', 'ADMINPW'):
            assert secret.encode() not in data, secret

    def test_manifest_describes_the_export(self, app, project):
        with app.app_context():
            _, manifest = export_project(app, 'admin', project)
        assert manifest['schema_version'] == SCHEMA_VERSION
        assert manifest['source_project'] == project
        assert manifest['hosts'] == ['core-01']
        assert manifest['credentials_stripped'] is True
        assert manifest['fields_redacted'] == 6

    def test_generated_configs_are_excluded(self, app, project):
        with app.app_context():
            gc = os.path.join(project_dir(app, 'admin', project),
                              'generated_configs')
            os.makedirs(gc, exist_ok=True)
            with open(os.path.join(gc, 'core-01_FULL.ios'), 'w') as f:
                f.write('! config\n')
            data, _ = export_project(app, 'admin', project)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert not any('generated_configs' in n for n in names)

    def test_deploy_mapping_is_excluded(self, app, project):
        with app.app_context():
            with open(os.path.join(project_dir(app, 'admin', project),
                                   'deploy_mapping.yml'), 'w') as f:
                f.write('hosts: []\n')
            data, _ = export_project(app, 'admin', project)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert not any('deploy_mapping' in n for n in names)

    def test_ospf_auth_key_is_stripped(self, app):
        """The flat name must be caught — 'auth_key' alone would not match."""
        with app.app_context():
            create_project(app, 'admin', 'ospf-proj')
            hv = os.path.join(project_host_vars_dir(app, 'admin', 'ospf-proj'),
                              'core-01')
            os.makedirs(hv, exist_ok=True)
            with open(os.path.join(hv, 'interfaces.yml'), 'w') as f:
                f.write('physical_interfaces:\n  - name: "1/1/1"\n'
                        '    ospf_area: "0.0.0.0"\n'
                        '    ospf_auth_key: "OSPFSECRET"\n')
            data, manifest = export_project(app, 'admin', 'ospf-proj')
        assert b'OSPFSECRET' not in data
        assert manifest['fields_redacted'] == 1

    def test_missing_project_raises(self, app):
        with app.app_context():
            with pytest.raises(TransferError):
                export_project(app, 'admin', 'no-such-project')


class TestImportValidation:

    @pytest.mark.parametrize('entry', [
        '../../../etc/passwd',
        '/etc/passwd',
        'host_vars/../../escape.yml',
        'evil.sh',
        'host_vars/sw1/evil.sh',
    ])
    def test_unexpected_entries_are_rejected(self, app, entry):
        with app.app_context():
            with pytest.raises(TransferError):
                import_project(app, 'admin', 'dest', _make_archive({entry: 'x'}))

    def test_missing_manifest_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(TransferError):
                read_manifest(_make_archive({'config.json': '{}'},
                                            with_manifest=False))

    def test_newer_schema_version_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(TransferError) as e:
                read_manifest(_make_archive({}, schema_version=SCHEMA_VERSION + 1))
        assert 'newer version' in str(e.value)

    def test_non_zip_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(TransferError):
                read_manifest(io.BytesIO(b'definitely not a zip'))

    def test_malformed_json_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(TransferError):
                import_project(app, 'admin', 'dest',
                               _make_archive({'config.json': '{nope'}))

    def test_malformed_yaml_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(TransferError):
                import_project(app, 'admin', 'dest',
                               _make_archive({'host_vars/sw1/a.yml': 'x: [un'}))

    def test_invalid_target_name_is_rejected(self, app):
        with app.app_context():
            with pytest.raises(TransferError):
                import_project(app, 'admin', '../escape', _make_archive({}))

    def test_nothing_written_when_payload_invalid(self, app):
        """A bad archive must not leave a half-built project behind."""
        with app.app_context():
            with pytest.raises(TransferError):
                import_project(app, 'admin', 'half-built',
                               _make_archive({'config.json': '{bad'}))
            assert not os.path.isdir(project_dir(app, 'admin', 'half-built'))


class TestRoundTrip:

    def test_export_then_import_recreates_project(self, app, project):
        with app.app_context():
            data, _ = export_project(app, 'admin', project)
            import_project(app, 'admin', 'dest-proj', io.BytesIO(data))
            hv = os.path.join(project_host_vars_dir(app, 'admin', 'dest-proj'),
                              'core-01')
            assert sorted(os.listdir(hv)) == [
                'aaa.yml', 'general.yml', 'management.yml', 'snmp.yml']
            with open(os.path.join(hv, 'general.yml')) as f:
                assert 'core-01' in f.read()

    def test_imported_secrets_are_blank(self, app, project):
        with app.app_context():
            data, _ = export_project(app, 'admin', project)
            import_project(app, 'admin', 'dest-proj', io.BytesIO(data))
            hv = os.path.join(project_host_vars_dir(app, 'admin', 'dest-proj'),
                              'core-01')
            with open(os.path.join(hv, 'aaa.yml')) as f:
                parsed = yaml.safe_load(f)
        assert parsed['radius_server_key'] == ''
        assert parsed['radius_group_name'] == 'CORP'

    def test_config_name_matches_new_project(self, app, project):
        with app.app_context():
            data, _ = export_project(app, 'admin', project)
            import_project(app, 'admin', 'renamed-proj', io.BytesIO(data))
            with open(os.path.join(project_dir(app, 'admin', 'renamed-proj'),
                                   'config.json')) as f:
                cfg = json.load(f)
        assert cfg['name'] == 'renamed-proj'

    def test_existing_project_is_not_overwritten(self, app, project):
        with app.app_context():
            data, _ = export_project(app, 'admin', project)
            with pytest.raises(TransferError):
                import_project(app, 'admin', project, io.BytesIO(data))


class TestSecretFieldDrift:

    # Editor password inputs -> the YAML key the writer emits for them.
    # Adding a password field to the editor without adding it here fails the
    # test below, which is the point: the new field has to be classified
    # before it can ship.
    EDITOR_SECRET_BINDINGS = {
        'password':          'password',            # local user
        'snmpCommunity':     'community',
        'auth_password':     'auth_password',       # snmpv3
        'priv_password':     'priv_password',       # snmpv3
        'radiusServerKey':   'radius_server_key',
        'key':               'key',                 # per-radius-server
        'ospf_auth_key':     'ospf_auth_key',
        'bulkOspfAuthKey':   'ospf_auth_key',   # bulk routed-link creation
    }

    def test_editor_password_fields_are_all_stripped(self):
        """Every password input in the editor must map to a stripped field.

        The skeleton scan below only runs when NetForge is checked out; this
        one always runs, so it is the guard that actually protects CI. A new
        password-type field whose YAML key is missing from SECRET_FIELDS
        would otherwise be exported in plain text.
        """
        import re
        editor = os.path.join(os.path.dirname(__file__), '..', 'app',
                              'templates', 'project_editor.html')
        with open(editor) as f:
            src = f.read()

        bound = set()
        for m in re.finditer(r"type:'password',\s*value:([A-Za-z0-9_.]+)", src):
            bound.add(m.group(1).split('.')[-1])

        assert bound, 'No password fields found — has the editor changed shape?'

        unmapped = bound - set(self.EDITOR_SECRET_BINDINGS)
        assert not unmapped, (
            'Editor password fields with no known YAML key: %s. Add them to '
            'EDITOR_SECRET_BINDINGS and to SECRET_FIELDS.' % sorted(unmapped))

        leaking = {
            self.EDITOR_SECRET_BINDINGS[b] for b in bound
            if self.EDITOR_SECRET_BINDINGS[b] not in SECRET_FIELDS
        }
        assert not leaking, (
            'Editor password fields not covered by SECRET_FIELDS: %s'
            % sorted(leaking))

    def test_skeleton_has_no_unlisted_secret_fields(self):
        """Guard SECRET_FIELDS against new secret-bearing skeleton fields.

        If NetForge gains a field whose name suggests a credential and it is
        not in SECRET_FIELDS, exports would leak it silently. Fail here
        instead.
        """
        import re
        skeleton = os.environ.get(
            'NETFORGE_SKELETON_DIR',
            os.path.join(os.path.dirname(__file__), '..', 'configgen',
                         'inventory', 'skeleton', 'aoscx'))
        if not os.path.isdir(skeleton):
            pytest.skip('NetForge skeleton not available')

        suspicious = re.compile(r'(password|passwd|secret|community|_key|^key)$',
                                re.I)
        found = set()
        for fname in os.listdir(skeleton):
            if not fname.endswith('.yml'):
                continue
            with open(os.path.join(skeleton, fname)) as f:
                for line in f:
                    line = line.lstrip().lstrip('#').lstrip()
                    m = re.match(r'^(?:-\s*)?([A-Za-z0-9_]+)\s*:', line)
                    if m and suspicious.search(m.group(1)):
                        found.add(m.group(1))

        unlisted = found - SECRET_FIELDS
        assert not unlisted, (
            'Skeleton fields look like credentials but are not in '
            'SECRET_FIELDS: %s' % sorted(unlisted))


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------

class TestTransferRoutes:

    def test_export_downloads_an_archive(self, app, auth_client, project):
        res = auth_client.get('/projects/%s/export' % project)
        assert res.status_code == 200
        assert res.headers['Content-Type'] == 'application/zip'
        assert 'src-proj.netforge.zip' in res.headers['Content-Disposition']
        assert b'RADSECRET' not in res.data

    def test_preview_returns_manifest(self, app, auth_client, project):
        data = auth_client.get('/projects/%s/export' % project).data
        res = auth_client.post(
            '/projects/import/preview',
            data={'archive': (io.BytesIO(data), 'p.zip')},
            content_type='multipart/form-data')
        body = res.get_json()
        assert body['ok'] is True
        assert body['manifest']['source_project'] == 'src-proj'
        assert body['manifest']['credentials_stripped'] is True

    def test_preview_rejects_rubbish(self, app, auth_client):
        res = auth_client.post(
            '/projects/import/preview',
            data={'archive': (io.BytesIO(b'not a zip'), 'p.zip')},
            content_type='multipart/form-data')
        assert res.status_code == 400
        assert res.get_json()['ok'] is False

    def test_import_creates_project(self, app, auth_client, project):
        data = auth_client.get('/projects/%s/export' % project).data
        res = auth_client.post(
            '/projects/import',
            data={'archive': (io.BytesIO(data), 'p.zip'),
                  'project_name': 'brought-in'},
            content_type='multipart/form-data')
        assert res.get_json()['ok'] is True
        listing = auth_client.get('/projects')
        assert b'brought-in' in listing.data

    def test_import_rejects_duplicate_name(self, app, auth_client, project):
        data = auth_client.get('/projects/%s/export' % project).data
        res = auth_client.post(
            '/projects/import',
            data={'archive': (io.BytesIO(data), 'p.zip'),
                  'project_name': project},
            content_type='multipart/form-data')
        assert res.status_code == 400
        assert 'already exists' in res.get_json()['error']

    def test_export_requires_login(self, client, project):
        res = client.get('/projects/%s/export' % project,
                         follow_redirects=False)
        assert res.status_code == 401

    def test_import_requires_login(self, client):
        res = client.post('/projects/import', follow_redirects=False)
        assert res.status_code == 401
