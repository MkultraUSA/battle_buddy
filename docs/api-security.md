# API Exposure and Route Security

Battle Buddy has both public read-only routes and private control/ingest routes. Do not expose the full Flask application directly to the public internet without a reverse proxy, TLS, and route-level access controls.

## Recommended Exposure Model

Use Nginx, Caddy, Apache, or another reverse proxy as the public edge. Publish only the routes needed by your dashboard or demo site. Keep ingestion, bot, command, and admin-style routes on a private network, VPN, WireGuard tunnel, or IP allowlist.

Suggested model:

```text
Internet
  |
  v
Reverse proxy with HTTPS
  |
  +--> public read-only API routes only
  |
  +--> private routes restricted by IP allowlist, VPN, or auth token
```

## Route Classes

| Route | Suggested Exposure | Reason |
| --- | --- | --- |
| `/api/incidents` | Public or restricted | Read-only incident list; review privacy before publishing |
| `/api/incidents/active` | Public or restricted | Read-only active incidents; avoid publishing sensitive live tactical details |
| `/api/sitrep` | Public or restricted | Read-only summary; ensure confidence wording is clear |
| `/api/daily_summary` | Public or restricted | Read-only aggregate summary |
| `/api/shooting_intel` | Restricted by default | May contain transcript evidence and sensitive incident details |
| `/api/calls` | Restricted by default | Can expose raw transcript text and operational metadata |
| `/api/tgid_guesses` | Restricted by default | Can expose inferred talkgroup analysis |
| `/metrics` | Private only | Prometheus metrics should usually be localhost, VPN, or monitoring-network only |
| `/receive` | Private only | Audio ingest route; should be accepted only from trusted capture nodes |
| `/watchdog_event` | Private only | Operational alert route from capture node watchdogs |
| `/pi/commands` | Private only | Command queue for capture nodes; do not publish publicly |
| `/test_call` | Development/private only | Injects synthetic calls and can create false incidents |
| `/api/tgid_guesses/confirm` | Admin/private only | Writes talkgroup confirmation data |
| `/bot/talk` | Private or signed webhook only | Chat bot webhook; should validate a shared secret |
| `/api/drone_sighting` | Private or signed ingest only | Accepts external sighting data |

## Authentication Guidance

At minimum, protect mutating or ingest routes with one of these controls:

- private network/VPN only,
- reverse-proxy IP allowlist,
- shared bearer token,
- HMAC-signed request body,
- mTLS for capture nodes,
- Nextcloud Talk bot shared secret validation for bot routes.

For public deployments, prefer defense in depth: reverse-proxy allowlisting plus application-level shared-token or HMAC validation.

## Nginx Sketch

This example exposes a few read-only dashboard routes and blocks private routes at the edge. Adjust it for your own deployment.

```nginx
location /api/incidents {
    proxy_pass http://127.0.0.1:9001;
}

location /api/incidents/active {
    proxy_pass http://127.0.0.1:9001;
}

location /api/sitrep {
    proxy_pass http://127.0.0.1:9001;
}

location /metrics {
    allow 127.0.0.1;
    allow 10.0.0.0/8;
    deny all;
    proxy_pass http://127.0.0.1:9001;
}

location ~ ^/(receive|watchdog_event|pi/commands|test_call|bot/talk) {
    allow 10.0.0.0/8;
    deny all;
    proxy_pass http://127.0.0.1:9001;
}
```

## TLS Verification

TLS verification should remain enabled by default. The `ALLOW_INSECURE_TLS=true` setting exists only for isolated lab systems with self-signed certificates or broken local certificate chains. Do not enable it for public internet connections, production deployments, or third-party API providers.

If a local Nextcloud or lab service has certificate problems, fix the certificate trust chain first. Use insecure TLS only as a temporary diagnostic workaround.

## Public Safety / Privacy Notes

Before publishing any endpoint publicly:

- remove personally identifying information where possible,
- avoid publishing live tactical details that could interfere with public safety operations,
- distinguish confirmed reports from AI-classified or inferred events,
- clearly label confidence levels,
- avoid implying official agency affiliation.
