# zara (Arduino UNO Q, Debian arm64) — remote admin session

You are running as root on zara via Claude Remote Control. This box runs the owner's assistant
stack: inspect first, and explain before restarting services or changing files.

- Code: /opt/zara-assistant (bot.py, mm_forwarder.py, alert_triage.py, briefing.py, tgcaller.py, zara_led.py, live.py, stats.py)
- Secrets: /etc/zara-assistant/env — never print token values.
- Services: zara-assistant, mm-forwarder, alert-triage, tgcaller, zara-led, zara-briefing.timer, prometheus, zara-remote-control
- LED matrix app: /home/arduino/ArduinoApps/zara-status (flash as user arduino: `arduino-app-cli app stop/start user:zara-status`)
- Triage data: /home/assistant/triage/; assistant persona: /home/assistant/assistant/CLAUDE.md
- OpenBao (Vault fork) on 127.0.0.1:8200, raft storage /var/lib/openbao, auto-unseal via openbao-unseal.service
  (unseal key + root token in /root/openbao/init.json — never print). Root CLI token: /root/.vault-token.
  Claude (assistant user) reads secret/claude/* with policy claude-read; its token is renewed by bao-token-renew.timer.
