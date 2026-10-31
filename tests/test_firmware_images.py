"""
Admin → Firmware images: streamed uploads, the catalogue (SHA-256 and
version), deletion, and who may do it.
"""

import hashlib
import io
import os

import pytest

from app import firmware_images as fi
from app.models import User

FL_IMAGE = b'\x00HDR' + b'junk ' * 100 + b'FL.10.13.1005\x00' + b'x' * 1000


@pytest.fixture
def fw(app, auth_client, tmp_path):
    d = tmp_path / 'firmware'
    d.mkdir()
    app.config['FIRMWARE_DIR'] = str(d)
    app.config['FIRMWARE_MAX_UPLOAD_MB'] = 1
    return d


def upload(client, name, data, **kw):
    return client.post('/admin/firmware/upload?name=' + name, data=data,
                       content_type='application/octet-stream', **kw)


class TestVersions:

    @pytest.mark.parametrize('name, want', [
        ('FL_10_13_1005.swi', ('FL', '10.13.1005')),
        ('ArubaOS-CX_6300-6200_10_13_1005.swi', (None, '10.13.1005')),
        ('PL.10.12.1030.swi', ('PL', '10.12.1030')),
        ('mystery.swi', (None, None)),
    ])
    def test_from_name(self, name, want):
        assert fi.version_from_name(name) == want

    def test_from_the_image_beats_the_name(self, tmp_path):
        p = tmp_path / 'ArubaOS-CX_6300_10_12_0001.swi'
        p.write_bytes(FL_IMAGE)
        assert fi.image_version(str(p), p.name) == ('FL', '10.13.1005', 'image')

    def test_falls_back_to_the_name(self, tmp_path):
        p = tmp_path / 'FL_10_12_0001.swi'
        p.write_bytes(b'nothing useful')
        assert fi.image_version(str(p), p.name) == ('FL', '10.12.0001', 'file name')

    @pytest.mark.parametrize('text, want', [
        ('FL.10.13.1005', ('FL', '10.13.1005')), ('10.13.1005', (None, '10.13.1005')),
        ('', (None, None)), (None, (None, None)), ('garbage', (None, None)),
    ])
    def test_parse_version(self, text, want):
        assert fi.parse_version(text) == want


class TestUpload:

    def test_streams_to_disk_with_checksum(self, app, auth_client, fw):
        res = upload(auth_client, 'FL_10_13_1005.swi', FL_IMAGE)
        assert res.status_code == 200, res.get_json()
        img = res.get_json()['image']
        assert img['sha256'] == hashlib.sha256(FL_IMAGE).hexdigest()
        assert (img['platform'], img['version'], img['uploaded_by']) == ('FL', '10.13.1005', 'admin')
        assert (fw / 'FL_10_13_1005.swi').read_bytes() == FL_IMAGE
        assert [p.name for p in fw.iterdir()] == ['FL_10_13_1005.swi']    # no temp file left

    @pytest.mark.parametrize('name', ['../evil.swi', 'a/b.swi', 'image.bin', '.hidden.swi', ''])
    def test_bad_names(self, auth_client, fw, name):
        res = upload(auth_client, name, b'x')
        assert res.status_code == 400 and list(fw.iterdir()) == []

    def test_too_big(self, auth_client, fw):
        res = upload(auth_client, 'big.swi', b'x' * (1024 * 1024 + 1))
        assert res.status_code == 400 and 'larger than the 1 MB limit' in res.get_json()['error']
        assert list(fw.iterdir()) == []

    def test_too_big_without_a_length_is_stopped_while_streaming(self, app, fw):
        with pytest.raises(fi.ImageError, match='larger'):
            fi.save_upload(app, 'big.swi', io.BytesIO(b'x' * (1024 * 1024 + 5)),
                           length=None, by='admin', max_bytes=1024 * 1024)
        assert list(fw.iterdir()) == []

    def test_cut_short(self, app, fw):
        with pytest.raises(fi.ImageError, match='cut short'):
            fi.save_upload(app, 'a.swi', io.BytesIO(b'abc'), length=10, by='admin',
                           max_bytes=1024)
        assert list(fw.iterdir()) == []

    def test_existing_name_is_refused(self, auth_client, fw):
        (fw / 'a.swi').write_bytes(b'old')
        res = upload(auth_client, 'a.swi', b'new')
        assert res.status_code == 400 and 'already exists' in res.get_json()['error']
        assert (fw / 'a.swi').read_bytes() == b'old'

    def test_empty(self, auth_client, fw):
        assert upload(auth_client, 'a.swi', b'').status_code == 400

    def test_admins_only(self, app, fw):
        User.create('plain', 'password1', app)
        client = app.test_client()
        client.post('/login', data={'username': 'plain', 'password': 'password1'})
        assert upload(client, 'a.swi', b'x').status_code == 403
        assert client.get('/admin/firmware').status_code == 403
        assert client.post('/admin/firmware/delete', data={'name': 'a.swi'}).status_code == 403

    def test_stale_partial_uploads_are_cleared(self, fw):
        (fw / '.upload-abc.part').write_bytes(b'half')
        (fw / 'keep.swi').write_bytes(b'x')
        fi.remove_stale_uploads(str(fw))
        assert [p.name for p in fw.iterdir()] == ['keep.swi']


class TestCatalogue:

    def test_copied_in_images_are_hashed_and_listed(self, app, fw):
        (fw / 'FL_10_12_0001.swi').write_bytes(b'abc')
        [img] = fi.catalogue(app)
        assert img['sha256'] == hashlib.sha256(b'abc').hexdigest()
        assert img['uploaded_by'] is None and img['version'] == '10.12.0001'

    def test_rehashed_when_the_file_changes(self, app, fw):
        p = fw / 'a.swi'
        p.write_bytes(b'one')
        first = fi.catalogue(app)[0]['sha256']
        p.write_bytes(b'two!')
        os.utime(p, ns=(1, 1))
        assert fi.catalogue(app)[0]['sha256'] == hashlib.sha256(b'two!').hexdigest() != first

    def test_upload_record_survives_listing(self, app, auth_client, fw):
        upload(auth_client, 'a.swi', b'abc')
        assert fi.catalogue(app)[0]['uploaded_by'] == 'admin'

    def test_removed_files_are_forgotten(self, app, fw):
        (fw / 'a.swi').write_bytes(b'x')
        fi.catalogue(app)
        (fw / 'a.swi').unlink()
        assert fi.catalogue(app) == []


class TestPage:

    def test_lists_images_with_checksum_and_version(self, app, auth_client, fw):
        upload(auth_client, 'FL_10_13_1005.swi', FL_IMAGE)
        body = auth_client.get('/admin/firmware').get_data(as_text=True)
        assert 'FL_10_13_1005.swi' in body and 'FL.10.13.1005' in body
        assert hashlib.sha256(FL_IMAGE).hexdigest() in body
        assert 'client_max_body_size' in body

    def test_sidebar_link(self, auth_client, fw):
        body = auth_client.get('/projects').get_data(as_text=True)
        assert 'href="/admin/firmware"' in body and 'Firmware images' in body

    def test_delete(self, app, auth_client, fw):
        (fw / 'a.swi').write_bytes(b'x')
        auth_client.post('/admin/firmware/delete', data={'name': 'a.swi'})
        assert not (fw / 'a.swi').exists()

    def test_delete_refused_while_installing(self, app, auth_client, fw):
        from app.firmware_routes import _fw_jobs
        (fw / 'a.swi').write_bytes(b'x')
        _fw_jobs['j'] = {'image': 'a.swi'}
        try:
            body = auth_client.post('/admin/firmware/delete', data={'name': 'a.swi'},
                                    follow_redirects=True).get_data(as_text=True)
        finally:
            _fw_jobs.pop('j')
        assert 'being installed' in body and (fw / 'a.swi').exists()

    def test_delete_cannot_escape_the_directory(self, app, auth_client, fw, tmp_path):
        outside = tmp_path / 'outside.swi'
        outside.write_bytes(b'x')
        auth_client.post('/admin/firmware/delete', data={'name': '../outside.swi'})
        assert outside.exists()


class TestReadOnlyFolder:

    @pytest.fixture
    def ro(self, app, auth_client, fw, monkeypatch):
        monkeypatch.setattr(fi.os, 'access', lambda path, mode: False)
        return fw

    def test_page_explains_and_disables_upload(self, auth_client, ro):
        body = auth_client.get('/admin/firmware').get_data(as_text=True)
        assert 'read-only inside the container' in body
        assert 'id="fw-up-btn" data-click="upload" disabled' in body

    def test_upload_refused_with_the_fix(self, auth_client, ro):
        res = upload(auth_client, 'a.swi', b'x')
        assert res.status_code == 400 and ':ro' in res.get_json()['error']
        assert list(ro.iterdir()) == []

    def test_write_failure_mid_upload_is_explained(self, app, fw, monkeypatch):
        import errno
        real_open = open

        def fail_open(path, mode='r', *a, **k):
            if 'w' in mode:
                raise OSError(errno.EROFS, 'Read-only file system')
            return real_open(path, mode, *a, **k)
        monkeypatch.setattr('builtins.open', fail_open)
        with pytest.raises(fi.ImageError, match='read-only'):
            fi.save_upload(app, 'a.swi', io.BytesIO(b'x'), length=1, by='a', max_bytes=10)
