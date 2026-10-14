from flask import Flask
from flask_login import LoginManager
from .models import User, init_db
from config import Config


login_manager = LoginManager()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    from .security import check_secret_key, install_origin_check

    # Refuse to start with a published or trivial key rather than run with
    # sessions anyone can forge. Tests set TESTING and use their own key.
    if not app.config.get('TESTING'):
        check_secret_key(app.config.get('SECRET_KEY'))

    # Behind Nginx every request arrives from the proxy's address, so the
    # login throttle would treat all users as one client and the CSRF check
    # would see the upstream host name. Only trust the forwarded headers
    # when a proxy is actually configured in front.
    proxies = int(app.config.get('TRUSTED_PROXIES') or 0)
    if proxies > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies,
                                x_proto=proxies, x_host=proxies)

    install_origin_check(app)


    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to continue.'
    login_manager.login_message_category = 'info'

    @login_manager.user_loader
    def load_user(user_id):
        return User.from_session_id(user_id, app)

    init_db(app)

    @app.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    # Register blueprints
    from .auth     import auth_bp
    from .admin    import admin_bp
    from .projects import projects_bp
    from .sso      import sso_bp, init_sso

    # Refuses to start if SSO is half configured.
    init_sso(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(sso_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(projects_bp)

    return app
