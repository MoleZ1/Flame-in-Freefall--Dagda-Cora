from flask import Flask
from pymongo import MongoClient

from .config import Config


def create_app(config=Config):
    app = Flask(__name__)
    app.config.from_object(config)
    if not app.config.get("MAX_CONTENT_LENGTH"):
        app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # insertion.js allows several files of up to 50 MB each

    # One MongoDB client for the whole app
    client = MongoClient(app.config["MONGO_URI"])
    app.extensions["db"] = client[app.config["MONGO_DB"]]

    # Recommendation engine: created ONCE (loads the database and the embeddings index)
    try:
        from motor_psi import Motor
        # actualizar_indice=True: embeds only records that are not in the index yet (none on a normal start),
        # so the app also starts after records were added or removed directly in MongoDB.
        app.extensions["motor"] = Motor(actualizar_indice=True)
        app.extensions["motor_error"] = None
    except Exception as e:
        app.extensions["motor"] = None
        app.extensions["motor_error"] = f"{type(e).__name__}: {e}"
        app.logger.error("motor_psi did not load: %s", e)

    from .pages import pages
    app.register_blueprint(pages)

    from .chat_api import chat_api
    app.register_blueprint(chat_api)

    from .investigations_api import investigations_api
    app.register_blueprint(investigations_api)

    from .assistant_api import assistant_api
    app.register_blueprint(assistant_api)

    from .dashboard_api import dashboard_api
    app.register_blueprint(dashboard_api)

    return app
