"""
Bulk host add: names by line or comma, numbered runs in brackets, one
stacking type per batch, preview before adding, bad names skipped.
"""

import os

import pytest

from app.bulk_hosts import expand, plan, BulkError, MAX_HOSTS


class TestExpand:

    def test_lines_and_commas(self):
        assert expand(' core-01, core-02\n\nedge-01 ,\n') == ['core-01', 'core-02', 'edge-01']

    def test_zero_padding_is_kept(self):
        assert expand('EDGE-SW-[01-03]') == ['EDGE-SW-01', 'EDGE-SW-02', 'EDGE-SW-03']
        assert expand('SW[098-100]') == ['SW098', 'SW099', 'SW100']

    def test_unpadded_range(self):
        assert expand('SW[8-10]') == ['SW8', 'SW9', 'SW10']

    def test_ranges_multiply_grouped_by_the_left(self):
        assert expand('B1-FL[1-2]-SW[01-03]') == [
            'B1-FL1-SW01', 'B1-FL1-SW02', 'B1-FL1-SW03',
            'B1-FL2-SW01', 'B1-FL2-SW02', 'B1-FL2-SW03']

    def test_two_hundred_stack_site(self):
        """The motivating case: floor in the middle, stack number at the end."""
        names = expand('SITE-FL[1-5]-SW[001-200]')
        assert len(names) == 1000
        assert names[0] == 'SITE-FL1-SW001' and names[-1] == 'SITE-FL5-SW200'

    def test_mixed_with_plain_names(self):
        assert expand('core-01\nacc-[1-2]') == ['core-01', 'acc-1', 'acc-2']

    def test_single_value_range(self):
        assert expand('SW[05-05]') == ['SW05']

    @pytest.mark.parametrize('text', ['SW[10-1]', 'SW[1-]', 'SW[a-c]', 'SW]1', 'SW[1-2'])
    def test_malformed_ranges(self, text):
        with pytest.raises(BulkError):
            expand(text)

    def test_one_entry_over_the_cap(self):
        with pytest.raises(BulkError):
            expand('SW[1-%d]' % (MAX_HOSTS + 1))

    def test_huge_single_range_is_refused_quickly(self):
        import time
        t0 = time.monotonic()
        with pytest.raises(BulkError):
            expand('SW[1-999999999]')
        assert time.monotonic() - t0 < 0.5

    def test_huge_product_is_refused_without_building_it(self):
        with pytest.raises(BulkError):
            expand('A[1-1000]-B[1-1000]-C[1-1000]')

    def test_batch_over_the_cap(self):
        with pytest.raises(BulkError):
            expand('A[1-600]\nB[1-600]')


class TestPlan:

    def test_reasons(self):
        items = plan('ok-1, Core-01, ok-1, bad name, ' + 'x' * 65, existing=['core-01'])
        reasons = [(i['name'][:10], i['reason']) for i in items]
        assert reasons == [
            ('ok-1', None),
            ('Core-01', 'already in the project'),
            ('ok-1', 'repeated in this list'),
            ('bad name', 'invalid: letters, numbers, - and _ only, up to 64'),
            ('x' * 10, 'invalid: letters, numbers, - and _ only, up to 64'),
        ]
        assert [i['add'] for i in items] == [True, False, False, False, False]


@pytest.fixture
def proj(app_with_skeleton, auth_client):
    from app.project import create_project
    with app_with_skeleton.app_context():
        create_project(app_with_skeleton, 'admin', 'bulk')
    return app_with_skeleton


def _hosts(app):
    from app.utils import read_project_hosts
    return {h['hostname']: h['group'] for h in read_project_hosts(app, 'admin', 'bulk')}


class TestRoutes:

    def test_preview_changes_nothing(self, proj, auth_client):
        res = auth_client.post('/projects/bulk/hosts/bulk/preview',
                               json={'text': 'EDGE-[01-03]', 'stacking': 'none'})
        data = res.get_json()
        assert data['ok'] and data['add_count'] == 3
        assert _hosts(proj) == {}

    def test_add_with_one_stacking_type(self, proj, auth_client):
        from app.project import project_host_vars_dir
        res = auth_client.post('/projects/bulk/hosts/bulk',
                               json={'text': 'ACC-[1-3]', 'stacking': 'vsf'})
        data = res.get_json()
        assert data['added'] == ['ACC-1', 'ACC-2', 'ACC-3'] and data['skipped'] == []
        assert _hosts(proj) == {'ACC-1': 'cx_vsf', 'ACC-2': 'cx_vsf', 'ACC-3': 'cx_vsf'}
        hv = project_host_vars_dir(proj, 'admin', 'bulk')
        for n in ('ACC-1', 'ACC-2', 'ACC-3'):
            files = os.listdir(os.path.join(hv, n))
            assert 'vsf.yml' in files and 'vsx.yml' not in files

    def test_bad_and_existing_names_are_skipped(self, proj, auth_client):
        auth_client.post('/projects/bulk/hosts/bulk', json={'text': 'core-01'})
        data = auth_client.post('/projects/bulk/hosts/bulk', json={
            'text': 'CORE-01, core-02, core-02, bad!name'}).get_json()
        assert data['added'] == ['core-02']
        assert {s['name']: s['reason'] for s in data['skipped']} == {
            'CORE-01': 'already in the project',
            'core-02': 'repeated in this list',
            'bad!name': 'invalid: letters, numbers, - and _ only, up to 64'}
        assert set(_hosts(proj)) == {'core-01', 'core-02'}

    def test_add_uses_the_text_not_a_client_list(self, proj, auth_client):
        auth_client.post('/projects/bulk/hosts/bulk', json={
            'text': 'sw-1', 'names': ['../../evil'], 'items': [{'name': 'evil', 'add': True}]})
        assert set(_hosts(proj)) == {'sw-1'}

    @pytest.mark.parametrize('body', [
        {'text': 'a', 'stacking': 'stack-of-plates'},
        {'stacking': 'none'},
        {'text': 'SW[1-5000]'},
        {'text': 'SW[5-1]'},
    ])
    def test_rejected_batches_add_nothing(self, proj, auth_client, body):
        for url in ('/projects/bulk/hosts/bulk/preview', '/projects/bulk/hosts/bulk'):
            res = auth_client.post(url, json=body)
            assert res.status_code == 400
            assert res.get_json()['error']
        assert _hosts(proj) == {}

    def test_hosts_ini_stays_valid_after_a_big_batch(self, proj, auth_client):
        auth_client.post('/projects/bulk/hosts/bulk', json={'text': 'core-[1-2]', 'stacking': 'vsx'})
        auth_client.post('/projects/bulk/hosts/bulk', json={'text': 'E-FL[1-3]-SW[01-40]'})
        hosts = _hosts(proj)
        assert len(hosts) == 122
        assert hosts['core-1'] == 'cx_vsx' and hosts['E-FL3-SW40'] == 'cx'

    def test_requires_login(self, client):
        assert client.post('/projects/bulk/hosts/bulk', json={'text': 'a'}).status_code in (302, 401)


class TestPage:

    def test_toggle_and_bulk_controls(self, proj, auth_client):
        body = auth_client.get('/projects/bulk/hosts').data.decode()
        assert 'id="mode-single"' in body and 'id="mode-bulk"' in body
        assert 'id="bulk-text"' in body and 'id="bulk-stacking"' in body
        assert '/projects/bulk/hosts/bulk/preview' in body
        # single add is unchanged
        assert 'name="hostname"' in body and '/projects/bulk/hosts/add' in body

    def test_preview_output_is_escaped(self):
        with open('app/templates/project_hosts.html') as f:
            src = f.read()
        assert 'esc(i.name)' in src and 'esc(i.reason)' in src
        assert 'esc(e.message)' in src
