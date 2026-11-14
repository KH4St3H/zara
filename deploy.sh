#!/bin/sh
# Copy this repo's files to zara and restart what changed.
# Usage: ./deploy.sh [host]   (default: $ZARA_HOST, else root@zara.local)
# Secrets stay on zara in /etc/zara-assistant/env and are never touched.
set -eu
H=${1:-${ZARA_HOST:-root@zara.local}}
cd "$(dirname "$0")"
export COPYFILE_DISABLE=1      # no macOS ._ files in the tarballs

# put <local dir>'s contents into <remote dir>, owned by root (or $3)
push() { (cd "$1" && tar cf - --no-xattrs *) | ssh "$H" "tar xf - --no-same-owner --no-overwrite-dir -C '$2'${3:+ && chown -R $3 '$2'}"; }

push assistant /opt/zara-assistant
push bin /usr/local/bin
push systemd /etc/systemd/system
push web /var/www/zara
push led /home/arduino/ArduinoApps/zara-status arduino:arduino
scp -q openbao/openbao-unseal "$H":/usr/local/sbin/openbao-unseal
scp -q prometheus/prometheus.yml "$H":/etc/prometheus/prometheus.yml
scp -q claude/admin-CLAUDE.md "$H":/root/zara/CLAUDE.md

ssh "$H" 'systemctl daemon-reload &&
  systemctl restart zara-assistant mm-forwarder alert-triage tgcaller zara-led zara-web &&
  systemctl reload prometheus 2>/dev/null || true'
echo "deployed. LED sketch changes need: ssh $H \"su arduino -c 'arduino-app-cli app stop user:zara-status; sleep 3; arduino-app-cli app start user:zara-status'\""
