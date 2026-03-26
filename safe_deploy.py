#!/usr/bin/env python3
"""
Safe deploy & watchdog for llm-web.

Modes:
    python3 safe_deploy.py deploy -m "msg"   Deploy: commit, restart, health check, rollback if broken
    python3 safe_deploy.py watchdog           Watchdog: monitor health every 30s, auto-rollback on failure
    python3 safe_deploy.py health             Just check if service is alive

How watchdog works:
    - Runs as a separate PM2 process (llm-web-watchdog)
    - Every 30 seconds checks GET /health
    - If 3 consecutive checks fail (~90 seconds of downtime):
        1. Reads .last_good_commit
        2. git reset --hard to that commit
        3. Restarts llm-web via PM2
        4. Verifies recovery
    - Logs everything to stdout (visible via pm2 logs llm-web-watchdog)

Timeline of self-healing:
    0s    — bad code deployed, service crashes
    0-30s — PM2 autorestart tries to bring it back (will fail if code is broken)
    30s   — watchdog detects first failure
    60s   — watchdog detects second failure
    90s   — watchdog detects third failure → ROLLBACK triggered
    95s   — git reset --hard to last good commit
    98s   — PM2 restart with good code
    ~100s — service is back online

    Worst case: ~100 seconds of downtime.
"""

import argparse
import subprocess
import sys
import time
import urllib.request
import json
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path("/mnt/c/Users/xtech/Projects/llm_web")
SERVICE_NAME = "llm-web"
HEALTH_URL = "http://localhost:8921/health"
LAST_GOOD_FILE = PROJECT_DIR / ".last_good_commit"

# Deploy settings
DEPLOY_HEALTH_RETRIES = 5
DEPLOY_RETRY_DELAY = 3

# Watchdog settings
WATCHDOG_INTERVAL = 60        # seconds between checks
WATCHDOG_FAIL_THRESHOLD = 5   # consecutive failures before rollback (~5 min)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd, cwd=PROJECT_DIR):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    return result


def get_current_commit():
    r = run(["git", "rev-parse", "--short=8", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def get_last_good_commit():
    if LAST_GOOD_FILE.exists():
        return LAST_GOOD_FILE.read_text().strip()
    return get_current_commit()


def save_last_good_commit(sha):
    LAST_GOOD_FILE.write_text(sha + "\n")
    log(f"Saved {sha[:8]} as last known good")


def single_health_check():
    """Single health check attempt. Returns True if healthy."""
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except Exception:
        return False


def health_check_with_retries(retries=DEPLOY_HEALTH_RETRIES, delay=DEPLOY_RETRY_DELAY):
    """Multiple health check attempts for deploy mode."""
    for attempt in range(1, retries + 1):
        if single_health_check():
            return True
        if attempt < retries:
            log(f"Health check {attempt}/{retries} failed, retry in {delay}s...")
            time.sleep(delay)
    return False


def pm2_restart():
    log(f"Stopping {SERVICE_NAME}...")
    run(["pm2", "stop", SERVICE_NAME])
    time.sleep(1)
    log(f"Starting {SERVICE_NAME}...")
    run(["pm2", "start", SERVICE_NAME])
    time.sleep(2)


def rollback_and_restart(target_sha):
    """Reset to known good commit and restart."""
    log(f"ROLLBACK → git reset --hard {target_sha[:8]}")
    run(["git", "reset", "--hard", target_sha])
    pm2_restart()
    return health_check_with_retries()


# ─── DEPLOY MODE ───────────────────────────────────────────────

def deploy(message):
    log("=" * 50)
    log("SAFE DEPLOY")
    log("=" * 50)

    last_good = get_last_good_commit()
    log(f"Last good: {(last_good or 'none')[:8]}, current: {(get_current_commit() or 'none')}")

    # Commit & push
    run(["git", "add", "-A"])
    status = run(["git", "status", "--porcelain"])
    had_changes = bool(status.stdout.strip())

    if had_changes:
        run(["git", "commit", "-m", message])
        push = run(["git", "push", "origin", "main"])
        if push.returncode != 0:
            log("Push failed (non-fatal)")
        log(f"Committed: {get_current_commit()}")
    else:
        log("No changes to commit")

    new_commit = get_current_commit()

    # Restart
    pm2_restart()

    # Health check
    log("Health check...")
    if health_check_with_retries():
        save_last_good_commit(new_commit)
        log("DEPLOY SUCCESS")
        return True
    else:
        log("SERVICE DOWN after deploy!")
        if last_good and last_good != new_commit:
            if rollback_and_restart(last_good):
                log(f"ROLLBACK SUCCESS → {last_good[:8]}")
            else:
                log("CRITICAL: Rollback also failed!")
        else:
            log("No previous good commit to rollback to")
        return False


# ─── WATCHDOG MODE ─────────────────────────────────────────────

def watchdog():
    log("=" * 50)
    log("WATCHDOG STARTED")
    log(f"Checking every {WATCHDOG_INTERVAL}s, rollback after {WATCHDOG_FAIL_THRESHOLD} failures")
    log(f"Last good commit: {(get_last_good_commit() or 'none')[:8]}")
    log("=" * 50)

    consecutive_failures = 0

    while True:
        time.sleep(WATCHDOG_INTERVAL)

        if single_health_check():
            if consecutive_failures > 0:
                log(f"Service recovered (was failing for {consecutive_failures} checks)")
                consecutive_failures = 0
            continue

        consecutive_failures += 1
        log(f"Health check FAILED ({consecutive_failures}/{WATCHDOG_FAIL_THRESHOLD})")

        if consecutive_failures >= WATCHDOG_FAIL_THRESHOLD:
            log("THRESHOLD REACHED — initiating rollback")

            last_good = get_last_good_commit()
            current = get_current_commit()

            if not last_good:
                log("No last good commit found, just restarting...")
                pm2_restart()
            elif last_good == current:
                log(f"Already on last good commit {current[:8]}, just restarting...")
                pm2_restart()
            else:
                log(f"Current: {current[:8]} → rolling back to: {last_good[:8]}")
                if rollback_and_restart(last_good):
                    log("ROLLBACK SUCCESS — service restored")
                else:
                    log("CRITICAL: Rollback failed, will retry next cycle")

            consecutive_failures = 0


# ─── MAIN ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Safe deploy & watchdog for llm-web")
    sub = parser.add_subparsers(dest="command")

    deploy_cmd = sub.add_parser("deploy", help="Deploy: commit, restart, health check")
    deploy_cmd.add_argument("-m", "--message", default="Auto-deploy: update llm-web")

    sub.add_parser("watchdog", help="Run watchdog: monitor and auto-rollback")
    sub.add_parser("health", help="Single health check")

    args = parser.parse_args()

    if args.command == "deploy":
        sys.exit(0 if deploy(args.message) else 1)
    elif args.command == "watchdog":
        try:
            watchdog()
        except KeyboardInterrupt:
            log("Watchdog stopped")
    elif args.command == "health":
        ok = single_health_check()
        print("HEALTHY" if ok else "DOWN")
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
