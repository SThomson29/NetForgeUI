"""
Shared job plumbing: finished-job expiry and throttled output.

Job stores used to keep every job, with its full output, until the process
restarted. Generate output was rebuilt from scratch on every log chunk —
cost proportional to the square of its length.
"""

import threading

import pytest

from app.jobs import (OutputBuffer, prune_finished, mark_finished,
                      JOB_RETENTION_SECS)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestPruneFinished:

    def _store(self):
        return {}, threading.Lock()

    def test_old_finished_jobs_are_dropped(self):
        store, lock = self._store()
        store['old'] = {'status': 'done', '_finished_at': 0.0}
        prune_finished(store, lock, now=JOB_RETENTION_SECS + 1)
        assert 'old' not in store

    def test_recent_finished_jobs_are_kept(self):
        """A page reloaded just after a run ends must still find it."""
        store, lock = self._store()
        store['recent'] = {'status': 'done', '_finished_at': 0.0}
        prune_finished(store, lock, now=JOB_RETENTION_SECS - 1)
        assert 'recent' in store

    def test_running_jobs_are_never_dropped(self):
        store, lock = self._store()
        store['running'] = {'status': 'running'}
        prune_finished(store, lock, now=10 ** 9)
        assert 'running' in store

    def test_finish_time_is_not_shown_to_the_page(self):
        from app.security import job_tag, visible_job
        job = {'status': 'done', **job_tag('a', 'p')}
        mark_finished(job)
        assert visible_job(job, 'a', 'p') == {'status': 'done'}


class TestJobsAreMarkedFinished:
    """Every store must stamp a finish time, or pruning never applies."""

    def test_deploy(self, app, auth_client):
        from unittest.mock import patch
        import time
        import app.deploy_routes as dr
        with patch('app.deploy_routes._run_playbook_job'):
            job_id = dr._start_deploy_job(app, 'deploy_push.yml', {}, {}, None)
            for _ in range(100):
                with dr._deploy_jobs_lock:
                    if '_finished_at' in dr._deploy_jobs[job_id]:
                        break
                time.sleep(0.01)
        assert '_finished_at' in dr._deploy_jobs[job_id]

    def test_deploy_job_that_raises(self, app):
        from unittest.mock import patch
        import time
        import app.deploy_routes as dr
        with patch('app.deploy_routes._run_playbook_job',
                   side_effect=RuntimeError('boom')):
            job_id = dr._start_deploy_job(app, 'deploy_push.yml', {}, {}, None)
            for _ in range(100):
                with dr._deploy_jobs_lock:
                    if '_finished_at' in dr._deploy_jobs[job_id]:
                        break
                time.sleep(0.01)
        job = dr._deploy_jobs[job_id]
        assert job['status'] == 'failed' and '_finished_at' in job

    def test_generate(self, app, auth_client, monkeypatch):
        import time
        import app.projects as P
        import app.docker_runner as D
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'gen')
        monkeypatch.setattr(D, 'run_generate', lambda **kw: (0, 'ok'))
        res = auth_client.post('/projects/admin/gen/generate/run',
                               json={'limit': '', 'tags': ''})
        job_id = res.get_json()['job_id']
        for _ in range(100):
            with P._jobs_lock:
                if '_finished_at' in P._jobs[job_id]:
                    break
            time.sleep(0.01)
        assert P._jobs[job_id]['status'] == 'done'
        assert '_finished_at' in P._jobs[job_id]

    def test_starting_a_job_prunes_old_ones(self, app, auth_client, monkeypatch):
        import app.projects as P
        import app.docker_runner as D
        from app.project import create_project
        with app.app_context():
            create_project(app, 'admin', 'gen2')
        with P._jobs_lock:
            P._jobs['ancient'] = {'status': 'done', '_finished_at': -10 ** 9}
        monkeypatch.setattr(D, 'run_generate', lambda **kw: (0, 'ok'))
        auth_client.post('/projects/admin/gen2/generate/run',
                         json={'limit': '', 'tags': ''})
        assert 'ancient' not in P._jobs


class TestOutputBuffer:

    def test_first_chunk_is_published_at_once(self):
        seen, clock = [], FakeClock()
        buf = OutputBuffer(seen.append, interval=0.5, clock=clock)
        buf.add('PLAY ***\n')
        assert seen == ['PLAY ***\n']

    def test_bursts_are_coalesced(self):
        seen, clock = [], FakeClock()
        buf = OutputBuffer(seen.append, interval=0.5, clock=clock)
        for i in range(1000):
            buf.add('line %d\n' % i)
        assert len(seen) == 1          # only the first, until time passes
        clock.t += 0.6
        buf.add('later\n')
        assert len(seen) == 2
        assert seen[-1].endswith('later\n')
        assert seen[-1].count('\n') == 1001

    def test_flush_publishes_the_tail(self):
        seen, clock = [], FakeClock()
        buf = OutputBuffer(seen.append, interval=0.5, clock=clock)
        buf.add('a\n')
        buf.add('b\n')
        buf.flush()
        assert seen[-1] == 'a\nb\n'

    def test_flush_with_nothing_new_does_not_republish(self):
        seen, clock = [], FakeClock()
        buf = OutputBuffer(seen.append, interval=0.5, clock=clock)
        buf.add('a\n')
        buf.flush()
        buf.flush()
        assert seen == ['a\n']

    def test_generate_output_is_complete_despite_throttling(self, tmp_path, monkeypatch):
        """The final output must hold every line even if most were never
        individually published."""
        import tests.test_docker_runner as T
        import app.docker_runner as dr

        class Chatty(T.FakeContainer):
            def logs(self, stream=True, follow=True):
                return iter([('line %d\n' % i).encode() for i in range(5000)])

        fs = tmp_path / 'c'
        fs.mkdir()
        client = T.FakeClient(str(fs), lambda f: 0)
        client.containers.create = lambda **kw: setattr(
            client, 'container', Chatty(client.fs_root, client.run)) \
            or client.container
        monkeypatch.setattr(dr, '_get_client', lambda: client)
        proj = tmp_path / 'data' / 'u' / 'projects' / 'p'
        (proj / 'generated_configs').mkdir(parents=True)

        published = []
        rc, out = dr.run_generate('j', 'u', 'p', str(tmp_path / 'data'),
                                  progress_callback=published.append)
        assert out.count('\n') == 5000
        assert published[-1] == out
        assert len(published) < 50
