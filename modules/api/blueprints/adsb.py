from flask import Blueprint, jsonify
from modules.database import get_adsb_map_points

bp = Blueprint('adsb', __name__)


@bp.route("/api/adsb")
def api_adsb():
    return jsonify(get_adsb_map_points())
