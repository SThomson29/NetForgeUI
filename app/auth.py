from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from .models import User
from .utils import ensure_workspace

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('projects.projects_page'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('projects.projects_page'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        user = User.authenticate(username, password, current_app._get_current_object())
        if user:
            login_user(user)
            ensure_workspace(current_app._get_current_object(), user.username)
            return redirect(url_for('projects.projects_page'))
        flash('Invalid username or password.', 'error')

    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw     = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')

        app = current_app._get_current_object()
        user = User.authenticate(current_user.username, current_pw, app)
        if not user:
            flash('Current password is incorrect.', 'error')
        elif new_pw != confirm_pw:
            flash('New passwords do not match.', 'error')
        elif len(new_pw) < 8:
            flash('Password must be at least 8 characters.', 'error')
        else:
            User.change_password(current_user.id, new_pw, app)
            flash('Password changed successfully.', 'success')

    return render_template('account.html')
