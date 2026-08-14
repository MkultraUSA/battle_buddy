#!/usr/bin/env python3
"""
KG Integration Module — Real-time ingestion bridge from Battle Buddy's live
pipeline into the Knowledge Graph (BattleBuddyKG).

HOOK POINTS IN PRODUCTION CODEBASE (kevcloud /opt/battlebuddy/):
================================================================

1) kg_write_call() — Hook AFTER insert_call() returns a call_id.
   File: audio_receiver.py
   Location: Inside the `process()` nested function (launched as daemon thread
            from the /receive route handler), immediately after this line:

        call_id = insert_call(ts, tgid, tag, category, node, duration,
                              transcript, lat, lon, location, coords_approx,
                              accuracy)

   Add:  kg_write_call(call)   # where 'call' is the dict built on the next line

   Also hook in /api/backlog/complete (same pattern, same insert_call call)
   and /test_call route.

   Rationale: At this point we have call_id, ts, tgid, tag, category,
   transcript, lat, lon, location — everything needed to build a full Call
   node + relationships.

--------------------------------------------------------------------

2) kg_write_incident() — Hook INSIDE _create_incident() after DB commit.
   File: modules/incident_engine.py
   Function: _create_incident(itype, desc, call, ts)
   Location: After the `conn.commit()` / `conn.close()` block and after
             `_active_incidents[inc_id]` is populated, before the print()
             statement and alert threads.

   Add:  from modules.kg_integration import kg_write_incident
         kg_write_incident({
             "id": inc_id,
             "itype": itype,
             "description": desc,
             "location": call.get("location"),
             "lat": call.get("lat"),
             "lon": call.get("lon"),
             "ts_start": ts,
             "agencies": [cat],
             "status": "active",
         })

   Rationale: We have the fresh incident ID, type, description, location,
   agencies — everything for an Incident node.

--------------------------------------------------------------------

3) kg_write_transcript() — Optional explicit hook if you want transcript
   text linked as a first-class property or separate entity on the Call node.
   In practice, transcript text is already written inside kg_write_call().
   Use this standalone function if you get corrected/enhanced transcripts
   later in the pipeline (e.g., LLM cleanup pass).

   Suggested hook: after llm_analyze() returns, if the LLM produces a
   cleaned/summarized transcript, call kg_write_transcript(call_id, clean).
================================================================
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

from modules.kg_ontology import BattleBuddyKG

logger = logging.getLogger("kg_integration")

# ---------------------------------------------------------------------------
# Singleton KG instance + thread-safe lock
# ---------------------------------------------------------------------------
_kg_instance: Optional[BattleBuddyKG] = None
_kg_lock = threading.Lock()
_kg_init_lock = threading.Lock()


def _get_kg() -> BattleBuddyKG:
    """Return the singleton BattleBuddyKG instance (thread-safe lazy init)."""
    global _kg_instance
    if _kg_instance is not None:
        return _kg_instance
    with _kg_init_lock:
        # Double-check locking
        if _kg_instance is None:
            _kg_instance = BattleBuddyKG(
                db_path="battle_knowledge.db",
                graph_path="battle_kg.gexf",
            )
            logger.info("BattleBuddyKG initialized (singleton)")
        return _kg_instance


# ---------------------------------------------------------------------------
# Duplicate detection helpers
# ---------------------------------------------------------------------------


def _node_exists(node_id: str) -> bool:
    """Check if a node with the given ID already exists in the KG."""
    kg = _get_kg()
    return kg.get_node(node_id) is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def kg_write_call(call_data: Dict[str, Any]) -> bool:
    """Write a Call node and its relationships into the Knowledge Graph.

    Expected keys in call_data:
        id          – int/str  unique call identifier (from insert_call)
        ts          – float    unix timestamp
        tgid        – int      talkgroup ID
        tag         – str      talkgroup display name
        category    – str      agency category (APD, AFD, TCEMS, ...)
        transcript  – str      transcribed text
        duration    – float    clip duration in seconds
        lat         – float    latitude (optional)
        lon         – float    longitude (optional)
        location    – str      human-readable location string (optional)

    Creates / updates:
        Call node       – ID: "call:<call_id>"
        Talkgroup node  – ID: "tg:<tgid>"  (if not yet present)
        Agency node     – ID: "agency:<category>"  (if not yet present)
    Relationships:
        Call FROM Talkgroup
        Call BY Agency
    Returns True on success, False on error.
    """
    try:
        call_id = call_data.get("id")
        if call_id is None:
            logger.warning("kg_write_call: missing call id, skipping")
            return False

        node_id = f"call:{call_id}"

        # Duplicate detection — skip if we've already ingested this call
        with _kg_lock:
            if _node_exists(node_id):
                logger.debug("kg_write_call: duplicate call %s, skipping", call_id)
                return True  # Not an error — already present

        kg = _get_kg()

        # Build properties dict for the Call node
        props = {
            "ts": call_data.get("ts", time.time()),
            "tgid": call_data.get("tgid"),
            "tag": call_data.get("tag", ""),
            "category": call_data.get("category", "Unknown"),
            "transcript": (call_data.get("transcript") or "")[:5000],  # Truncate huge transcripts
            "duration": call_data.get("duration"),
            "lat": call_data.get("lat"),
            "lon": call_data.get("lon"),
            "location": call_data.get("location"),
            "source": "live_pipeline",
            "created_at": time.time(),
        }

        # Strip None values — NetworkX doesn't allow them as node attributes
        props = {k: v for k, v in props.items() if v is not None}

        with _kg_lock:
            kg.add_node(node_id, label="Call", properties=props)

            # Link to Talkgroup
            tgid = call_data.get("tgid")
            if tgid is not None:
                tg_node_id = f"tg:{tgid}"
                if not _node_exists(tg_node_id):
                    tg_props = {
                        "tgid": tgid,
                        "name": call_data.get("tag", f"TGID {tgid}"),
                        "agency": call_data.get("category", "Unknown"),
                        "created_at": time.time(),
                    }
                    tg_props = {k: v for k, v in tg_props.items() if v is not None}
                    kg.add_node(tg_node_id, label="Talkgroup", properties=tg_props)
                kg.add_relationship(node_id, tg_node_id, "FROM", {"source": "live_pipeline"})

            # Link to Agency
            category = call_data.get("category")
            if category and category != "Unknown":
                agency_node_id = f"agency:{category}"
                if not _node_exists(agency_node_id):
                    agency_props = {
                        "id": category,
                        "name": category,
                        "type": "public_safety",
                        "jurisdiction": "Austin-Travis",
                        "created_at": time.time(),
                    }
                    agency_props = {k: v for k, v in agency_props.items() if v is not None}
                    kg.add_node(agency_node_id, label="Agency", properties=agency_props)
                kg.add_relationship(node_id, agency_node_id, "BY", {"source": "live_pipeline"})

            kg.save()

        logger.info(
            "kg_write_call: wrote call %s (tg:%s %s)", call_id, tgid, call_data.get("tag", "")
        )
        return True

    except Exception as e:
        logger.error("kg_write_call: FAILED for call %s: %s", call_data.get("id"), e, exc_info=True)
        return False


def kg_write_incident(incident_data: Dict[str, Any]) -> bool:
    """Write an Incident node into the Knowledge Graph.

    Expected keys in incident_data:
        id          – int/str  unique incident identifier (DB primary key)
        itype       – str      incident type (SHOOTING, STRUCTURE FIRE, …)
        description – str      human-readable description
        location    – str      human-readable location (optional)
        lat         – float    latitude (optional)
        lon         – float    longitude (optional)
        ts_start    – float    unix timestamp of first detection
        agencies    – list[str] list of agency names involved
        status      – str      active / cleared

    Creates / updates:
        Incident node  – ID: "incident:<incident_id>"
    Returns True on success, False on error.
    """
    try:
        inc_id = incident_data.get("id")
        if inc_id is None:
            logger.warning("kg_write_incident: missing incident id, skipping")
            return False

        node_id = f"incident:{inc_id}"

        with _kg_lock:
            if _node_exists(node_id):
                # Incident may be getting updated rather than created fresh;
                # still update its properties (e.g., severity upgrade)
                logger.debug("kg_write_incident: updating existing incident %s", inc_id)

        kg = _get_kg()

        # Map itype to severity score (matches incident_engine.ITYPE_SEVERITY)
        severity_map = {
            "CRASH/COLLISION": 1,
            "PEDESTRIAN INCIDENT": 2,
            "FIRE DISPATCH": 2,
            "TRANSIT INCIDENT": 2,
            "DEATH INVESTIGATION": 3,
            "FATAL CRASH": 4,
            "SHOOTING": 5,
            "STABBING": 5,
            "WEAPONS": 5,
            "STRUCTURE FIRE": 5,
            "HAZMAT": 5,
            "OFFICER DOWN": 6,
            "MASS CASUALTY": 6,
            "HOSTAGE/BARRICADE": 6,
            "AIRCRAFT EMERGENCY": 6,
            "MULTI-AGENCY RESPONSE": 4,
            "AIR ASSET ACTIVE": 3,
            "DPS CAPITOL ACTIVATION": 4,
            "APD SURGE": 4,
            "AIRPORT EMERGENCY": 4,
            "EMS DISPATCH": 2,
        }

        itype = incident_data.get("itype", "UNKNOWN")
        props = {
            "ts_start": incident_data.get("ts_start", time.time()),
            "ts_updated": time.time(),
            "itype": itype,
            "description": incident_data.get("description", ""),
            "location": incident_data.get("location"),
            "lat": incident_data.get("lat"),
            "lon": incident_data.get("lon"),
            "status": incident_data.get("status", "active"),
            "severity": severity_map.get(itype, 0),
            "confidence": 0.8,  # Detected by rule engine / LLM
            "agencies": ",".join(incident_data.get("agencies", [])),
            "source": "live_pipeline",
            "created_at": time.time(),
        }

        props = {k: v for k, v in props.items() if v is not None}

        with _kg_lock:
            kg.add_node(node_id, label="Incident", properties=props)

            # Link involved agencies
            for agency_name in incident_data.get("agencies", []):
                if not agency_name:
                    continue
                agency_node_id = f"agency:{agency_name}"
                if not _node_exists(agency_node_id):
                    agency_props = {
                        "id": agency_name,
                        "name": agency_name,
                        "type": "public_safety",
                        "jurisdiction": "Austin-Travis",
                        "created_at": time.time(),
                    }
                    agency_props = {k: v for k, v in agency_props.items() if v is not None}
                    kg.add_node(agency_node_id, label="Agency", properties=agency_props)
                kg.add_relationship(
                    node_id, agency_node_id, "INVOLVED", {"source": "live_pipeline"}
                )

            kg.save()

        logger.info(
            "kg_write_incident: wrote incident %s (%s) @ %s",
            inc_id,
            itype,
            incident_data.get("location", "unknown"),
        )
        return True

    except Exception as e:
        logger.error(
            "kg_write_incident: FAILED for incident %s: %s",
            incident_data.get("id"),
            e,
            exc_info=True,
        )
        return False


def kg_write_transcript(call_id: int, transcript_text: str) -> bool:
    """Link or update transcript text on an existing Call node.

    Use this when a corrected or enhanced transcript becomes available
    after the initial write (e.g., LLM post-processing).

    Args:
        call_id:         The numeric call ID (from insert_call)
        transcript_text: The (possibly updated) transcript string

    Returns True on success, False on error.
    """
    try:
        if not call_id:
            logger.warning("kg_write_transcript: missing call_id")
            return False

        node_id = f"call:{call_id}"
        kg = _get_kg()

        with _kg_lock:
            existing = kg.get_node(node_id)
            if existing is None:
                logger.debug(
                    "kg_write_transcript: call node %s not found yet; "
                    "will be picked up on next kg_write_call()",
                    call_id,
                )
                # Don't create a bare Call node here — let kg_write_call do the
                # full ingest.  Stash nothing.
                return True

            # Update the transcript property on the existing node
            props = dict(existing)
            props["transcript"] = (transcript_text or "")[:5000]
            props["updated_at"] = time.time()

            kg.add_node(node_id, label="Call", properties=props)
            kg.save()

        logger.info(
            "kg_write_transcript: updated transcript on call %s (%d chars)",
            call_id,
            len(transcript_text or ""),
        )
        return True

    except Exception as e:
        logger.error("kg_write_transcript: FAILED for call %s: %s", call_id, e, exc_info=True)
        return False


def kg_link_call_to_incident(call_id: int, incident_id: int) -> bool:
    """Create a PART_OF relationship between a Call and an Incident.

    This should be called after analyze_for_incident has linked a call
    to an incident (i.e., after INSERT INTO incident_calls ...).

    Args:
        call_id:     Numeric call ID
        incident_id: Numeric incident ID

    Returns True on success, False on error.
    """
    try:
        call_node = f"call:{call_id}"
        inc_node = f"incident:{incident_id}"
        kg = _get_kg()

        with _kg_lock:
            # Ensure both ends exist (they should — kg_write_call / kg_write_incident
            # should have run already).  If not, silently skip.
            if not _node_exists(call_node):
                logger.debug("kg_link_call_to_incident: call %s not in KG", call_id)
            if not _node_exists(inc_node):
                logger.debug("kg_link_call_to_incident: incident %s not in KG", incident_id)

            kg.add_relationship(
                call_node,
                inc_node,
                "PART_OF",
                {"source": "live_pipeline", "created_at": time.time()},
            )
            kg.save()

        logger.info(
            "kg_link_call_to_incident: call %s → PART_OF → incident %s", call_id, incident_id
        )
        return True

    except Exception as e:
        logger.error("kg_link_call_to_incident: FAILED: %s", e, exc_info=True)
        return False
