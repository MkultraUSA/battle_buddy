from flask import Blueprint, request, jsonify
from modules.llm import handle_intel_submission

bp = Blueprint('intel', __name__)


@bp.route("/api/intel", methods=["POST"])
def api_intel():
    data = request.json
    handle_intel_submission(data)
    return jsonify({"status": "ok"}), 200
