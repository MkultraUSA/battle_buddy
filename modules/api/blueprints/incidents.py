from flask import Blueprint, jsonify, request
from modules.database import (
    get_all_incidents, get_active_incidents, flag_incident, get_flagged_incidents
)

bp = Blueprint('incidents', __name__)


@bp.route("/api/incidents")
def api_incidents():
    return jsonify(get_all_incidents(250))


@bp.route("/api/incidents/active")
def api_incidents_active():
    return jsonify(get_active_incidents())


@bp.route("/api/incidents/<int:inc_id>/flag", methods=["POST"])
def api_flag_incident(inc_id):
    data = request.get_json(force=True)
    user_id = data.get("user_id", "anonymous")
    reason = data.get("reason", "").strip()
    if not reason:
        return jsonify({"error": "Reason is required"}), 400
    if flag_incident(inc_id, user_id, reason):
        return jsonify({"status": "ok"}), 200
    return jsonify({"error": "Incident not found"}), 404


@bp.route("/api/incidents/flagged")
def api_flagged_incidents():
    return jsonify(get_flagged_incidents())
