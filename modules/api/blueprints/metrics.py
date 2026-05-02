from flask import Blueprint
from prometheus_client import generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
from modules.metrics import _BB_METRICS_REGISTRY

bp = Blueprint('metrics', __name__)

@bp.route("/metrics")
def prometheus_metrics():
    return (
        generate_latest(_BB_METRICS_REGISTRY),
        200,
        {"Content-Type": CONTENT_TYPE_LATEST},
    )
