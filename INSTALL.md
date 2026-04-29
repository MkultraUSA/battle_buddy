# Installation

This guide is written for a clean Linux deployment and a separate development setup. The reference deployment uses a Raspberry Pi capture node and a Linux server/VM, but the paths and hostnames should be adjusted for your environment.

## Requirements

- Linux host, Ubuntu 22.04/24.04 or Debian-based system recommended
- Python 3.10+
- `git`, `python3-venv`, `python3-pip`
- OP25 and an SDR device for live P25 capture, if using the radio pipeline
- Optional: Nginx, Nextcloud Talk/Deck, FreeTAKServer, Prometheus/Grafana

## Clone the Repository

```bash
git clone https://github.com/MkultraUSA/battle_buddy.git
cd battle_buddy
```

## Create a Python Virtual Environment

Do not install directly into the system Python with `--break-system-packages`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For development tools:

```bash
python -m pip install -r requirements-dev.txt
```

## Create Configuration

```bash
cp config.env.example config.env
nano config.env
```

Only put real credentials in `config.env` or in your service manager's environment file. Do not commit it.

Load the environment for a local run:

```bash
set -a
source config.env
set +a
```

## Run in Development Mode

```bash
source .venv/bin/activate
python audio_receiver.py --port 9001 --model base
```

Then test locally:

```bash
curl -s http://127.0.0.1:9001/api/status || true
curl -s http://127.0.0.1:9001/api/calls | head
```

Some routes depend on a populated SQLite database and live capture pipeline, so an empty development instance may return empty data.

## Production Layout

A production install can use this layout:

```text
/opt/battlebuddy              application checkout
/opt/battlebuddy/.venv        Python virtual environment
/var/lib/battlebuddy          runtime database/data, optional future layout
/var/log/battlebuddy          logs, optional future layout
/var/www/battlebuddy          static/public output, optional future layout
```

Example setup:

```bash
sudo mkdir -p /opt/battlebuddy /var/lib/battlebuddy /var/log/battlebuddy /var/www/battlebuddy
sudo chown -R "$USER:$USER" /opt/battlebuddy /var/lib/battlebuddy /var/log/battlebuddy /var/www/battlebuddy
```

## systemd Service Example

Create `/etc/systemd/system/battlebuddy.service`:

```ini
[Unit]
Description=Battle Buddy Flask service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/battlebuddy
EnvironmentFile=/opt/battlebuddy/config.env
ExecStart=/opt/battlebuddy/.venv/bin/python /opt/battlebuddy/audio_receiver.py --port 9001 --model base
Restart=always
RestartSec=5
StartLimitIntervalSec=0
User=battlebuddy
Group=battlebuddy

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now battlebuddy.service
sudo systemctl status battlebuddy.service
```

View logs:

```bash
journalctl -u battlebuddy.service -f
```

## Nginx Reverse Proxy Example

```nginx
server {
    listen 80;
    server_name battlebuddy.example.com;

    location / {
        proxy_pass http://127.0.0.1:9001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Use HTTPS with Certbot or your preferred certificate automation before exposing the service publicly.

## Testing and Linting

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest
```

Tests should not require live radio streams, API keys, real Nextcloud credentials, or production databases.

## Updating

```bash
cd /opt/battlebuddy
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl restart battlebuddy.service
journalctl -u battlebuddy.service -n 100 --no-pager
```

## Troubleshooting

Check service state:

```bash
systemctl status battlebuddy.service
journalctl -u battlebuddy.service -n 200 --no-pager
```

Check port binding:

```bash
ss -tulpn | grep 9001
```

Check configuration variables:

```bash
systemctl show battlebuddy.service -p Environment
```

Run a secret scan before publishing or cutting a release:

```bash
gitleaks detect --source . --verbose
```
