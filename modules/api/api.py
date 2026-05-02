from flask import Flask

def create_app():
    app = Flask(__name__, static_folder="/opt/battlebuddy/static", static_url_path="/static")

    from .blueprints import (
        receive, pi_interface, test_call, reports, incidents, 
        stats, summary, drone, adsb, main, bot, auth, commute, 
        intel, premium, metrics
    )
    
    app.register_blueprint(receive.bp)
    app.register_blueprint(pi_interface.bp)
    app.register_blueprint(test_call.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(incidents.bp)
    app.register_blueprint(stats.bp)
    app.register_blueprint(summary.bp)
    app.register_blueprint(drone.bp)
    app.register_blueprint(adsb.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(bot.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(commute.bp)
    app.register_blueprint(intel.bp)
    app.register_blueprint(premium.bp)
    app.register_blueprint(metrics.bp)

    # Registering the already existing blueprints
    from modules.public import public_bp
    app.register_blueprint(public_bp)
    from modules.tips import tips_bp
    app.register_blueprint(tips_bp)

    return app
