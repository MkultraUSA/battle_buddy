from flask import Blueprint, jsonify
from modules.database import get_stats, get_shooting_intel

bp = Blueprint('stats', __name__)


@bp.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@bp.route("/api/shooting_intel")
def api_shooting_intel():
    return jsonify(get_shooting_intel())
