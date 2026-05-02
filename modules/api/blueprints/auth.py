from flask import Blueprint, request, jsonify, session
from modules.auth import check_password, check_session

bp = Blueprint('auth', __name__)


@bp.route("/api/login", methods=["POST"])
def api_login():
    data = request.json
    if check_password(data.get("password")):
        session["auth"] = True
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "denied"}), 401


@bp.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("auth", None)
    return jsonify({"status": "ok"}), 200


@bp.route("/api/me")
def api_me():
    if check_session(session):
        return jsonify({"auth": True}), 200
    return jsonify({"auth": False}), 401
