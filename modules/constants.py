"""
Shared constants for Battle Buddy modules.
Moved here to avoid circular imports between audio_receiver.py and modules/.
"""

import os

DB_PATH = "/opt/battlebuddy/calls.db"

# Default map coordinates by category
CAT_COORDS = {
    "APD":          (30.2672, -97.7431),
    "AFD":          (30.2672, -97.7431),
    "TCEMS":        (30.2672, -97.7431),
    "ABIA":         (30.1975, -97.6664),
    "TCSO":         (30.2672, -97.7431),
    "TCFD":         (30.2672, -97.7431),
    "UTPD":         (30.2849, -97.7341),
    "DPS":          (30.2747, -97.7404),   # Texas State Capitol
    "Bastrop":      (30.1107, -97.3154),
    "Burnet":       (30.7488, -98.2345),
    "Comal":        (29.7030, -98.1245),
    "Kerr":         (30.0474, -99.1403),
    "Pflugerville": (30.4394, -97.6200),
    "Lakeway":      (30.3577, -97.9772),
    "TXDOT":        (30.2672, -97.7431),
    "Interop":      (30.2672, -97.7431),
    "Unknown":      (30.2672, -97.7431),
}
