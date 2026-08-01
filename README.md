# Flibler build server

A server-side alternative to the local qUnleashed assembler: it accepts a link to a Bundle.zip from the Flipper Apps catalog, builds a `.fap` file with uFBT support, and returns the client output.

```bash
sudo useradd -r -m -d /opt/flibler-server flibler
sudo -u flibler bash -c '
  cd /opt/flibler-server
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
  cp .env.example .env
'
sudo mkdir -p /var/lib/flibler-server && sudo chown flibler: /var/lib/flibler-server
sudo nano /opt/flibler-server/.env        # задать QU_SHARED_SECRET
sudo cp systemd/flibler-server.service /etc/systemd/system/
sudo systemctl enable --now flibler-server
```# flibler-server
