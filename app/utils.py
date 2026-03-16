import os
import configparser
import shutil


# ---------------------------------------------------------------------------
# User workspace helpers
# ---------------------------------------------------------------------------

def user_dir(app, username):
    return os.path.join(app.config['DATA_DIR'], username)

def hosts_ini_path(app, username):
    return os.path.join(user_dir(app, username), 'hosts.ini')

def host_vars_dir(app, username):
    return os.path.join(user_dir(app, username), 'host_vars')

def generated_configs_dir(app, username):
    return os.path.join(user_dir(app, username), 'generated_configs')

def ensure_workspace(app, username):
    """Create the user's workspace directories and a blank hosts.ini if needed."""
    dirs = [
        user_dir(app, username),
        host_vars_dir(app, username),
        generated_configs_dir(app, username),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    ini = hosts_ini_path(app, username)
    if not os.path.exists(ini):
        with open(ini, 'w') as f:
            f.write('[cx_vsx]\n\n[cx_vsf]\n\n[cx]\n\n[cx:children]\ncx_vsx\ncx_vsf\n')


# ---------------------------------------------------------------------------
# hosts.ini helpers
# ---------------------------------------------------------------------------

# Map group name -> stacking type label shown in UI
STACKING_GROUPS = {
    'cx_vsx': 'VSX',
    'cx_vsf': 'VSF',
    'cx':     'None',
}

# Map stacking choice -> ini group
STACKING_TO_GROUP = {
    'vsx':  'cx_vsx',
    'vsf':  'cx_vsf',
    'none': 'cx',
}

def read_hosts(app, username):
    """Return list of dicts: [{hostname, group, stacking}]."""
    ini = hosts_ini_path(app, username)
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

def add_host(app, username, hostname, stacking):
    """Add a host to the correct group in hosts.ini and scaffold its host_vars."""
    ini = hosts_ini_path(app, username)
    cp  = configparser.ConfigParser(allow_no_value=True)
    cp.optionxform = str
    cp.read(ini)

    group = STACKING_TO_GROUP.get(stacking, 'cx')
    if not cp.has_section(group):
        cp.add_section(group)
    cp.set(group, hostname)

    with open(ini, 'w') as f:
        cp.write(f)

    _scaffold_host_vars(app, username, hostname, stacking)

def remove_host(app, username, hostname):
    """Remove a host from hosts.ini and delete its host_vars directory."""
    ini = hosts_ini_path(app, username)
    cp  = configparser.ConfigParser(allow_no_value=True)
    cp.optionxform = str
    cp.read(ini)

    for group in STACKING_GROUPS:
        if cp.has_section(group) and cp.has_option(group, hostname):
            cp.remove_option(group, hostname)

    with open(ini, 'w') as f:
        cp.write(f)

    hvdir = os.path.join(host_vars_dir(app, username), hostname)
    if os.path.isdir(hvdir):
        shutil.rmtree(hvdir)

    # Remove any generated configs for this host
    gcdir = generated_configs_dir(app, username)
    for fname in os.listdir(gcdir):
        if fname.startswith(hostname + '_'):
            os.remove(os.path.join(gcdir, fname))


# ---------------------------------------------------------------------------
# host_vars scaffolding
# ---------------------------------------------------------------------------

PLATFORM_MAP = {
    'cx_vsx': 'aoscx',
    'cx_vsf': 'aoscx',
    'cx':     'aoscx',
}

SKIP_VSX = {'cx_vsf', 'cx'}
SKIP_VSF = {'cx_vsx', 'cx'}

def _scaffold_host_vars(app, username, hostname, stacking):
    group     = STACKING_TO_GROUP.get(stacking, 'cx')
    platform  = PLATFORM_MAP.get(group, 'aoscx')
    skeleton  = os.path.join(app.config['SKELETON_DIR'], platform)
    dest      = os.path.join(host_vars_dir(app, username), hostname)

    if os.path.isdir(dest):
        return  # already exists, don't overwrite

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


# ---------------------------------------------------------------------------
# host_vars file I/O
# ---------------------------------------------------------------------------

# Ordered list of YAML files for display
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

def get_hostvars_files(app, username, hostname):
    """Return ordered list of {filename, content, exists} for the host."""
    hvdir = os.path.join(host_vars_dir(app, username), hostname)
    result = []
    for fname in HOSTVARS_FILES:
        fpath = os.path.join(hvdir, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                content = f.read()
            result.append({'filename': fname, 'content': content, 'exists': True})
    return result

def save_hostvars_file(app, username, hostname, filename, content):
    """Write a single host_vars YAML file."""
    # Only allow known filenames to prevent path traversal
    if filename not in HOSTVARS_FILES:
        raise ValueError(f'Unknown file: {filename}')
    hvdir = os.path.join(host_vars_dir(app, username), hostname)
    os.makedirs(hvdir, exist_ok=True)
    fpath = os.path.join(hvdir, filename)
    with open(fpath, 'w') as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Generated config listing
# ---------------------------------------------------------------------------

def list_generated_configs(app, username):
    """Return list of generated .ios filenames for this user."""
    gcdir = generated_configs_dir(app, username)
    if not os.path.isdir(gcdir):
        return []
    return sorted(
        f for f in os.listdir(gcdir)
        if f.endswith('.ios') and not f.startswith('.')
    )

def delete_generated_config(app, username, filename):
    gcdir = generated_configs_dir(app, username)
    fpath = os.path.join(gcdir, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)
