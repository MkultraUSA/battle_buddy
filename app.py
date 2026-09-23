#!/usr/bin/env python3
"""
Battle Buddy Overwatch — Flask API Server
Exposes Knowledge Graph data as REST endpoints for the dashboard.
"""

import os
from time import time

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from modules.aircraft import aircraft_bp
from modules.kg_ontology import ONTOLOGY, BattleBuddyKG
from modules.maintenance import _kg_prune_loop, prune_kg_calls, set_kg_instance

# ---------------------------------------------------------------------------
# App & KG initialization
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.register_blueprint(aircraft_bp)
CORS(app)  # enabled for development

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "battle_knowledge.db")
GRAPH_PATH = os.path.join(BASE_DIR, "battle_kg.gexf")

kg = BattleBuddyKG(db_path=DB_PATH, graph_path=GRAPH_PATH)
set_kg_instance(kg)  # register for periodic pruning

# ---------------------------------------------------------------------------
# Simple in-memory cache for API responses (60s TTL)
# ---------------------------------------------------------------------------

_CACHE_TTL = 60  # seconds
_api_cache = {}


def _cached_json(endpoint: str, ttl: int = _CACHE_TTL):
    """Decorator: cache JSON responses in memory for `ttl` seconds."""
    def decorator(func):
        from functools import wraps
        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time()
            cached = _api_cache.get(endpoint)
            if cached and (now - cached["ts"]) < ttl:
                return cached["resp"]
            resp = func(*args, **kwargs)
            _api_cache[endpoint] = {"resp": resp, "ts": now}
            return resp
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_node(node_id: str) -> dict:
    """Serialize a single node from the graph into a plain dict."""
    data = dict(kg.G.nodes[node_id])
    data["id"] = node_id
    return data


def _serialize_edge(source: str, target: str, edge_data: dict) -> dict:
    """Serialize a single edge from the graph into a plain dict."""
    return {
        "source": source,
        "target": target,
        "type": edge_data.get("type", "UNKNOWN"),
        "properties": {k: v for k, v in edge_data.items() if k != "type"},
    }


def _json_error(message: str, status_code: int = 400) -> tuple:
    """Return a standardized JSON error response."""
    return jsonify({"error": message, "status": status_code}), status_code


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the Overwatch dashboard at root."""
    return render_template("overwatch_dashboard.html")


@app.route("/adsb")
def adsb_map():
    """Serve the ADS-B aircraft map for Austin area."""
    return render_template("adsb_map.html")


@app.route("/api/adsb/aircraft")
def get_aircraft():
    """Fetch aircraft within a radius of a point from adsb.lol API."""
    import json
    import urllib.request

    lat = request.args.get("lat", 30.2672, type=float)
    lon = request.args.get("lon", -97.7431, type=float)
    radius = min(request.args.get("radius", 100, type=int), 250)

    url = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "ac": [], "total": 0}), 500


# ---------------------------------------------------------------------------
# Knowledge Graph API endpoints
# ---------------------------------------------------------------------------


@app.route("/api/kg/nodes", methods=["GET"])
@_cached_json("nodes")
def get_nodes():
    """Return all nodes (entities) in the knowledge graph."""
    try:
        nodes = [_serialize_node(nid) for nid in kg.G.nodes()]
        return jsonify({"count": len(nodes), "nodes": nodes})
    except Exception as exc:
        return _json_error(f"Failed to retrieve nodes: {exc}", 500)


@app.route("/api/kg/relationships", methods=["GET"])
@_cached_json("relationships")
def get_relationships():
    """Return all edges / relationships in the knowledge graph."""
    try:
        edges = [_serialize_edge(src, tgt, data) for src, tgt, data in kg.G.edges(data=True)]
        return jsonify({"count": len(edges), "relationships": edges})
    except Exception as exc:
        return _json_error(f"Failed to retrieve relationships: {exc}", 500)


@app.route("/api/kg/incidents", methods=["GET"])
@_cached_json("incidents")
def get_incidents():
    """Return incidents enriched with related call and agency data."""
    try:
        incident_nodes = []
        for nid in kg.G.nodes():
            node_data = dict(kg.G.nodes[nid])
            label = node_data.get("label", "").lower()
            itype = node_data.get("itype", "")
            if "incident" in label or itype or "incident" in str(node_data.get("type", "")).lower():
                entry = {"id": nid, **node_data}

                related_calls = []
                for src, tgt, data in kg.G.in_edges(nid, data=True):
                    if data.get("type", "") == "PART_OF":
                        call_data = _serialize_node(src)
                        call_data["relationship"] = "PART_OF"
                        related_calls.append(call_data)

                entry["related_calls"] = related_calls

                agencies = []
                for src, tgt, data in kg.G.out_edges(nid, data=True):
                    if data.get("type", "") == "INVOLVED":
                        agencies.append(_serialize_node(tgt))

                entry["agencies"] = agencies
                incident_nodes.append(entry)

        return jsonify({"count": len(incident_nodes), "incidents": incident_nodes})
    except Exception as exc:
        return _json_error(f"Failed to retrieve incidents: {exc}", 500)


@app.route("/api/kg/filter-options", methods=["GET"])
@_cached_json("filter-options")
def get_filter_options():
    """Return unique agencies and talkgroups for filter UI."""
    try:
        agencies = {}
        talkgroups = {}

        for nid in kg.G.nodes():
            data = dict(kg.G.nodes[nid])
            label = data.get("label", "")

            if label == "Agency":
                agencies[nid] = {
                    "id": nid,
                    "name": data.get("name", nid),
                    "type": data.get("type", "unknown"),
                    "color": data.get("color", "#00B4D8"),
                }

            if label == "Talkgroup":
                talkgroups[nid] = {
                    "id": nid,
                    "name": data.get("name", nid),
                    "agency": data.get("agency", ""),
                    "category": data.get("category", ""),
                    "tgid": data.get("tgid", ""),
                }

        return jsonify({"agencies": list(agencies.values()), "talkgroups": list(talkgroups.values())})
    except Exception as exc:
        return _json_error(f"Failed to retrieve filter options: {exc}", 500)


@app.route("/api/kg/search", methods=["GET"])
def search_nodes():
    """Search nodes by name, type, or description using the 'q' query param."""
    query = request.args.get("q", "").strip()
    if not query:
        return _json_error("Missing required query parameter 'q'", 400)

    query_lower = query.lower()
    results = []

    for nid in kg.G.nodes():
        data = dict(kg.G.nodes[nid])
        searchable_fields = [
            data.get("label", ""),
            data.get("name", ""),
            data.get("description", ""),
            data.get("itype", ""),
            data.get("type", ""),
            str(data.get("tgid", "")),
            nid,
        ]

        for field in searchable_fields:
            if query_lower in str(field).lower():
                results.append(_serialize_node(nid))
                break

    return jsonify({"query": query, "count": len(results), "results": results})


@app.route("/api/kg/stats", methods=["GET"])
@_cached_json("stats")
def get_stats():
    """Return summary statistics about the knowledge graph."""
    try:
        type_counts = {}
        for nid in kg.G.nodes():
            label = kg.G.nodes[nid].get("label", "Unknown")
            type_counts[label] = type_counts.get(label, 0) + 1

        edge_type_counts = {}
        for src, tgt, data in kg.G.edges(data=True):
            etype = data.get("type", "UNKNOWN")
            edge_type_counts[etype] = edge_type_counts.get(etype, 0) + 1

        return jsonify({
            "node_count": kg.G.number_of_nodes(),
            "edge_count": kg.G.number_of_edges(),
            "nodes_by_type": type_counts,
            "edges_by_type": edge_type_counts,
            "ontology_entities": list(ONTOLOGY["entities"].keys()),
        })
    except Exception as exc:
        return _json_error(f"Failed to compute stats: {exc}", 500)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.errorhandler(404)
def not_found(error):
    return _json_error("Resource not found", 404)


@app.errorhandler(405)
def method_not_allowed(error):
    return _json_error("Method not allowed", 405)


@app.errorhandler(500)
def internal_error(error):
    return _json_error("Internal server error", 500)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Battle Buddy Overwatch API starting...")
    print(f"  DB:      {DB_PATH}")
    print(f"  Graph:   {GRAPH_PATH}")
    print(f"  Nodes:   {kg.G.number_of_nodes()}")
    print(f"  Edges:   {kg.G.number_of_edges()}")
    import threading
    threading.Thread(target=_kg_prune_loop, daemon=True).start()
    threading.Thread(target=lambda: (lambda: (set_kg_instance(kg), prune_kg_calls(kg)))(), daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
