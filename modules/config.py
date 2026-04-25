import os

# Database
DB_PATH = "/opt/data/workspace/battle_buddy/incidents.db"
FTS_HOST = "localhost"
FTS_COT_PORT = 8087
FTS_ENABLED = True
MULTIAGENCY_WINDOW_MIN = 15
HOLD_ENABLED = True
APD_SURGE_WINDOW_MIN = 30
APD_SURGE_THRESHOLD = 5
HOLD_RELEASE_MINUTES = 30
PI1_OP25_URL = "http://radiodesk.ddns.net:8080/"

# Incident types and severities
ITYPE_MERGE_COMPAT = {}
ITYPE_SEVERITY = {}
INCIDENT_TIMEOUT_MINUTES = {}
_INCIDENT_TIMEOUT_DEFAULT = 60

# ATAK constants
_BB_SA_UID  = "BATTLEBUDDY-SERVER"
_BB_SA_XML  = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<event version='2.0' uid='{uid}' type='t-x-c-t' "
    "time='{t}' start='{t}' stale='{s}' how='m-g'>"
    "<point lat='0.0' lon='0.0' hae='0.0' ce='9999999.0' le='9999999.0'/>"
    "<detail>"
    "<contact callsign='BattleBuddy'/>"
    "<remarks>Austin P25 AI Monitor</remarks>"
    "</detail>"
    "</event>"
)

# Incident Profile mapping
_FEMA_FIRE_ICON      = "f8f7f666-8b28-4b57-9fbb-e48e61d33b79/Iron Sites/Fire Incident.png"
_GEOOPS_FIRE_ICON    = "83198b4872a8c34eb9c549da8a4de5a28f07821185b39a2277948f66c24ac17a/WildFire/Fire Location.png"
_RESPONDER_EMS_ICON  = "de450cbf-2ffc-47fb-bd2b-ba2db89b035e/Incident/EMS--Plain.png"

_COT_PROFILE = {
    "SHOOTING":           ("a-h-G",          -65536,       60,  None),
    # ... fill with rest from audio_receiver.py
}
_COT_DEFAULT = ("b-m-p-s-p-i", -8355712, 30, None)
