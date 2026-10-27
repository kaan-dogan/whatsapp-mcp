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
      notify "⚠️ WhatsApp $label: '$svc' servisi DÜŞTÜ (ingest-box)."
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

  # 3) stuck disconnected — the process stays "active" but whatsmeow gave up.
  # On 2026-10-26 both bridges hit "Client outdated (405)" and never retried;
  # systemd kept saying active and no logout/QR line was printed, so checks 1
  # and 2 stayed silent for hours. Judge by the LAST connection event in the
  # log, and only alert if it stays bad for DISCONNECT_GRACE seconds (normal
  # 503/EOF blips reconnect within seconds and never match these patterns).
  local last since now
  last=$(tail -c 2000000 "$logf" | grep -aE "Connected to WhatsApp|Client outdated|connect failure|Not connected to WhatsApp" | tail -1)
  now=$(date +%s)
  if [ -n "$last" ] && ! grep -q "Connected to WhatsApp" <<<"$last"; then
    [ -f "$STATE/disc_since_$svc" ] || echo "$now" > "$STATE/disc_since_$svc"
    since=$(cat "$STATE/disc_since_$svc")
    if [ $((now - since)) -ge "${DISCONNECT_GRACE:-600}" ] && [ ! -f "$STATE/disc_alert_$svc" ]; then
      local hint="systemctl restart $svc ile dene."
      grep -q "Client outdated" <<<"$last" && hint="WhatsApp istemciyi eski buluyor (405): whatsmeow güncellenip bridge yeniden derlenmeli."
      notify "🔴 WhatsApp $label BAĞLI DEĞİL — servis ayakta ama WhatsApp'a bağlanamıyor.
Son satır: $(sed 's/\x1b\[[0-9;]*m//g' <<<"$last" | cut -c1-200)
$hint"
      touch "$STATE/disc_alert_$svc"
    fi
  else
    if [ -f "$STATE/disc_alert_$svc" ]; then
      notify "✅ WhatsApp $label yeniden bağlandı."
    fi
    rm -f "$STATE/disc_since_$svc" "$STATE/disc_alert_$svc"
  fi
}

check_bridge whatsapp-bridge  "ana hesap (main)"      /var/log/whatsapp-bridge.log  /root/whatsapp/bridge
check_bridge whatsapp-bridge2 "ikinci hesap (second)" /var/log/whatsapp-bridge2.log /root/whatsapp/bridge2

# 3) search integrity — the quiet failure mode
# ------------------------------------------------
# A bridge can be up, authenticated and writing messages while every
# name-based query still returns nothing: that is exactly what the June 2026
# @lid migration did, and it went unnoticed for weeks because listings kept
# showing names. selfcheck.py asserts the invariant that catches it -- whatever
# a listing shows by name must be findable by that name. Hourly is enough; the
# failure is structural, not intermittent.
# The MCP source dir and its interpreter are discovered rather than hardcoded,
# so a future relocation degrades to "check skipped" instead of "check silently
# passes". SELFCHECK_DIR / SELFCHECK_PY in the .env override the search.
SELFCHECK_EVERY=${SELFCHECK_EVERY:-3600}
SELFCHECK_DIR=${SELFCHECK_DIR:-}
SELFCHECK_PY=${SELFCHECK_PY:-}

find_selfcheck() {
  local d
  if [ -n "$SELFCHECK_DIR" ]; then echo "$SELFCHECK_DIR"; return; fi
  for d in /root/whatsapp/mcp /root/whatsapp/mcp-server /root/whatsapp/whatsapp-mcp-server; do
    [ -f "$d/selfcheck.py" ] && { echo "$d"; return; }
  done
  echo ""
}

find_python() {
  local d="$1" p
  if [ -n "$SELFCHECK_PY" ]; then echo "$SELFCHECK_PY"; return; fi
  for p in "$d/.venv/bin/python" "$d/venv/bin/python" /root/whatsapp/mcp/.venv/bin/python; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
  echo python3
}

check_search_integrity() {
  local dir py
  dir=$(find_selfcheck)
  if [ -z "$dir" ]; then
    if [ ! -f "$STATE/selfcheck_missing" ]; then
      notify "ℹ️ WhatsApp arama kontrolü atlandı: selfcheck.py bulunamadı (SELFCHECK_DIR ayarla)."
      touch "$STATE/selfcheck_missing"
    fi
    return 0
  fi
  rm -f "$STATE/selfcheck_missing"
  py=$(find_python "$dir")

  local last_file="$STATE/selfcheck_last" last now
  now=$(date +%s)
  last=$(cat "$last_file" 2>/dev/null || echo 0)
  [ $((now - last)) -lt "$SELFCHECK_EVERY" ] && return 0
  echo "$now" > "$last_file"

  # Take the account map from the MCP unit itself, so this never drifts from
  # what the server actually reads.
  local accounts
  accounts=$(systemctl show whatsapp-mcp -p Environment --value 2>/dev/null \
             | tr ' ' '\n' | sed -n 's/^WHATSAPP_ACCOUNTS=//p')
  if [ -z "$accounts" ] && [ -f "$dir/.env" ]; then
    accounts=$(sed -n 's/^WHATSAPP_ACCOUNTS=//p' "$dir/.env" | tail -1 | sed 's/^"//; s/"$//')
  fi

  local out rc
  out=$(cd "$dir" && WHATSAPP_ACCOUNTS="$accounts" timeout 120 "$py" selfcheck.py 2>&1)
  rc=$?

  if [ "$rc" -ne 0 ]; then
    if [ ! -f "$STATE/selfcheck_fail" ]; then
      notify "🔍 WhatsApp arama BOZUK — servisler ayakta ama isimle arama sonuç döndürmüyor.

$(grep -E '^\s+(FAIL|ERROR)' <<<"$out" | head -6)

VPS'te: cd $dir && $py selfcheck.py"
      touch "$STATE/selfcheck_fail"
    fi
  elif [ -f "$STATE/selfcheck_fail" ]; then
    notify "✅ WhatsApp arama yeniden çalışıyor."
    rm -f "$STATE/selfcheck_fail"
  fi
}

check_search_integrity
