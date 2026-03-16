from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app, jsonify
from flask_login import login_required, current_user
from .utils import read_hosts, add_host, remove_host, ensure_workspace

hosts_bp = Blueprint('hosts', __name__)


@hosts_bp.route('/hosts')
@login_required
def hosts_page():
    app = current_app._get_current_object()
    ensure_workspace(app, current_user.username)
    hosts = read_hosts(app, current_user.username)
    return render_template('hosts.html', hosts=hosts)


@hosts_bp.route('/hosts/add', methods=['POST'])
@login_required
def add():
    app      = current_app._get_current_object()
    hostname = request.form.get('hostname', '').strip()
    stacking = request.form.get('stacking', 'none')

    if not hostname:
        flash('Hostname is required.', 'error')
        return redirect(url_for('hosts.hosts_page'))

    # Basic validation - alphanumeric, hyphens, underscores only
    import re
    if not re.match(r'^[A-Za-z0-9_\-]+$', hostname):
        flash('Hostname may only contain letters, numbers, hyphens and underscores.', 'error')
        return redirect(url_for('hosts.hosts_page'))

    existing = [h['hostname'].lower() for h in read_hosts(app, current_user.username)]
    if hostname.lower() in existing:
        flash(f'{hostname} already exists.', 'error')
        return redirect(url_for('hosts.hosts_page'))

    add_host(app, current_user.username, hostname, stacking)
    flash(f'{hostname} added successfully.', 'success')
    return redirect(url_for('hosts.hosts_page'))


@hosts_bp.route('/hosts/delete/<hostname>', methods=['POST'])
@login_required
def delete(hostname):
    app = current_app._get_current_object()
    remove_host(app, current_user.username, hostname)
    flash(f'{hostname} removed.', 'success')
    return redirect(url_for('hosts.hosts_page'))
