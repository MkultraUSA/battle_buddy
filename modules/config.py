import os
# Core configuration
DB_PATH = "/opt/battlebuddy/calls.db"
LOCUTION_TGIDS = [1121, 1122, 1155, 1162, 1371, 1377, 1378]
TRANSIT_TGIDS = [1485, 1486]
ABIA_ALERT_TGIDS = [1480, 1481]
ABIA_OPS_TGIDS = [1471, 1472, 1473, 1474]
INCIDENT_KEYWORDS = [
    ("officer down", "OFFICER DOWN"), ("shooting", "SHOOTING"),
    ("stabbing", "STABBING"), ("structure fire", "STRUCTURE FIRE"),
    ("hazmat", "HAZMAT"), ("hostage", "HOSTAGE/BARRICADE"),
    ("crash", "CRASH/COLLISION"), ("fatal crash", "FATAL CRASH")
]
MULTIAGENCY_WINDOW_MIN = 15
APD_SURGE_WINDOW_MIN = 15
APD_SURGE_THRESHOLD = 5
HOLD_ENABLED = False
