"""
A change summary for a dry run: what each switch would receive, for a
change request.

Built from the dry run's audit entry, not the browser, so it's available
from History afterwards and the commands are the masked copies the log
keeps. Each switch's config is checked against the file on disk now: if
it has been regenerated since the dry run, the summary says so at the top,
because the commands shown are no longer what a push would send.
"""

import os

from . import audit


def _truthy(v):
    return v in (True, 'True', 'true')


def build(app, owner, project, entry_id):
    """The summary for one dry run, or None if entry_id isn't a dry run
    of this project."""
    from .deploy_routes import _load_mapping
    from .project import get_inventory, project_generated_configs_dir
    e = audit.entry(app, entry_id)
    if not e or e['action'] != 'deploy_dryrun' or e['owner'] != owner \
            or e['project'] != project:
        return None

    config_dir = project_generated_configs_dir(app, owner, project)
    inventory = get_inventory(app, owner, project)
    results = {str(r.get('hostname')): r for r in e['results'] if isinstance(r, dict)}
    switches, regenerated = [], []
    for t in e['targets']:
        host = t.get('hostname')
        r = results.get(host)
        name = t.get('config_file') or ''
        current = audit.file_fingerprint(os.path.join(config_dir, os.path.basename(name))) \
            if name else None
        changed_since = bool(t.get('sha256')) and current != t.get('sha256')
        if changed_since:
            regenerated.append(host)
        commands = r.get('commands') if r else None
        switches.append({
            'hostname': host,
            'mgmt_ip': t.get('mgmt_ip', ''),
            'config_file': name,
            'sha256': t.get('sha256'),
            'generated_by': t.get('generated_by'),
            'state': ('no result' if r is None
                      else 'changes' if _truthy(r.get('changed')) else 'no changes'),
            'commands': commands if isinstance(commands, list) else None,
            'regenerated': changed_since,
            'config_missing': bool(name) and current is None,
            'serials': (inventory.get(host) or {}).get('serials') or [],
            'model': (inventory.get(host) or {}).get('model'),
        })

    order = {'changes': 0, 'no result': 1, 'no changes': 2}
    switches.sort(key=lambda s: (order[s['state']], s['hostname'] or ''))
    settings = _load_mapping(app, owner, project).get('settings') or {}
    return {
        'entry': e,
        'owner': owner,
        'project': project,
        'switches': switches,
        'regenerated': regenerated,
        'with_changes': [s for s in switches if s['state'] == 'changes'],
        'commands_dropped': any(s['state'] == 'changes' and s['commands'] is None
                                for s in switches),
        'rollback_minutes': settings.get('rollback_timeout', 5),
        'detail_days': audit.AUDIT_DETAIL_DAYS,
    }


def as_text(s, ref='', notes=''):
    """The summary as plain text for pasting into a ticket."""
    e = s['entry']
    when = e['started_at'].replace('T', ' ').replace('Z', ' UTC')
    out = ['CHANGE SUMMARY: %s / %s' % (s['owner'], s['project']), '']
    if ref:
        out.append('Change reference: %s' % ref)
    out += ['Dry run:           %s by %s' % (when, e['username']),
            'Switch account:    %s' % (e['switch_user'] or '-'),
            'Switches:          %d, %d with changes' % (len(s['switches']), len(s['with_changes'])),
            '']
    if notes:
        out += ['Notes:', notes, '']
    if s['regenerated']:
        out += ['WARNING: configs regenerated since this dry run: %s.' % ', '.join(s['regenerated']),
                'The commands below are not what a push would now send. Run a new dry run.', '']
    out += ['BACKOUT',
            'NetForgeUI checkpoints each switch before pushing. If the change is not',
            'confirmed within %s minutes the switch rolls back to the checkpoint' % s['rollback_minutes'],
            'automatically.', '']
    for sw in s['switches']:
        out.append('=' * 72)
        out.append('%s  (%s)  %s' % (sw['hostname'], sw['mgmt_ip'] or 'no address', sw['state'].upper()))
        if sw['serials']:
            out.append('Hardware: %s  serial %s' % (sw['model'] or '-', ', '.join(sw['serials'])))
        out.append('Config: %s  sha256 %s' % (sw['config_file'] or '-', sw['sha256'] or '-'))
        g = sw['generated_by']
        if g:
            out.append('Generated: %s by %s' % (g['started_at'].replace('T', ' ').replace('Z', ' UTC'),
                                                g['username']))
        if sw['regenerated']:
            out.append('!! Regenerated since this dry run')
        if sw['state'] == 'changes':
            out.append('')
            if sw['commands'] is None:
                out.append('(Commands no longer kept: the audit log drops them after %d days.)'
                           % s['detail_days'])
            else:
                out += ['  ' + c for c in sw['commands']] or ['  (no commands listed)']
        out.append('')
    out.append('Secrets in commands are masked.')
    return '\n'.join(out) + '\n'
