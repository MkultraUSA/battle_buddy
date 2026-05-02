from flask import Blueprint, request, jsonify
from modules.talk import _bot_reply

bp = Blueprint('bot', __name__)


@bp.route("/bot/talk", methods=["POST"])
def bot_talk():
    data = request.get_json(force=True)
    user = data.get("user", "unknown")
    msg = data.get("msg", "")
    if not user or not msg:
        return jsonify({"error": "missing user/msg"}), 400
    _bot_reply(user, msg)
    return jsonify({"status": "ok"}), 200
