from flask import Blueprint, jsonify, request
from modules.llm import build_daily_summary, confirm_tgid_guess
from modules.database import get_tgid_guesses, confirm_tgid_guess as db_confirm_tgid_guess

bp = Blueprint('summary', __name__)
@bp.route("/api/daily_summary")
def api_daily_summary():
    summary_text = build_daily_summary()
    return jsonify({"summary": summary_text, "ts": summary_text.split('\n')[0]})


@bp.route("/api/tgid_guesses")
def api_tgid_guesses():
    return jsonify(get_tgid_guesses())


@bp.route("/api/tgid_guesses/confirm", methods=["POST"])
def api_confirm_tgid_guess():
    data = request.get_json()
    tgid = data.get("tgid")
    tag = data.get("tag")
    if not tgid or not tag:
        return jsonify({"error": "`tgid` and `tag` are required"}), 400
    try:
        db_confirm_tgid_guess(tgid, tag)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
