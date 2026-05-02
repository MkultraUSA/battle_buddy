from flask import Blueprint, request, jsonify
from modules.commute import save_commute, get_commute_time, get_commute_polyline, get_commute_incidents, get_commute_share_token

bp = Blueprint('commute', __name__)


@bp.route("/api/commute/save", methods=["POST"])
def api_save_commute():
    data = request.get_json(force=True)
    user_id = data.get("user_id", "anonymous")
    home_lat = data.get("home_lat")
    home_lon = data.get("home_lon")
    work_lat = data.get("work_lat")
    work_lon = data.get("work_lon")
    if not all([user_id, home_lat, home_lon, work_lat, work_lon]):
        return jsonify({"error": "Missing required fields"}), 400
    save_commute(user_id, home_lat, home_lon, work_lat, work_lon)
    return jsonify({"status": "ok"}), 200


@bp.route("/api/commute/time", methods=["GET"])
def api_get_commute_time():
    user_id = request.args.get("user_id", "anonymous")
    return jsonify(get_commute_time(user_id))


@bp.route("/api/commute/polyline", methods=["GET"])
def api_get_commute_polyline():
    user_id = request.args.get("user_id", "anonymous")
    return jsonify(get_commute_polyline(user_id))


@bp.route("/api/commute/incidents", methods=["GET"])
def api_get_commute_incidents():
    user_id = request.args.get("user_id", "anonymous")
    return jsonify(get_commute_incidents(user_id))


@bp.route("/api/commute/share_token", methods=["POST"])
def api_get_commute_share_token():
    user_id = request.json.get("user_id", "anonymous")
    return jsonify({"token": get_commute_share_token(user_id)}), 200
