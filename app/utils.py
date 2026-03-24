import os
import configparser
import shutil


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STACKING_GROUPS = {
    'cx_vsx': 'VSX',
    'cx_vsf': 'VSF',
    'cx':     'None',
}

STACKING_TO_GROUP = {
    'vsx':  'cx_vsx',
    'vsf':  'cx_vsf',
    'none': 'cx',
}

PLATFORM_MAP = {
    'cx_vsx': 'aoscx',
    'cx_vsf': 'aoscx',
    'cx':     'aoscx',
}

SKIP_VSX = {'cx_vsf', 'cx'}
SKIP_VSF = {'cx_vsx', 'cx'}

HOSTVARS_FILES = [
    'general.yml',
    'management.yml',
    'banner.yml',
    'snmp.yml',
    'aaa.yml',
    'vrfs.yml',
    'vlans.yml',
    'static_routes.yml',
    'interfaces.yml',
    'routing.yml',
    'vxlan.yml',
    'vsx.yml',
    'vsf.yml',
]


# ---------------------------------------------------------------------------
# User directory helpers (used by admin for user creation/deletion)
# ---------------------------------------------------------------------------

def user_dir(app, username):
    return os.path.join(app.config['DATA_DIR'], username)

def ensure_workspace(app, username):
    """Create the user's base workspace directory."""    
    os.makedirs(user_dir(app, username), exist_ok=True)
    os.makedirs(os.path.join(user_dir(app, username), 'projects'), exist_ok=True)


# ---------------------------------------------------------------------------
# Project-scoped workspace helpers
# ---------------------------------------------------------------------------

def project_hosts_ini_path(app, username, project_name):
    from .project import project_hosts_ini_path as _p
    return _p(app, username, project_name)

def project_host_vars_dir(app, username, project_name):
    from .project import project_host_vars_dir as _p
    return _p(app, username, project_name)

def project_generated_configs_dir(app, username, project_name):
    from .project import project_generated_configs_dir as _p
    return _p(app, username, project_name)

def read_project_hosts(app, username, project_name):
    """Return list of dicts: [{hostname, group, stacking}] for a project."""
    from .project import project_hosts_ini_path
    ini = project_hosts_ini_path(app, username, project_name)
    if not os.path.exists(ini):
        return []
    cp = configparser.ConfigParser(allow_no_value=True)
    cp.optionxform = str
    cp.read(ini)
    hosts = []
    for group, label in STACKING_GROUPS.items():
        if cp.has_section(group):
            for host in cp.options(group):
                if host:
                    hosts.append({'hostname': host, 'group': group, 'stacking': label})
    return hosts

def add_project_host(app, username, project_name, hostname, stacking):
    """Add a host to a project's hosts.ini and scaffold host_vars."""
    from .project import project_hosts_ini_path, project_host_vars_dir
    ini   = project_hosts_ini_path(app, username, project_name)
    hvdir = project_host_vars_dir(app, username, project_name)
    cp = configparser.ConfigParser(allow_no_value=True)
    cp.optionxform = str
    cp.read(ini)
    group = STACKING_TO_GROUP.get(stacking, 'cx')
    if not cp.has_section(group):
        cp.add_section(group)
    cp.set(group, hostname)
    with open(ini, 'w') as f:
        cp.write(f)
    _scaffold_host_vars_to(app, hostname, stacking, hvdir)

def remove_project_host(app, username, project_name, hostname):
    """Remove a host from a project."""
    from .project import (project_hosts_ini_path, project_host_vars_dir,
                          project_generated_configs_dir)
    ini = project_hosts_ini_path(app, username, project_name)
    cp = configparser.ConfigParser(allow_no_value=True)
    cp.optionxform = str
    cp.read(ini)
    for group in STACKING_GROUPS:
        if cp.has_section(group) and cp.has_option(group, hostname):
            cp.remove_option(group, hostname)
    with open(ini, 'w') as f:
        cp.write(f)
    hvdir = os.path.join(project_host_vars_dir(app, username, project_name), hostname)
    if os.path.isdir(hvdir):
        shutil.rmtree(hvdir)
    gcdir = project_generated_configs_dir(app, username, project_name)
    if os.path.isdir(gcdir):
        for fname in os.listdir(gcdir):
            if fname.startswith(hostname + '_'):
                os.remove(os.path.join(gcdir, fname))

def _scaffold_host_vars_to(app, hostname, stacking, base_hvdir):
    """Scaffold host_vars for a hostname into an arbitrary host_vars directory."""
    group    = STACKING_TO_GROUP.get(stacking, 'cx')
    platform = PLATFORM_MAP.get(group, 'aoscx')
    skeleton = os.path.join(app.config['SKELETON_DIR'], platform)
    dest     = os.path.join(base_hvdir, hostname)
    if os.path.isdir(dest):
        return
    if not os.path.isdir(skeleton):
        raise RuntimeError(
            f'Skeleton directory not found at {skeleton}. '
            f'Pull the config repo from the Admin panel first.'
        )
    os.makedirs(dest, exist_ok=True)
    for fname in os.listdir(skeleton):
        if not fname.endswith('.yml'):
            continue
        if fname == 'vsx.yml' and group in SKIP_VSX:
            continue
        if fname == 'vsf.yml' and group in SKIP_VSF:
            continue
        src_path = os.path.join(skeleton, fname)
        dst_path = os.path.join(dest, fname)
        with open(src_path) as f:
            content = f.read()
        content = content.replace('__HOSTNAME__', hostname)
        with open(dst_path, 'w') as f:
            f.write(content)

def list_project_generated_configs(app, username, project_name):
    from .project import project_generated_configs_dir
    gcdir = project_generated_configs_dir(app, username, project_name)
    if not os.path.isdir(gcdir):
        return []
    return sorted(
        f for f in os.listdir(gcdir)
        if f.endswith('.ios') and not f.startswith('.')
    )

def save_hostvars_file(app_or_path, *args):
    """Write a single host_vars YAML file.
    Accepts either (app, username, project_name, hostname, filename, content)
    or legacy (app, username, hostname, filename, content) for compatibility.
    """
    # Called from projects.py as save_hostvars_file directly with path
    # This is a thin wrapper kept for import compatibility
    raise NotImplementedError("Use project-scoped save in projects.py directly")
