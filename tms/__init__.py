"""TransportationOne TMS prototype (CW2): application factory."""
import os
from datetime import timedelta

from flask import Flask

from .db import close_db, init_db


def create_app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="prototype-only-change-me",
        DATABASE=os.path.join(root, "instance", "tms.sqlite"),
        UPLOAD_DIR=os.path.join(root, "instance", "uploads"),
        SAMPLE_DIR=os.path.join(root, "samples"),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),  # NFR-03: session closes after 15 minutes
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,                # NFR-02: documents up to 10 MB
    )
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    init_db(app)
    app.teardown_appcontext(close_db)
    from .web import bp
    app.register_blueprint(bp)
    return app
