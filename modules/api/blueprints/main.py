from flask import Blueprint, redirect

bp = Blueprint('main', __name__)


@bp.route("/")
def index():
    return redirect("/static/index.html", code=302)
