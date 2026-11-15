from flask import Flask
from pymongo import MongoClient

from .config import Config


def create_app(config=Config):
    app = Flask(__name__)
    app.config.from_object(config)

    # One MongoDB client for the whole app
    client = MongoClient(app.config["MONGO_URI"])
    app.extensions["db"] = client[app.config["MONGO_DB"]]

    from .pages import pages
    app.register_blueprint(pages)

    return app
