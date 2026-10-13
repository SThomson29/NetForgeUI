"""
Ansible templates variable values: a string containing {{ ... }} is
evaluated when the variable is used, and lookup plugins can run commands on
the controller from there. Deploy and firmware run ansible-playbook inside
the NetForgeUI container — which mounts the Docker socket — using
credentials, addresses and names taken from the request.

So every string written for those playbooks is tagged !unsafe, and the host
names that become inventory keys and --limit patterns are validated.
"""

import io
import os
import shutil
import subprocess
import textwrap

import pytest
import yaml

from app.ansible_safe import dump_unsafe, validate_targets


class TestDumpUnsafe:

    def _dump(self, data):
        buf = io.StringIO()
        dump_unsafe(data, buf)
        return buf.getvalue()

    def test_string_values_are_tagged(self):
        out = self._dump({'ansible_password': '{{ 7 * 7 }}'})
        assert "ansible_password: !unsafe '{{ 7 * 7 }}'" in out

    def test_nested_values_are_tagged(self):
        out = self._dump({'all': {'hosts': {'sw1': {'ansible_host': '10.0.0.1'}}}})
        assert "ansible_host: !unsafe '10.0.0.1'" in out

    def test_lists_are_tagged(self):
        out = self._dump({'servers': ['a', '{{ x }}']})
        assert "- !unsafe '{{ x }}'" in out

    def test_keys_stay_plain(self):
        """Keys are not templated, and tagging them would rename the host."""
        out = self._dump({'all': {'hosts': {'core-01': {}}}})
        assert 'core-01:' in out
        assert "!unsafe 'core-01'" not in out

    def test_non_strings_are_untouched(self):
        out = self._dump({'port': 22, 'become': True, 'nothing': None})
        assert 'port: 22' in out
        assert 'become: true' in out
        assert '!unsafe' not in out

    def test_quotes_inside_values_survive(self):
        value = "it's {{ 'quoted' }}"
        out = self._dump({'v': value})

        class _Loader(yaml.SafeLoader):
            pass
        _Loader.add_constructor('!unsafe',
                                lambda loader, node: loader.construct_scalar(node))
        assert yaml.load(out, Loader=_Loader)['v'] == value

    @pytest.mark.skipif(shutil.which('ansible-playbook') is None,
                        reason='ansible-playbook not installed here')
    def test_real_ansible_does_not_evaluate_it(self, tmp_path):
        """The property that matters, checked against Ansible itself."""
        vars_file = tmp_path / 'vars.yml'
        with open(vars_file, 'w') as f:
            dump_unsafe({'probe': '{{ 7 * 7 }}'}, f)
        play = tmp_path / 'play.yml'
        play.write_text(textwrap.dedent("""\
            - hosts: localhost
              gather_facts: false
              connection: local
              tasks:
                - debug: msg="value [{{ probe }}]"
            """))
        res = subprocess.run(
            ['ansible-playbook', '-i', 'localhost,', str(play),
             '-e', '@' + str(vars_file)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, 'ANSIBLE_NOCOLOR': '1'})
        assert 'value [{{ 7 * 7 }}]' in res.stdout, res.stdout + res.stderr
        assert 'value [49]' not in res.stdout


class TestValidateTargets:

    def test_ordinary_hosts_pass(self):
        assert validate_targets([
            {'hostname': 'CTY-01-Core-01', 'mgmt_ip': '10.1.100.1'},
            {'hostname': 'core_02', 'mgmt_ip': 'core-02.mgmt.local'},
            {'hostname': 'v6', 'mgmt_ip': 'fd00::1'},
        ]) == []

    @pytest.mark.parametrize('name', [
        'a,b',          # would add a host to --limit
        'all:!x',       # --limit pattern syntax
        '{{ x }}',
        '../etc',
        'has space',
        '',
    ])
    def test_unsafe_hostnames_are_rejected(self, name):
        assert validate_targets([{'hostname': name, 'mgmt_ip': '10.0.0.1'}])

    def test_unsafe_address_is_rejected(self):
        assert validate_targets([{'hostname': 'sw1', 'mgmt_ip': '{{ lookup() }}'}])

    def test_blank_address_is_allowed(self):
        """Some flows filter on it later; blank is not dangerous."""
        assert validate_targets([{'hostname': 'sw1', 'mgmt_ip': ''}]) == []

    def test_not_a_list(self):
        assert validate_targets('sw1')


class TestRoutes:

    def _deploy_project(self, app):
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'dep')
        return 'dep'

    def test_deploy_refuses_a_crafted_hostname(self, app, auth_client):
        proj = self._deploy_project(app)
        res = auth_client.post('/projects/%s/deploy/dryrun' % proj, json={
            'hosts': [{'hostname': 'sw1,all', 'mgmt_ip': '10.0.0.1'}],
            'username': 'admin', 'password': 'pw'})
        assert res.status_code == 400
        assert 'invalid hostname' in res.get_json()['error']

    def test_mapping_save_refuses_a_crafted_hostname(self, app, auth_client):
        proj = self._deploy_project(app)
        res = auth_client.post('/projects/%s/deployment-ips/save' % proj, json={
            'mappings': [{'hostname': '{{ x }}', 'mgmt_ip': '10.0.0.1'}]})
        assert res.status_code == 400

    def test_deploy_writes_tagged_inputs(self, app, auth_client, monkeypatch):
        """Capture what is actually handed to ansible-playbook."""
        import app.deploy_routes as dr
        seen = {}

        def capture(job_id, playbook_name, repo_dir, ansible_bin,
                    inventory_dict, extra_vars, limit_hosts=None):
            buf = io.StringIO()
            dump_unsafe(inventory_dict, buf)
            seen['inventory'] = buf.getvalue()
            with dr._deploy_jobs_lock:
                dr._deploy_jobs[job_id].update(status='done', returncode=0)

        monkeypatch.setattr(dr, '_run_playbook_job', capture)
        proj = self._deploy_project(app)
        res = auth_client.post('/projects/%s/deploy/dryrun' % proj, json={
            'hosts': [{'hostname': 'sw1', 'mgmt_ip': '10.0.0.1'}],
            'username': 'admin', 'password': '{{ 7 * 7 }}'})
        assert res.status_code == 200
        import time
        for _ in range(50):
            if 'inventory' in seen:
                break
            time.sleep(0.02)
        assert "ansible_password: !unsafe '{{ 7 * 7 }}'" in seen['inventory']

    def test_deploy_job_runner_uses_the_unsafe_dumper(self):
        """The writer in the job runner itself, not just the helper."""
        import inspect
        import app.deploy_routes as dr
        import app.firmware_routes as fr
        for fn in (dr._run_playbook_job, dr._run_playbook,
                   fr._run_firmware_playbook):
            src = inspect.getsource(fn)
            assert 'dump_unsafe(' in src, fn.__name__
            assert 'yaml.dump(' not in src, fn.__name__
