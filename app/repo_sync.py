"""
Which NetForge the app runs: following a branch, or pinned to a release.

    following   on a branch (normally main). A container start and the
                Admin "Pull latest" button move to its newest commit.
    pinned      on a release tag, and staying there. A container start
                fetches, so newer releases can be seen, but doesn't move.

Which of the two is simply what the repo has checked out, so the setting
can't disagree with what generation uses: a branch means following, a tag
means pinned. An admin changes it on Admin → Config repo.

CONFIGGEN_REPO_REF, when set, decides instead: every container start
checks that tag or branch out, and the Admin page shows it read-only. That
keeps a managed instance's version in its deployment config, where a
rebuild finds it again.

Refs named in a request are only ever used after being matched against
the repo's own tags and remote branches, so nothing from a request reaches
git as an option or a path.

The app keeps a second repo the same way: the samthomson.new_central
Ansible collection, which applies Central projects. Every function here
takes a source saying which repo it means and defaults to NetForge, so
NetForge callers read as they always have. The collection follows a branch
or is pinned by CENTRAL_REPO_REF; see CENTRAL.
"""

import contextlib
import os
import re
import shutil
import subprocess

from . import repo_version
from .ssh_setup import get_git_env

MAX_RELEASES = 12       # newest releases offered
MAX_COMMITS = 30        # commits listed for each

# What generation renders from; a change outside these can't alter a config.
TEMPLATE_PATHS = ('roles/generate_config', 'playbooks/generate_configs.yml')


class Source:
    """One repo the app clones and keeps at a version.

        name       what to call it in messages
        path_key   app.config key holding where it is cloned
        url_key    app.config key holding its remote
        ref_key    app.config key holding the tag or branch the
                   environment fixes it to, if any
        paths      what a run reads from it; a change outside these can't
                   alter a result
    """

    def __init__(self, name, path_key, url_key, ref_key, paths):
        self.name = name
        self.path_key = path_key
        self.url_key = url_key
        self.ref_key = ref_key
        self.paths = tuple(paths)

    def path(self, app):
        return app.config.get(self.path_key) or ''

    def url(self, app):
        return (app.config.get(self.url_key) or '').strip()


NETFORGE = Source('NetForge', 'CONFIGGEN_REPO', 'CONFIGGEN_REPO_URL',
                  'CONFIGGEN_REPO_REF', TEMPLATE_PATHS)

# The collection's modules and the playbooks that apply a Central build.
CENTRAL = Source('the Central collection', 'CENTRAL_REPO', 'CENTRAL_REPO_URL',
                 'CENTRAL_REPO_REF', ('plugins', 'playbooks', 'meta'))

_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}$')


class RepoError(Exception):
    """A repo operation that didn't work, with a message for the admin."""


def _mask(text):
    """git prints the remote URL, which can carry a token."""
    return re.sub(r'(://)[^/@\s]+@', r'\1********@', text or '')


def _run(repo, *args, env=None, timeout=120):
    """(returncode, combined output) of a git command in repo."""
    try:
        p = subprocess.run(['git', '-C', repo] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, 'git %s timed out after %d seconds' % (args[0], timeout)
    except OSError as e:
        return 1, str(e)
    return p.returncode, _mask(p.stdout).strip()


def _out(repo, *args):
    rc, out = _run(repo, *args, timeout=20)
    return out if rc == 0 else None


def _repo(app, source=NETFORGE):
    return source.path(app)


def cloned(app, source=NETFORGE):
    # No path configured is not a clone; joining '' would test the
    # working directory instead.
    repo = _repo(app, source)
    return bool(repo) and os.path.isdir(os.path.join(repo, '.git'))


def configured_ref(app, source=NETFORGE):
    """The tag or branch the environment fixes the repo to
    (CONFIGGEN_REPO_REF for NetForge), or None."""
    return (app.config.get(source.ref_key) or '').strip() or None


def current_branch(repo):
    """The branch checked out, or None on a tag (detached)."""
    return _out(repo, 'symbolic-ref', '-q', '--short', 'HEAD')


def default_branch(repo):
    head = _out(repo, 'symbolic-ref', '-q', '--short', 'refs/remotes/origin/HEAD')
    return head.split('/', 1)[1] if head and '/' in head else 'main'


def state(app, source=NETFORGE):
    """{cloned, mode: 'follow'|'pinned', branch, version, locked}. locked
    is the configured ref when the environment decides."""
    repo = _repo(app, source)
    if not cloned(app, source):
        return {'cloned': False, 'mode': None, 'branch': None, 'version': None,
                'locked': configured_ref(app, source)}
    branch = current_branch(repo)
    version = repo_version.read(repo)
    return {'cloned': True, 'mode': 'follow' if branch else 'pinned', 'branch': branch,
            'version': version['label'] if version else None,
            'locked': configured_ref(app, source)}


def fetch(app, source=NETFORGE):
    """Bring the remote's commits and tags in without moving anything."""
    rc, out = _run(_repo(app, source), 'fetch', '--tags', '--prune', 'origin', env=get_git_env(app))
    if rc != 0:
        raise RepoError('Could not reach %s repo: %s' % (
            'the NetForge' if source is NETFORGE else source.name + "'s",
            out or 'git fetch failed'))
    return out


def _resolve(repo, ref, name='NetForge'):
    """(kind, git name to check out, name to compare with) for a tag or
    remote branch called ref. Raises RepoError for anything else."""
    ref = str(ref or '').strip()
    # origin/HEAD is a pointer to the default branch, not a branch.
    if not _REF.match(ref) or '..' in ref or ref == 'HEAD':
        raise RepoError('"%s" is not a release or branch name.' % ref[:100])
    if _out(repo, 'rev-parse', '-q', '--verify', 'refs/tags/%s^{commit}' % ref):
        return 'tag', 'refs/tags/%s' % ref, 'refs/tags/%s' % ref
    if _out(repo, 'rev-parse', '-q', '--verify', 'refs/remotes/origin/%s^{commit}' % ref):
        return 'branch', ref, 'refs/remotes/origin/%s' % ref
    raise RepoError('%s has no release or branch called "%s".' % (name[:1].upper() + name[1:], ref))


def _releases(repo):
    """Release tags, newest version first, one per commit: where several
    tags sit on one commit the highest stands for it."""
    seen, out = set(), []
    for tag in (_out(repo, 'tag', '--sort=-v:refname') or '').splitlines():
        if not _REF.match(tag):
            continue
        commit = _out(repo, 'rev-parse', '-q', '--verify', 'refs/tags/%s^{commit}' % tag)
        if commit and commit not in seen:
            seen.add(commit)
            out.append(tag)
        if len(out) >= MAX_RELEASES:
            break
    return out


def _commits(repo, frm, to):
    log = _out(repo, 'log', '--format=%h%x09%ad%x09%s', '--date=short',
               '-n', str(MAX_COMMITS), '%s..%s' % (frm, to)) or ''
    return [dict(zip(('commit', 'date', 'subject'), line.split('\t', 2)))
            for line in log.splitlines() if line.count('\t') >= 2]


def _choice(repo, ref, branch_now, source=NETFORGE):
    kind, _checkout, target = _resolve(repo, ref, source.name)
    head = _out(repo, 'rev-parse', 'HEAD')
    commit = _out(repo, 'rev-parse', '%s^{commit}' % target)
    ahead = int(_out(repo, 'rev-list', '--count', 'HEAD..%s' % target) or 0)
    behind = int(_out(repo, 'rev-list', '--count', '%s..HEAD' % target) or 0)
    rc, _ = _run(repo, 'diff', '--quiet', 'HEAD', target, '--', *source.paths, timeout=20)
    if kind == 'branch':
        current = branch_now == ref
    else:
        current = branch_now is None and commit == head
    return {
        'ref': ref, 'kind': kind, 'commit': (commit or '')[:7], 'current': current,
        # Moving here adds `ahead` commits and drops `behind`.
        'ahead': ahead, 'behind': behind,
        'commits': _commits(repo, 'HEAD', target) if ahead else _commits(repo, target, 'HEAD'),
        'templates_changed': rc == 1,
    }


def choices(app, source=NETFORGE):
    """What the repo can be moved to, as fetched: the default branch to
    follow, then the newest releases to pin to."""
    repo = _repo(app, source)
    branch_now = current_branch(repo)
    refs = [default_branch(repo)]
    if branch_now and branch_now not in refs:
        refs.append(branch_now)
    out = []
    for ref in refs + _releases(repo):
        try:
            out.append(_choice(repo, ref, branch_now, source))
        except RepoError:
            continue
    return out


def switch(app, ref, source=NETFORGE):
    """Check out the release or branch called ref, as last fetched.
    Returns (version before, version after)."""
    repo = _repo(app, source)
    kind, checkout, target = _resolve(repo, ref, source.name)
    before = repo_version.read(repo)
    if kind == 'tag':
        rc, out = _run(repo, 'checkout', '-q', '--detach', checkout)
    else:
        rc, out = _run(repo, 'checkout', '-q', checkout)
        if rc == 0:
            rc, out = _run(repo, 'merge', '-q', '--ff-only', target)
    repo_version.forget()
    if rc != 0:
        raise RepoError('Could not switch %s to %s: %s' % (source.name, ref, out or 'git failed'))
    return before, repo_version.read(repo)


def clone(app, source=NETFORGE, log=print):
    """Clone a repo that isn't here yet, then check out the ref the
    environment fixes it to, if any. What a container's first start does.
    Returns True if the clone worked.

    A non-empty directory with no .git means a previous clone was
    interrupted, or image content seeded the volume. git clone refuses to
    write into it, so it is cleared rather than wedging every restart.
    """
    repo, url = _repo(app, source), source.url(app)
    if os.path.isdir(repo) and os.listdir(repo):
        log('[repo] %s is non-empty but not a git repo — clearing it' % repo)
        for entry in os.listdir(repo):
            path = os.path.join(repo, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    os.remove(path)

    # The remote can carry a token, so it is masked wherever it is shown.
    log('[repo] Cloning %s into %s ...' % (_mask(url), repo))
    os.makedirs(repo, exist_ok=True)
    try:
        result = subprocess.run(['git', 'clone', url, repo], capture_output=True,
                                text=True, env=get_git_env(app))
    except OSError as e:
        log('[repo] Clone failed: %s' % e)
        return False
    if result.returncode != 0:
        log('[repo] Clone failed: %s' % _mask(result.stderr.strip()))
        return False
    log('[repo] Clone successful')
    repo_version.forget()

    # A fresh clone lands on the default branch; move it if a ref is set.
    ref = configured_ref(app, source)
    if ref:
        try:
            _before, after = switch(app, ref, source)
            log('[repo] Checked out %s (%s): %s' % (ref, source.ref_key,
                                                    after['label'] if after else '?'))
        except RepoError as e:
            log('[repo] %s' % e)
    return True


def sync_at_startup(app, log=print, source=NETFORGE):
    """What a container start does to an existing clone.

        ref set in the environment   fetch, then check that ref out
        following a branch           pull it, as always
        pinned in Admin              fetch only, and stay on the release

    Returns (returncode, output, version before, version after).
    """
    repo = _repo(app, source)
    before = repo_version.read(repo)
    ref = configured_ref(app, source)
    env = get_git_env(app)
    if ref:
        log('[repo] Fetching, then checking out %s (%s) ...' % (ref, source.ref_key))
        rc, out = _run(repo, 'fetch', '--tags', '--prune', 'origin', env=env)
        try:
            # Even if the fetch failed: the ref may already be here.
            switch(app, ref, source)
        except RepoError as e:
            rc, out = 1, '%s\n%s' % (out, e)
    elif current_branch(repo):
        log('[repo] Pulling latest ...')
        rc, out = _run(repo, 'pull', env=env)
    else:
        log('[repo] Pinned to a release; fetching without moving ...')
        rc, out = _run(repo, 'fetch', '--tags', '--prune', 'origin', env=env)
    repo_version.forget()
    after = repo_version.read(repo)
    log('[repo] %s: %s' % ('Done' if rc == 0 else 'Failed', out or '(no output)'))
    if after:
        log('[repo] %s is at %s' % (source.name[:1].upper() + source.name[1:], after['label']))
    return rc, out, before, after
