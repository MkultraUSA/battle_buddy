from flask import Blueprint, request, jsonify
import threading
from modules.pollers import _pi_command_queue
from modules.talk import _pi_watchdog_alert

bp = Blueprint('pi_interface', __name__)

@bp.route("/watchdog_event", methods=["POST"])
def watchdog_event():
    """Receive Pi watchdog events and forward as Talk DM alerts."""
    data  = request.get_json(force=True)
    event = data.get("event", "unknown")
    msg   = data.get("msg", "")
    icons = {"op25_down": "⚠️", "op25_recovered": "✅", "op25_failed": "🚨"}
    icon  = icons.get(event, "⚠️")
    full  = f"{icon} PI WATCHDOG: {msg}"
    print(f"[pi-watchdog] {full}", flush=True)
    threading.Thread(target=_pi_watchdog_alert, args=(full,), daemon=True).start()
    return jsonify({"status": "ok"}), 200


@bp.route("/pi/commands", methods=["GET"])
def pi_commands():
    """Pi polls this endpoint for pending commands (restart_op25, etc.)."""
    cmds = list(_pi_command_queue)
    _pi_command_queue.clear()
    return jsonify({"commands": cmds}), 200
