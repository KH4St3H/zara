# zara

A personal assistant running on an Arduino UNO Q (Debian trixie, arm64) on the home network.

| Part | What it does | Code | Service |
|---|---|---|---|
| Telegram bot | Chat with Claude (`claude -p`), live drafts, `/new /cancel /usage /stats /briefing` | `assistant/bot.py`, `live.py`, `tgmd.py`, `stats.py`, `*_widget.html` | `zara-assistant` |
| Mattermost forwarder | Forwards DMs/mentions and alert channels to Telegram; websocket ping + catch-up replay; alert state (firing/mixed/resolved) | `assistant/mm_forwarder.py` | `mm-forwarder` |
| Sensitivity filter | Hides secret-looking text (passwords, tokens, keys, random "gibberish") from Mattermost messages before they reach Telegram or Claude; retrained weekly on words used in your own chats (kept on the board) | `assistant/sensitive.py`, `sensitive_train.py`, `bigram_base.json`, `tech_words.txt` | `sensitive-train.timer` |
| Alert triage | Claude decides whether an alert is worth a phone call; quiet hours 23:00–08:00 Europe/Helsinki; cancels calls on recovery | `assistant/alert_triage.py` | `alert-triage` |
| Telegram caller | Voice calls with Piper TTS (`tg-call`) | `assistant/tgcaller.py`, `tgcall_login.py` | `tgcaller` |
| Morning briefing | 08:00 summary | `assistant/briefing.py` | `zara-briefing.timer` |
| LED matrix | Status on the 8×13 matrix: scrolling time/date/temperature banner, unread count, alerts | `assistant/zara_led.py` (Linux side), `led/` (App Lab app + sketch) | `zara-led` |
| Read-only kubectl | Wrapper used by triage/briefing (read verbs only, no Secrets) | `assistant/bin/kubectl` | – |
| Web page | Public "about zara" page, via Cloudflare Tunnel | `web/index.html` | `zara-web`, `cloudflared` |
| OpenBao | Credentials for Claude under `secret/claude/*` | `openbao/` | `openbao`, `openbao-unseal`, `bao-token-renew.timer` |
| Remote Control | `claude remote-control` as root (`zara`) and as `assistant` (`zara-bot`) | – | `zara-remote-control`, `zara-bot-remote-control` |
| Metrics | Local Prometheus + node_exporter | `prometheus/prometheus.yml` | `prometheus` |

## Where things live on zara

- `assistant/` → `/opt/zara-assistant/` (plus `tgcall-venv/` and `voices/`, not in git)
- `bin/` → `/usr/local/bin/` (`tg-send`, `tg-call`, `grafana-mcp-env`)
- `systemd/` → `/etc/systemd/system/`
- `led/` → `/home/arduino/ArduinoApps/zara-status/` (must be the App Lab default app:
  `arduino-app-cli properties set default user:zara-status`)
- `web/` → `/var/www/zara/`
- `claude/assistant-CLAUDE.md.example` is a template for `/home/assistant/assistant/CLAUDE.md` (the real one stays on zara); `claude/admin-CLAUDE.md` → `/root/zara/CLAUDE.md`
- `openbao/openbao.hcl` → `/etc/openbao/`; `openbao/openbao-unseal` → `/usr/local/sbin/`

## Secrets

None are in this repo. They live on zara only:
- `/etc/zara-assistant/env` (root:assistant 640) — keys listed in `config/env.example`, including site settings (kube contexts, GitLab/Grafana hosts)
- `/etc/cloudflared/token.env`, `/root/openbao/init.json`, `~assistant/.vault-token`, `/var/lib/tgcaller/`

## Deploy

```sh
./deploy.sh            # copies files to zara and restarts the services
```
