#!/usr/bin/env python3
"""
Battle Buddy — Groq Relay
Runs on Pi 5 (residential IP). Accepts analysis requests from the VM,
forwards them to Groq (Cloudflare blocks datacenter IPs), returns JSON.

Listens on port 9002 — not exposed to internet, VM reaches it via LAN or VPN.
"""

import json
import os
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

GROQ_API_KEY = os.environ.get("GROQ_API_KEY",
               "GROQ_API_KEY_REMOVED")
GROQ_MODEL   = "llama-3.3-70b-versatile"
LISTEN_PORT  = 9002

_SYSTEM = """You are the incident detection brain for Battle Buddy, a real-time P25 radio monitoring system covering Austin/Travis County emergency services on the GATRRS trunked system.

Austin agencies: APD (police), AFD (Austin Fire Dept), TCEMS (Travis County EMS), TCFD (Travis County Fire), ABIA (Austin-Bergstrom Airport), TCSO (Travis County Sheriff), DPS (Texas Dept of Public Safety), UTPD (UT Police).

Analyze the radio call transcript and recent context. Respond ONLY with a JSON object — no prose, no markdown.

Required JSON fields:
{
  "incident_type": <one of the values below, or null for routine>,
  "priority": <"HIGH" | "MED" | "NONE">,
  "should_hold": <true | false>,
  "description": <one concise sentence describing the event>,
  "escalation_stage": <"welfare"|"disturbance"|"pursuit"|"weapons"|"backup"|"tactical"|"k9"|"air" or null>,
  "reasoning": <one sentence explaining your decision>
}

Valid incident_type values:
  "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY", "MASS CASUALTY",
  "STRUCTURE FIRE", "HAZMAT", "HOSTAGE/BARRICADE", "CRASH/COLLISION",
  "FIRE DISPATCH", "TRANSIT INCIDENT", "AIRPORT EMERGENCY",
  "MULTI-AGENCY RESPONSE", "APD SURGE", "AIR ASSET ACTIVE", "DPS CAPITOL ACTIVATION"

Priority rules:
  HIGH — active life threat: officer down, shooting, structure fire, mass casualty, hostage/barricade, aircraft emergency
  MED  — significant incident: crash, hazmat, fire dispatch, multi-agency, surge, transit, airport alert
  NONE — routine: traffic stop, medical assist, disturbance, welfare check, normal patrol, minor fender bender

should_hold: true only if the event is actively unfolding and worth continuous monitoring.

Be conservative. Most radio traffic is routine. Only flag genuine emergencies."""


def call_groq(payload: dict) -> dict:
    """Forward analysis request to Groq, return parsed JSON."""
    call     = payload.get("call", {})
    recent   = payload.get("recent_calls", [])
    transcript = call.get("transcript") or ""

    # Allow caller to override the system prompt and user message (TGID identification)
    system_prompt = call.pop("_system_override", None) or _SYSTEM
    user_msg_override = call.pop("_user_msg", None)

    if user_msg_override:
        user_msg = user_msg_override
    else:
        ctx_lines = []
        for rc in recent[-6:]:
            rc_txt = (rc.get("transcript") or "")[:100]
            if rc_txt:
                ctx_lines.append(
                    f"  [{rc.get('category','?')}] "
                    f"{rc.get('tag') or 'TGID '+str(rc.get('tgid','?'))}: {rc_txt}"
                )

        user_msg = (
            f"Agency: {call.get('category','Unknown')}\n"
            f"Talkgroup: {call.get('tag') or 'TGID '+str(call.get('tgid',0))}\n"
            f"Location hint: {call.get('location') or 'unknown'}\n"
            f"Transcript: {transcript}\n\n"
            f"Recent calls (last 15 min):\n"
            + ("\n".join(ctx_lines) if ctx_lines else "  (none)")
        )

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps({
            "model":      GROQ_MODEL,
            "messages":   [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "max_tokens":      300,
            "temperature":     0.1,
            "response_format": {"type": "json_object"},
        }).encode(),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
            "User-Agent":    "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return json.loads(data["choices"][0]["message"]["content"])


class RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)
        try:
            payload = json.loads(body)
            result  = call_groq(payload)
            code    = 200
            resp    = json.dumps(result).encode()
        except Exception as exc:
            code = 500
            resp = json.dumps({"error": str(exc)}).encode()
            print(f"[relay] error: {exc}", flush=True)

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, fmt, *args):
        pass  # suppress default access log noise


if __name__ == "__main__":
    print(f"[groq-relay] listening on :{LISTEN_PORT}  model={GROQ_MODEL}", flush=True)
    HTTPServer(("0.0.0.0", LISTEN_PORT), RelayHandler).serve_forever()
