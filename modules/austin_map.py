"""Stub for austin_map blueprint — map display for Austin, TX."""
from flask import Blueprint

austin_bp = Blueprint("austin", __name__, url_prefix="/austin", template_folder="templates")

@austin_bp.route("/")
def austin_map():
    return "Austin map placeholder", 200
