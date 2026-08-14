#!/usr/bin/env python3
"""
Battle Buddy Overwatch - Lightweight Knowledge Graph Ontology
Follows DESIGN.md: Knowledge graph as single source of truth.
Core entities: Incident, Call, Talkgroup, Agency, Pattern, Entity.
Uses NetworkX + SQLite persistence (Neo4j Community too heavy for current stack).
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

import networkx as nx


class BattleBuddyKG:
    """Lightweight knowledge graph for Battle Buddy Overwatch."""

    def __init__(self, db_path: str = "battle_knowledge.db", graph_path: str = "battle_kg.gexf"):
        self.db_path = Path(db_path)
        self.graph_path = Path(graph_path)
        self.G = nx.MultiDiGraph()
        self.conn = None
        self.init_db()
        self.load_graph()

    def init_db(self):
        """Initialize SQLite persistence for nodes/edges metadata."""
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                properties TEXT,
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                source TEXT,
                target TEXT,
                type TEXT NOT NULL,
                properties TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
            CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
        """)
        self.conn.commit()

    def load_graph(self):
        """Load graph from GEXF or rebuild from DB."""
        if self.graph_path.exists():
            try:
                st = self.graph_path.stat()
                if st.st_size > 0:
                    self.G = nx.read_gexf(str(self.graph_path))
                    print(f"Loaded graph with {self.G.number_of_nodes()} nodes")
                    return
            except Exception as e:
                print(f"Failed to load GEXF ({e}), rebuilding from DB...")
        self._rebuild_from_db()

    def _rebuild_from_db(self):
        """Rebuild NetworkX graph from SQLite metadata."""
        for row in self.conn.execute("SELECT * FROM nodes"):
            props = json.loads(row["properties"] or "{}")
            # Remove label/type from props to avoid duplicate keyword argument
            # (label is passed from the column, type may conflict)
            props.pop("label", None)
            self.G.add_node(row["id"], label=row["label"], **props)

        for row in self.conn.execute("SELECT * FROM edges"):
            props = json.loads(row["properties"] or "{}")
            # Remove type from props to avoid duplicate keyword argument
            props.pop("type", None)
            self.G.add_edge(row["source"], row["target"], key=row["id"], type=row["type"], **props)
        print(f"Rebuilt graph: {self.G.number_of_nodes()} nodes, {self.G.number_of_edges()} edges")

    def save(self):
        """Persist graph and metadata."""
        nx.write_gexf(self.G, str(self.graph_path))
        self.conn.commit()
        print(f"Saved KG to {self.graph_path} ({self.G.number_of_nodes()} nodes)")

    def add_node(self, node_id: str, label: str, properties: Dict = None) -> str:
        """Add or update a node."""
        if properties is None:
            properties = {}
        properties["label"] = label
        properties["updated_at"] = time.time()

        self.G.add_node(node_id, **properties)

        node_data = {
            "id": node_id,
            "label": label,
            "properties": json.dumps(properties),
            "created_at": properties.get("created_at", time.time()),
            "updated_at": properties["updated_at"],
        }
        self.conn.execute(
            """
            INSERT OR REPLACE INTO nodes (id, label, properties, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                node_id,
                label,
                node_data["properties"],
                node_data["created_at"],
                node_data["updated_at"],
            ),
        )
        return node_id

    def add_relationship(
        self, source: str, target: str, rel_type: str, properties: Dict = None
    ) -> str:
        """Add directed relationship (edge)."""
        if properties is None:
            properties = {}
        edge_id = f"{source}_{rel_type}_{target}_{int(time.time())}"
        properties["created_at"] = time.time()
        properties["type"] = rel_type

        self.G.add_edge(source, target, key=edge_id, **properties)

        self.conn.execute(
            """
            INSERT INTO edges (id, source, target, type, properties, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (edge_id, source, target, rel_type, json.dumps(properties), properties["created_at"]),
        )
        return edge_id

    def get_node(self, node_id: str) -> Optional[Dict]:
        """Get node with properties."""
        if node_id in self.G:
            data = dict(self.G.nodes[node_id])
            data["id"] = node_id
            return data
        return None


# === ONTOLOGY DEFINITION ===
ONTOLOGY = {
    "entities": {
        "Incident": {
            "properties": [
                "id",
                "ts_start",
                "ts_updated",
                "ts_cleared",
                "itype",
                "description",
                "location",
                "lat",
                "lon",
                "status",
                "confidence",
                "severity",
            ],
            "description": "Clustered real-world event synthesized from calls",
        },
        "Call": {
            "properties": [
                "id",
                "ts",
                "tgid",
                "tag",
                "transcript",
                "duration",
                "lat",
                "lon",
                "location",
            ],
            "description": "Individual radio transmission (P25 call)",
        },
        "Talkgroup": {
            "properties": ["tgid", "name", "agency", "category", "description", "confidence"],
            "description": "Radio talkgroup/channel identifier",
        },
        "Agency": {
            "properties": ["id", "name", "type", "jurisdiction", "color"],
            "description": "Law enforcement, fire, EMS, etc.",
        },
        "Pattern": {
            "properties": [
                "id",
                "name",
                "description",
                "itype",
                "tgids",
                "confidence",
                "first_seen",
            ],
            "description": "Recurring behavioral or linguistic pattern (e.g. '10-33', 'shots fired')",
        },
        "Entity": {
            "properties": ["id", "name", "type", "mentions", "first_seen", "last_seen"],
            "description": "Named entities extracted from transcripts (person, vehicle, location)",
        },
    },
    "relationships": {
        "PART_OF": {
            "from": "Call",
            "to": "Incident",
            "description": "Call contributes to incident",
        },
        "MENTIONS": {"from": "Call", "to": "Entity", "description": "Transcript mentions entity"},
        "INVOLVED": {
            "from": "Incident",
            "to": "Agency",
            "description": "Agency involved in incident",
        },
        "USES": {"from": "Agency", "to": "Talkgroup", "description": "Agency uses talkgroup"},
        "MATCHES": {"from": "Call", "to": "Pattern", "description": "Call matches known pattern"},
        "RELATED_TO": {
            "from": "Incident",
            "to": "Pattern",
            "description": "Incident exhibits pattern",
        },
        "LINKED_TO": {
            "from": "Incident",
            "to": "Incident",
            "description": "Related incidents (same event, escalation)",
        },
        "LOCATED_IN": {
            "from": ["Incident", "Entity"],
            "to": "Location",
            "description": "Geospatial link",
        },
    },
    "core_properties": {
        "all": ["id", "created_at", "updated_at", "source", "confidence", "verified"]
    },
}


def create_ontology_diagram() -> str:
    """Return ASCII ontology diagram."""
    return """
Battle Buddy Overwatch Knowledge Graph Ontology
============================================

Core Entities:
  [Incident] --PART_OF--> [Call]
     |                |
     |                +--MENTIONS--> [Entity]
     |
  INVOLVED           MATCHES
     |                |
     v                v
[Agency] <--USES-- [Talkgroup]     [Pattern]
     |                 ^
     +---------------RELATED_TO

Lightweight Implementation:
  - NetworkX MultiDiGraph (in-memory + GEXF persistence)
  - SQLite for node/edge metadata (battle_knowledge.db)
  - Follows DESIGN.md - KG as single source of truth
  - Import script bridges existing SQLite (calls.db)

Properties Examples:
  - Incident: itype, description, severity, status
  - Call: transcript, tgid, ts, location
  - Talkgroup: name, agency, category
  - Pattern: linguistic signatures, frequency, confidence
"""


if __name__ == "__main__":
    print("Battle Buddy KG Ontology")
    print(create_ontology_diagram())
    kg = BattleBuddyKG()
    print("KG initialized successfully.")
    kg.save()
