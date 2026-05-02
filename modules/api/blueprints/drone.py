from flask import Blueprint, request, jsonify
from modules.database import add_drone_sighting, get_drone_sightings

bp = Blueprint('drone', __name__)


@bp.route("/api/drone_sighting", methods=["POST"])
def api_add_drone_sighting():
    data = request.get_json(force=True)
    lat = data.get("lat")
    lon = data.get("lon")
    desc = data.get("description", "")
    if not lat or not lon:
        return jsonify({"error": "lat/lon required"}), 400
    add_drone_sighting(lat, lon, desc)
    return jsonify({"status": "ok"}), 200


@bp.route("/api/drone_sightings")
def api_get_drone_sightings():
    return jsonify(get_drone_sightings())
