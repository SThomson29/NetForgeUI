"""
What kind of project this is.

    config    AOS-CX switch configs generated from NetForge and pushed over
              SSH. Every project made before types existed is one of these.
    central   an HPE Aruba Networking Central tenant build, applied through
              the Central API with the samthomson.new_central collection.

The type is chosen when the project is created and kept as 'type' in its
config.json. It never changes afterwards. A config.json with no 'type' is a
config project, so projects from before this existed are read as they are
and nothing is written to them.

Each project page belongs to one type or to both. The projects blueprint
asks allowed() before any view runs, so a config page (Hosts, Generate,
Deploy, ...) answers 404 for a central project and the other way round,
whatever the URL says. A page added without being listed here is a config
page: that is what every page was before types, and it is the safe side to
fall on.
"""

import json
import os

CONFIG, CENTRAL = 'config', 'central'

LABELS = {
    CONFIG:  'Config',
    CENTRAL: 'Central',
}

DESCRIPTIONS = {
    CONFIG:  'AOS-CX switch configs, generated with NetForge and pushed to the switches.',
    CENTRAL: 'A New Central tenant build: sites, groups, profiles and assignments, '
             'applied through the Central API.',
}

# Pages every project has, whatever its type.
COMMON_ENDPOINTS = frozenset({
    'projects.delete',
    'projects.project_history',
    'projects.sharing_page',
    'projects.share_project',
    'projects.unshare_project',
    'projects.transfer_project',
})

# Pages only a central project has.
CENTRAL_ENDPOINTS = frozenset({
    'projects.central_overview',
})

# Where opening a project lands.
HOME_ENDPOINTS = {
    CONFIG:  'projects.hosts_page',
    CENTRAL: 'projects.central_overview',
}


def of(app, owner, project_name):
    """The type of owner's project. CONFIG when the project records none
    (or can't be read), which is every project older than types."""
    path = os.path.join(app.config['DATA_DIR'], owner, 'projects', project_name, 'config.json')
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return CONFIG
    kind = cfg.get('type') if isinstance(cfg, dict) else None
    return kind if isinstance(kind, str) and kind else CONFIG


def label(kind):
    return LABELS.get(kind, kind)


def allowed(endpoint, kind):
    """Whether a project of this type has the page at endpoint."""
    if endpoint in COMMON_ENDPOINTS:
        return True
    if endpoint in CENTRAL_ENDPOINTS:
        return kind == CENTRAL
    return kind == CONFIG


def home_endpoint(kind):
    """The page a project of this type opens on. A type this version
    doesn't know still has its History."""
    return HOME_ENDPOINTS.get(kind, 'projects.project_history')


def central_available(app):
    """Central projects can be created only where the collection that
    applies them has been configured (CENTRAL_REPO_URL). Without it nothing
    about them is offered."""
    return bool((app.config.get('CENTRAL_REPO_URL') or '').strip())


def collection_state(app):
    """What the Central collection is checked out at, for pages that show
    it: repo_sync.state plus 'configured' (CENTRAL_REPO_URL is set)."""
    from . import repo_sync
    return dict(repo_sync.state(app, repo_sync.CENTRAL), configured=central_available(app))


def creatable(app):
    """The types a new project can be here, config first."""
    return [CONFIG] + ([CENTRAL] if central_available(app) else [])
