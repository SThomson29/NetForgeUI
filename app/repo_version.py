"""
Which NetForge is checked out: the templates and playbooks every generation
runs come from that repo, not this one.

    current(app) -> {'commit': full hash, 'label': 'v1.4.1 (0375b4f)'} or None

The label names the release tag on the commit. Between releases it says how
far past the last one the repo is: 'v1.4.1 +3 (abc1234)'. Several tags can
sit on one commit; the highest version wins. The commit, not the label, is
what tells two versions apart, so tagging a commit later changes nothing
but its name.

None when the repo isn't a git checkout, in which case nothing is recorded
or judged.
"""

import os
import subprocess
import threading
import time

# Looked up by every Generate page and by each project's status badge, so
# the answer is kept briefly. A pull calls forget().
_TTL_SECS = 5
_cache = {}
_lock = threading.Lock()


def _git(repo, *args):
    try:
        out = subprocess.run(['git', '-C', repo] + list(args), capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _highest_tag(repo, at):
    tags = _git(repo, 'tag', '--points-at', at, '--sort=-v:refname')
    return tags.splitlines()[0] if tags else None


def read(repo):
    """The version of the checkout at repo, uncached."""
    if not repo or not os.path.isdir(os.path.join(repo, '.git')):
        return None
    commit = _git(repo, 'rev-parse', 'HEAD')
    if not commit:
        return None
    short = commit[:7]
    tag = _highest_tag(repo, 'HEAD')
    if tag:
        return {'commit': commit, 'label': '%s (%s)' % (tag, short)}
    nearest = _git(repo, 'describe', '--tags', '--abbrev=0')
    if nearest:
        tag = _highest_tag(repo, nearest + '^{commit}') or nearest
        ahead = _git(repo, 'rev-list', '--count', '%s..HEAD' % tag)
        if ahead:
            return {'commit': commit, 'label': '%s +%s (%s)' % (tag, ahead, short)}
    return {'commit': commit, 'label': short}


def current(app):
    repo = app.config.get('CONFIGGEN_REPO') or ''
    now = time.monotonic()
    with _lock:
        hit = _cache.get(repo)
        if hit and now - hit[0] < _TTL_SECS:
            return hit[1]
    version = read(repo)
    with _lock:
        _cache[repo] = (now, version)
    return version


def forget():
    """Drop the kept answer; the repo has just changed."""
    with _lock:
        _cache.clear()
