from flask import Flask
from flask_login import LoginManager
from .models import User, init_db
from config import Config


login_manager = LoginManager()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to continue.'
    login_manager.login_message_category = 'info'

    @login_manager.user_loader
    def load_user(user_id):
        return User.get(int(user_id), app)

    init_db(app)

    @app.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    # Register blueprints
    from .auth     import auth_bp
    from .hosts    import hosts_bp
    from .hostvars import hostvars_bp
    from .generate import generate_bp
    from .admin    import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(hosts_bp)
    app.register_blueprint(hostvars_bp)
    app.register_blueprint(generate_bp)
    app.register_blueprint(admin_bp)

    return app
