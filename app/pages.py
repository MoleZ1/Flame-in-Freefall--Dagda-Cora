from flask import Blueprint, render_template

pages = Blueprint("pages", __name__)


@pages.get("/")
def index():
    return render_template("index.html")


@pages.get("/suggestion")
def suggestion():
    return render_template("suggestion.html")


@pages.get("/insertion")
def insertion():
    return render_template("insertion.html")
