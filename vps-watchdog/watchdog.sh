#!/usr/bin/env bash
# WhatsApp bridge watchdog
# ------------------------
# Runs every ~2 min via systemd timer. Alerts to Telegram when a bridge session
# drops / needs re-auth (whatsmeow logs "Device logged out" and prints a fresh
# QR as "WA_QR_RAW>>>" when the linked device is invalidated) or the service dies.
# Debounced via flag files so each incident alerts once.
#
# Creds come from /root/whatsapp/watchdog/.env:
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
# If either is empty the watchdog still runs and logs to
# /var/log/whatsapp-watchdog.log, but sends nothing (safe placeholder mode).
set -uo pipefail

ENV_FILE=/root/whatsapp/watchdog/.env
STATE=/root/whatsapp/watchdog/state
LOG=/var/log/whatsapp-watchdog.log
mkdir -p "$STATE"

if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

notify() {
  local msg="$1"
  printf '%s ALERT: %s\n' "$(date -Is)" "${msg%%$'\n'*}" >> "$LOG"
  if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then return 0; fi
  curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${msg}" \
    -d "disable_web_page_preview=true" >/dev/null || true
}

check_bridge() {
  local svc="$1" label="$2" logf="$3" dir="$4"

  # 1) service down?
  if ! systemctl is-active --quiet "$svc"; then
    if [ ! -f "$STATE/down_$svc" ]; then
      notify "⚠️ WhatsApp $label: '$svc' servisi DÜŞTÜ (hermes-agent VPS)."
      touch "$STATE/down_$svc"
    fi
    return
  fi
  rm -f "$STATE/down_$svc"

  # 2) session logout / re-auth — scan only NEW log bytes since last run
  [ -f "$logf" ] || return
  local off_file="$STATE/off_$svc" size prev new
  size=$(stat -c%s "$logf")
  # first run: record offset, don't scan history (avoids false positives)
  if [ ! -f "$off_file" ]; then echo "$size" > "$off_file"; return; fi
  prev=$(cat "$off_file" 2>/dev/null || echo 0)
  [ "$size" -lt "$prev" ] && prev=0   # log rotated/truncated
  if [ "$size" -gt "$prev" ]; then
    new=$(tail -c "+$((prev + 1))" "$logf")
    if grep -qE "Device logged out|WA_QR_RAW|Scan this QR" <<<"$new"; then
      if [ ! -f "$STATE/reauth_$svc" ]; then
        notify "🔴 WhatsApp $label oturumu DÜŞTÜ — QR ile yeniden bağlanman lazım.
VPS'te (SSH):
  systemctl stop $svc
  cd $dir && ./whatsapp-bridge     # QR çıkar → telefon > Bağlı Cihazlar > tara → sonra Ctrl-C
  systemctl start $svc"
        touch "$STATE/reauth_$svc"
      fi
    fi
    if grep -qE "Successfully authenticated|Connected to WhatsApp" <<<"$new"; then
      if [ -f "$STATE/reauth_$svc" ]; then
        notify "✅ WhatsApp $label yeniden bağlandı."
        rm -f "$STATE/reauth_$svc"
      fi
    fi
  fi
  echo "$size" > "$off_file"
}

check_bridge whatsapp-bridge  "ana hesap (main)"      /var/log/whatsapp-bridge.log  /root/whatsapp/bridge
check_bridge whatsapp-bridge2 "ikinci hesap (second)" /var/log/whatsapp-bridge2.log /root/whatsapp/bridge2
