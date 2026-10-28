"""
Static assets: React is served from the app (so the editor works with no
internet), the editor script lives in static/js, and every asset URL
carries a content fingerprint so upgrades never run stale scripts.
"""

import glob
import hashlib
import os
import re

import pytest

from app import assets
from app.project import create_project
from app.utils import add_project_hosts, ensure_workspace

STATIC = assets.STATIC_DIR
TEMPLATES = os.path.join(os.path.dirname(STATIC), 'templates')

# The published react@18.2.0 / react-dom@18.2.0 UMD builds, as on npm.
VENDOR_SHA256 = {
    'vendor/react-18.2.0.production.min.js':
        '4b4969fa4ef3594324da2c6d78ce8766fbbc2fd121fff395aedf997db0a99a06',
    'vendor/react-dom-18.2.0.production.min.js':
        '21758ed084cd0e37e735722ee4f3957ea960628a29dfa6c3ce1a1d47a2d6e4f7',
}


@pytest.fixture
def editor_project(app, auth_client, skeleton_dir):
    ensure_workspace(app, 'admin')
    create_project(app, 'admin', 'p')
    add_project_hosts(app, 'admin', 'p', ['SW1'], 'none')
    return app


class TestVendoredReact:

    @pytest.mark.parametrize('name, digest', sorted(VENDOR_SHA256.items()))
    def test_files_are_the_published_builds(self, name, digest):
        with open(os.path.join(STATIC, name), 'rb') as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest

    def test_no_template_loads_scripts_from_the_internet(self):
        offenders = []
        for path in glob.glob(os.path.join(TEMPLATES, '*.html')):
            with open(path) as f:
                for m in re.finditer(r'<script[^>]+src="(https?:)?//[^"]+"', f.read()):
                    offenders.append('%s: %s' % (os.path.basename(path), m.group(0)))
        assert offenders == []


class TestEditorPage:

    def test_loads_local_react_and_editor_script(self, editor_project, auth_client):
        body = auth_client.get('/projects/admin/p/editor?host=SW1').get_data(as_text=True)
        srcs = re.findall(r'<script src="([^"]+)"', body)
        assert [s.split('?')[0] for s in srcs] == [
            '/static/vendor/react-18.2.0.production.min.js',
            '/static/vendor/react-dom-18.2.0.production.min.js',
            '/static/js/editor.js',
        ]
        assert all(re.search(r'\?v=[0-9a-f]{10}$', s) for s in srcs)

    def test_data_is_defined_before_the_script_that_reads_it(self, editor_project, auth_client):
        body = auth_client.get('/projects/admin/p/editor?host=SW1').get_data(as_text=True)
        assert body.index('const SELECTED_HOST') < body.index('/static/js/editor.js')
        assert 'const SELECTED_HOST   = "SW1";' in body

    def test_no_react_until_a_switch_is_chosen(self, editor_project, auth_client):
        body = auth_client.get('/projects/admin/p/editor').get_data(as_text=True)
        assert 'react' not in body.lower().split('</head>')[1].split('function selectHost')[0]
        assert '/static/js/editor.js' not in body and 'vendor/react' not in body
        assert 'function selectHost' in body

    def test_editor_script_is_plain_javascript(self):
        with open(os.path.join(STATIC, 'js', 'editor.js')) as f:
            src = f.read().split('*/', 1)[1]       # after the file header
        # Never rendered by Jinja: no template tags beyond the comment that
        # quotes the NetForge Ansible template.
        assert re.findall(r'\{\{|\{%', src) == ['{{']
        assert "'lag {{ lag_member }}'" in src
        assert 'ReactDOM.createRoot' in src


class TestFingerprint:

    def test_changes_with_the_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(assets, 'STATIC_DIR', str(tmp_path))
        monkeypatch.setattr(assets, '_cache', {})
        f = tmp_path / 'a.js'
        f.write_text('one')
        first = assets.fingerprint('a.js')
        f.write_text('two')
        os.utime(f, ns=(1, 1))        # a different mtime, as a real edit has
        assert assets.fingerprint('a.js') != first
        assert re.fullmatch(r'[0-9a-f]{10}', first)

    def test_missing_file_has_no_fingerprint(self, app):
        assert assets.fingerprint('nope.js') is None
        with app.test_request_context():
            assert assets.static_url('nope.js') == '/static/nope.js'
