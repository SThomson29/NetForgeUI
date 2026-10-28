"""
Firmware → Check versions: read each switch's versions over the AOS-CX
REST API and compare them with the chosen image.
"""

import pytest
import requests

from app import firmware_versions as fv
from app.models import User

IMG = {'platform': 'FL', 'version': '10.13.1005'}


def sw(current='FL.10.12.1000', primary='FL.10.12.1000', secondary='FL.10.11.0001',
       booted='primary'):
    return {'current': current, 'primary': primary, 'secondary': secondary,
            'booted': booted, 'platform_name': '6300'}


class TestAssess:

    def test_needs_upgrade(self):
        r = fv.assess(sw(), IMG)
        assert (r['verdict'], r['tick'], r['other_partition']) == ('upgrade', True, 'secondary')
        assert r['note'] == '10.12.1000 → 10.13.1005'

    def test_already_running(self):
        r = fv.assess(sw(current='FL.10.13.1005', primary='FL.10.13.1005'), IMG)
        assert (r['verdict'], r['tick']) == ('current', False)

    def test_staged_on_the_other_partition(self):
        r = fv.assess(sw(secondary='FL.10.13.1005'), IMG)
        assert (r['verdict'], r['tick']) == ('staged', True)
        assert 'secondary but not booted' in r['note']

    def test_staged_the_other_way_round(self):
        r = fv.assess(sw(current='FL.10.12.1000', primary='FL.10.13.1005',
                         secondary='FL.10.12.1000', booted='secondary'), IMG)
        assert (r['verdict'], r['other_partition']) == ('staged', 'primary')

    def test_wrong_platform(self):
        r = fv.assess(sw(current='PL.10.12.1000'), IMG)
        assert (r['verdict'], r['tick']) == ('wrong_platform', False)
        assert 'FL' in r['note'] and 'PL' in r['note']

    def test_image_without_a_platform_still_compares_versions(self):
        r = fv.assess(sw(current='PL.10.13.1005'), {'platform': None, 'version': '10.13.1005'})
        assert r['verdict'] == 'current'

    @pytest.mark.parametrize('image', [None, {'platform': None, 'version': None}])
    def test_unknown_image(self, image):
        r = fv.assess(sw(), image)
        assert (r['verdict'], r['tick']) == ('unknown', None)


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeSwitch:
    """A requests.Session stand-in that answers like an AOS-CX switch."""
    instances = []

    def __init__(self):
        self.calls, self.verify = [], True
        FakeSwitch.instances.append(self)

    login_status = 200
    rest_index = {'latest': {'version': 'v10.13', 'prefix': '/rest/v10.13'}}
    firmware = {'current_version': 'FL.10.12.1000', 'primary_version': 'FL.10.12.1000',
                'secondary_version': 'FL.10.11.0001', 'default_image': 'primary',
                'booted_image': 'primary'}
    raise_on = None

    def _hit(self, method, url, **kw):
        path = url.split('://', 1)[1].split('/', 1)[1]
        self.calls.append((method, '/' + path, kw.get('params')))
        if self.raise_on and self.raise_on[0] in path:
            raise self.raise_on[1]
        if path == 'rest':
            return FakeResponse(200, self.rest_index)
        if path.endswith('/login'):
            return FakeResponse(self.login_status, {})
        if path.endswith('/firmware'):
            return FakeResponse(200, self.firmware)
        if path.endswith('/system'):
            return FakeResponse(200, {'platform_name': '6300'})
        return FakeResponse(200, {})

    def get(self, url, **kw):
        return self._hit('GET', url, **kw)

    def post(self, url, **kw):
        return self._hit('POST', url, **kw)

    def close(self):
        pass


@pytest.fixture
def switch(monkeypatch):
    FakeSwitch.instances = []
    monkeypatch.setattr(requests, 'Session', FakeSwitch)
    for attr in ('login_status', 'rest_index', 'firmware', 'raise_on'):
        monkeypatch.setattr(FakeSwitch, attr, getattr(FakeSwitch, attr))
    return FakeSwitch


class TestReadVersions:

    def test_reads_firmware_with_the_latest_api(self, switch):
        v = fv.read_versions('10.0.0.1', 'admin', 'pw')
        assert v == {'current': 'FL.10.12.1000', 'primary': 'FL.10.12.1000',
                     'secondary': 'FL.10.11.0001', 'booted': 'primary', 'platform_name': '6300'}
        calls = [c[:2] for c in switch.instances[0].calls]
        assert calls == [('GET', '/rest'), ('POST', '/rest/v10.13/login'),
                         ('GET', '/rest/v10.13/firmware'), ('GET', '/rest/v10.13/system'),
                         ('POST', '/rest/v10.13/logout')]
        assert switch.instances[0].verify is False

    def test_falls_back_when_the_index_is_unhelpful(self, switch):
        switch.rest_index = {'v10.04': {}, 'v10.09': {}}
        fv.read_versions('10.0.0.1', 'a', 'b')
        assert ('POST', '/rest/v10.09/login', None) in switch.instances[0].calls

    def test_bad_credentials(self, switch):
        switch.login_status = 401
        with pytest.raises(fv.SwitchError, match='username or password'):
            fv.read_versions('10.0.0.1', 'a', 'b')

    def test_logs_out_even_when_reading_fails(self, switch):
        switch.raise_on = ('/firmware', requests.exceptions.ReadTimeout())
        with pytest.raises(fv.SwitchError, match='timed out'):
            fv.read_versions('10.0.0.1', 'a', 'b')
        assert any(c[1].endswith('/logout') for c in switch.instances[0].calls)

    def test_unreachable(self, switch):
        switch.raise_on = ('rest', requests.exceptions.ConnectionError())
        with pytest.raises(fv.SwitchError, match='REST API'):
            fv.read_versions('10.0.0.1', 'a', 'b')

    def test_ipv6_address(self, switch):
        fv.read_versions('2001:db8::1', 'a', 'b')
        assert switch.instances[0].calls[0][1] == '/rest'

    def test_check_runs_every_switch_and_keeps_failures_apart(self, monkeypatch):
        def fake(ip, u, p):
            if ip == '10.0.0.2':
                raise fv.SwitchError('timed out')
            return sw()
        monkeypatch.setattr(fv, 'read_versions', fake)
        out = fv.check([{'hostname': 'A', 'mgmt_ip': '10.0.0.1'},
                        {'hostname': 'B', 'mgmt_ip': '10.0.0.2'}], 'u', 'p', IMG)
        assert [(r['hostname'], r['ok'], r.get('verdict'), r.get('error')) for r in out] == \
            [('A', True, 'upgrade', None), ('B', False, None, 'timed out')]


class TestRoute:

    @pytest.fixture
    def proj(self, app, auth_client, tmp_path, monkeypatch):
        fw = tmp_path / 'firmware'
        fw.mkdir()
        (fw / 'FL_10_13_1005.swi').write_bytes(b'x')
        app.config['FIRMWARE_DIR'] = str(fw)
        from app.project import create_project
        from app.utils import ensure_workspace
        ensure_workspace(app, 'admin')
        create_project(app, 'admin', 'p')
        import app.firmware_routes as fr
        monkeypatch.setattr(fr, '_load_mapping', lambda a, u, p: {'hosts': [
            {'hostname': 'A', 'mgmt_ip': '10.0.0.1'}, {'hostname': 'B', 'mgmt_ip': ''}]})
        seen = []
        monkeypatch.setattr(fv, 'read_versions', lambda ip, u, pw: seen.append((ip, u, pw)) or sw())
        return seen

    BODY = {'hosts': ['A', 'B', 'nope'], 'switch_username': 'netops',
            'switch_password': 'pw', 'image': 'FL_10_13_1005.swi'}

    def test_checks_mapped_switches_with_an_address(self, auth_client, proj):
        data = auth_client.post('/projects/admin/p/firmware/versions', json=self.BODY).get_json()
        assert data['ok'] and data['image'] == {'platform': 'FL', 'version': '10.13.1005'}
        assert [(r['hostname'], r['verdict']) for r in data['results']] == [('A', 'upgrade')]
        assert proj == [('10.0.0.1', 'netops', 'pw')]

    def test_needs_credentials(self, auth_client, proj):
        res = auth_client.post('/projects/admin/p/firmware/versions',
                               json=dict(self.BODY, switch_password=''))
        assert res.status_code == 400 and proj == []

    def test_unknown_image_reads_versions_without_verdicts(self, auth_client, proj):
        data = auth_client.post('/projects/admin/p/firmware/versions',
                                json=dict(self.BODY, image='nope.swi')).get_json()
        assert data['image'] is None and data['results'][0]['verdict'] == 'unknown'

    def test_needs_deploy_access(self, app, auth_client, proj):
        from app import sharing
        User.create('ed', 'password1', app)
        sharing.set_share(app, 'admin', 'p', 'user', 'ed', sharing.EDIT, 'admin')
        c = app.test_client()
        c.post('/login', data={'username': 'ed', 'password': 'password1'})
        assert c.post('/projects/admin/p/firmware/versions', json=self.BODY).status_code == 403
        assert proj == []

    def test_page_has_the_button(self, auth_client, proj):
        body = auth_client.get('/projects/admin/p/firmware').get_data(as_text=True)
        assert 'onclick="checkVersions()"' in body and 'function checkVersions()' in body
