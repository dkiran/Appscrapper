#!/bin/bash
# Run on instance boot (systemd): start services, run one scrape (+ leads sync).
# Continuous mode (default): leave the instance running so the in-container cron keeps scraping.
# Legacy credit-saving stop: SHUTDOWN_AFTER_RUN=1 (or touch then delete stay-up after a timed wait).
#   touch ~/Appscrapper/stay-up   also keeps the instance up when shutdown mode is enabled
set -u
APP_DIR="${APP_DIR:-/home/ec2-user/Appscrapper}"
KEEP_UP_MINUTES="${KEEP_UP_MINUTES:-60}"          # only used when SHUTDOWN_AFTER_RUN=1
MIN_HOURS_BETWEEN_RUNS="${MIN_HOURS_BETWEEN_RUNS:-20}"   # a manual start inside this window skips the scrape
MAX_RUN_HOURS="${MAX_RUN_HOURS:-12}"
SHUTDOWN_AFTER_RUN="${SHUTDOWN_AFTER_RUN:-0}"     # 0 = continuous (do not stop the instance)
log() { echo "[$(date -u '+%F %T') UTC] $*"; }

cd "$APP_DIR" || { log "cannot cd to $APP_DIR"; exit 1; }
mkdir -p data
MARKER="$APP_DIR/data/.last_scrape"
FILES="-f docker-compose.yml -f docker-compose.hostschedule.yml"
SERVICES="db dashboard"
if grep -q '^DOMAIN=.\+' .env 2>/dev/null; then FILES="$FILES -f docker-compose.https.yml"; SERVICES="$SERVICES caddy"; fi
COMPOSE="docker compose $FILES"

# Write an event line into the same database log the dashboard shows (message must not contain single quotes).
event() {
    $COMPOSE exec -T db psql -U scraper -d scraper -q -c \
        "INSERT INTO logs (ts, level, message) VALUES (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS') || '+00:00', 'INFO', '$1')" \
        >/dev/null 2>&1 || log "could not write event to the database log"
}

until docker info >/dev/null 2>&1; do sleep 3; done
log "starting: $SERVICES"
$COMPOSE up -d $SERVICES
event "INSTANCE STARTED (booted at $(date -u -d "$(uptime -s)" '+%F %T') UTC)"

now=$(date +%s); last=$(cat "$MARKER" 2>/dev/null || echo 0)
if [ $((now - last)) -ge $((MIN_HOURS_BETWEEN_RUNS * 3600)) ]; then
    log "running one scrape (limit ${MAX_RUN_HOURS}h)"
    if timeout "${MAX_RUN_HOURS}h" $COMPOSE run -T --rm scraper python main.py --once; then
        date +%s > "$MARKER"; log "scrape finished"
    else
        log "scrape exited with an error; will be retried on the next start"
    fi
else
    log "last scrape was $(( (now - last) / 3600 )) h ago; skipping (manual start)"
fi

# Push new contacts to SingleQR before the instance stops. The always-on sync_cron
# never fires on this boot→scrape→shutdown schedule, so sync must run here.
if grep -q '^SINGLEQR_LEADS_URL=.\+' .env 2>/dev/null && grep -q '^SINGLEQR_LEADS_API_KEY=.\+' .env 2>/dev/null; then
    log "syncing contacts → SingleQR leads"
    if $COMPOSE run -T --rm scraper python main.py --sync-leads; then
        log "leads sync finished"
    else
        log "leads sync failed; will retry on the next boot (contacts stay in Postgres)"
    fi
else
    log "SINGLEQR_LEADS_URL / SINGLEQR_LEADS_API_KEY not set; skipping leads sync"
fi

if [ "$SHUTDOWN_AFTER_RUN" != "1" ] || [ -e "$APP_DIR/stay-up" ]; then
    log "keeping instance up (continuous scraping; SHUTDOWN_AFTER_RUN=${SHUTDOWN_AFTER_RUN})"
    event "INSTANCE STAYING UP (continuous scrape)"
    # hostschedule.yml puts scraper behind profile "manual"; enable it so the loop keeps running.
    $COMPOSE --profile manual up -d --build scraper 2>/dev/null \
      || $COMPOSE up -d --build scraper 2>/dev/null \
      || true
    exit 0
fi

log "dashboard stays up for $KEEP_UP_MINUTES minutes"
sleep $((KEEP_UP_MINUTES * 60))
while [ -e "$APP_DIR/stay-up" ]; do sleep 60; done
log "stopping containers and shutting down"
event "INSTANCE SHUTTING DOWN (stopping now)"
$COMPOSE stop
shutdown -h now
