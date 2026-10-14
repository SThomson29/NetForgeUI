"""
Background job plumbing shared by generate, deploy and firmware.

    run_ansible_job   run a playbook on this machine, streaming into a job
    OutputBuffer      publish growing output at a bounded rate
    prune_finished    drop finished jobs once nobody will poll them

Each feature keeps its own job store (a dict and a lock); these helpers
take the store as an argument rather than owning one, so the stores stay
where their routes and tests expect them.

Jobs carry private fields prefixed with '_' (owner, project, finish time).
security.visible_job strips them before a job is returned to the page.
"""

import glob
import json
import os
import shlex
import subprocess
import tempfile
import threading
import time

from .ansible_safe import dump_unsafe


# A finished job is kept this long so a page polling it — or reloaded just
# after it ended — still sees the result. After that it is only memory: the
# full output of every run since the container started.
JOB_RETENTION_SECS = 60 * 60

# How often growing output is republished into the job. Rebuilding it on
# every line costs time proportional to the square of its length; the page
# polls once a second, so publishing more often than this shows nothing.
OUTPUT_PUBLISH_SECS = 0.5


def mark_finished(job):
    job['_finished_at'] = time.monotonic()


def prune_finished(store, lock, now=None):
    """Remove jobs that finished more than JOB_RETENTION_SECS ago."""
    now = time.monotonic() if now is None else now
    with lock:
        stale = [jid for jid, job in store.items()
                 if job.get('_finished_at') is not None
                 and now - job['_finished_at'] > JOB_RETENTION_SECS]
        for jid in stale:
            del store[jid]


class OutputBuffer:
    """Collect output chunks; hand the joined text to publish() at most
    every OUTPUT_PUBLISH_SECS, and always on flush()."""

    def __init__(self, publish, interval=None, clock=time.monotonic):
        self._publish = publish
        self._interval = OUTPUT_PUBLISH_SECS if interval is None else interval
        self._clock = clock
        self._parts = []
        self._last = None
        self._dirty = False

    def add(self, chunk):
        self._parts.append(chunk)
        self._dirty = True
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            self._last = now
            self.flush()

    def flush(self):
        if self._dirty:
            self._dirty = False
            self._publish(self.text())

    def text(self):
        return ''.join(self._parts)


def run_ansible_job(store, lock, job_id, *, playbook_path, repo_dir,
                    ansible_bin, inventory, extra_vars, limit_hosts=None,
                    extra_args=(), timeout_secs, result_glob='*.json'):
    """Run ansible-playbook, streaming its output into store[job_id].

    Runs in a worker thread, so everything it needs is passed in — there is
    no application context here. Inventory and extra vars are written with
    every string tagged !unsafe, so request values are never templated.

    Per-host results are the JSON files the playbook writes into the
    directory passed to it as results_file.
    """
    def _set(**kw):
        with lock:
            store[job_id].update(kw)

    def _publish(text):
        _set(output=text)

    if not os.path.isfile(playbook_path):
        _set(status='failed', returncode=-1,
             output='Playbook not found: %s' % playbook_path)
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = os.path.join(tmpdir, 'inventory.yml')
        with open(inv_path, 'w') as f:
            dump_unsafe(inventory, f)

        results_dir = os.path.join(tmpdir, 'results')
        os.makedirs(results_dir, exist_ok=True)
        extra_vars = dict(extra_vars, results_file=results_dir)

        vars_path = os.path.join(tmpdir, 'extra_vars.yml')
        with open(vars_path, 'w') as f:
            dump_unsafe(extra_vars, f)

        cmd = [ansible_bin, playbook_path, '-i', inv_path, '-e', '@' + vars_path]
        if limit_hosts:
            cmd.extend(['--limit', ','.join(limit_hosts)])
        cmd.extend(extra_args)

        env = os.environ.copy()
        env['ANSIBLE_HOST_KEY_CHECKING'] = 'False'
        env['ANSIBLE_NOCOLOR'] = '1'
        env['PYTHONUNBUFFERED'] = '1'
        # Deliberately the default stdout callback. 'json' lives in
        # ansible.posix, not ansible-core, so requesting it fails to load;
        # it would also buffer everything until the run ends. Per-host
        # detail comes from the result files instead.

        _set(status='running', command=' '.join(shlex.quote(c) for c in cmd))

        try:
            proc = subprocess.Popen(
                cmd, cwd=repo_dir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            _set(status='failed', returncode=-1,
                 output='ansible-playbook not found. Is ansible-core '
                        'installed in this image?')
            return

        timed_out = threading.Event()

        def _kill():
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout_secs, _kill)
        timer.daemon = True
        timer.start()
        out = OutputBuffer(_publish)
        # The with block closes the output pipe as well as waiting.
        with proc:
            try:
                for line in proc.stdout:
                    out.add(line)
                proc.wait()
            finally:
                timer.cancel()

        if timed_out.is_set():
            out.add('\nStopped: the run exceeded %d minutes.\n'
                    % (timeout_secs // 60))
        out.flush()

        results = []
        for rf in sorted(glob.glob(os.path.join(results_dir, result_glob))):
            try:
                with open(rf) as f:
                    results.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass

        rc = proc.returncode
        _set(status='done' if rc == 0 else 'failed',
             returncode=rc, results=results)
