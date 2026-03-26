#!/usr/bin/env python3
"""
Safe deploy & watchdog for llm-web.

Modes:
    python3 safe_deploy.py deploy -m "msg"   Deploy with black box recording
    python3 safe_deploy.py watchdog           Monitor health, auto-rollback with crash report
    python3 safe_deploy.py health             Single health check
    python3 safe_deploy.py crashes            Show recent crash reports

Black box system:
    Before each deploy, the diff is saved to .llm_web_data/deploys/.
    If deploy fails → rollback + crash report written.
    On next startup, Claude can read the crash report and understand what went wrong.
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

# Data dirs
DATA_DIR = Path("/workspace/.llm_web_data")
DEPLOYS_DIR = DATA_DIR / "deploys"
CRASH_REPORT_FILE = DATA_DIR / "last_crash_report.md"

# Deploy settings
DEPLOY_HEALTH_RETRIES = 5
DEPLOY_RETRY_DELAY = 3

# Watchdog settings
WATCHDOG_INTERVAL = 60
WATCHDOG_FAIL_THRESHOLD = 5


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd, cwd=PROJECT_DIR):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def get_current_commit():
    r = run(["git", "rev-parse", "--short=8", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def get_full_commit():
    r = run(["git", "rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def get_last_good_commit():
    if LAST_GOOD_FILE.exists():
        return LAST_GOOD_FILE.read_text().strip()
    return get_current_commit()


def save_last_good_commit(sha):
    LAST_GOOD_FILE.write_text(sha + "\n")
    log(f"Saved {sha[:8]} as last known good")


def single_health_check():
    try:
        req = urllib.request.Request(HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("ok", False)
    except Exception:
        return False


def health_check_with_retries(retries=DEPLOY_HEALTH_RETRIES, delay=DEPLOY_RETRY_DELAY):
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
    log(f"ROLLBACK → git reset --hard {target_sha[:8]}")
    run(["git", "reset", "--hard", target_sha])
    pm2_restart()
    return health_check_with_retries()


# ─── BLACK BOX ─────────────────────────────────────────────────

def save_deploy_snapshot(message: str) -> Path:
    """Save diff + metadata before deploy. Returns snapshot dir."""
    DEPLOYS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = DEPLOYS_DIR / ts
    snap_dir.mkdir(exist_ok=True)

    # Staged diff
    diff_staged = run(["git", "diff", "--cached"])
    # Unstaged diff
    diff_unstaged = run(["git", "diff"])
    # Full status
    status = run(["git", "status"])
    # Changed file list
    files_changed = run(["git", "diff", "--cached", "--name-only"])

    (snap_dir / "message.txt").write_text(message)
    (snap_dir / "diff_staged.patch").write_text(diff_staged.stdout)
    (snap_dir / "diff_unstaged.patch").write_text(diff_unstaged.stdout)
    (snap_dir / "status.txt").write_text(status.stdout)
    (snap_dir / "files.txt").write_text(files_changed.stdout)
    (snap_dir / "commit_before.txt").write_text(get_current_commit() or "unknown")
    (snap_dir / "timestamp.txt").write_text(datetime.now().isoformat())

    log(f"Snapshot saved: {snap_dir.name}")
    return snap_dir


def write_crash_report(snap_dir: Path, last_good: str, failed_commit: str):
    """Write a crash report that Claude can read on next startup."""
    message = (snap_dir / "message.txt").read_text() if (snap_dir / "message.txt").exists() else "unknown"
    diff = (snap_dir / "diff_staged.patch").read_text() if (snap_dir / "diff_staged.patch").exists() else ""
    files = (snap_dir / "files.txt").read_text().strip() if (snap_dir / "files.txt").exists() else ""
    ts = datetime.now().isoformat()

    report = f"""# Crash Report — {ts}

## What happened
Deploy failed and was rolled back automatically.

## What was attempted
**Message:** {message}
**Failed commit:** {failed_commit}
**Rolled back to:** {last_good}

## Files changed
```
{files}
```

## Diff that caused the crash
```diff
{diff[:5000]}
```
{f'... (truncated, full diff in {snap_dir}/diff_staged.patch)' if len(diff) > 5000 else ''}

## What to do next
1. Read the diff above to understand what broke
2. Fix the issue in the code
3. Deploy again with `python3 safe_deploy.py deploy -m "fix: ..."`
"""

    CRASH_REPORT_FILE.write_text(report)
    # Also save in snapshot dir
    (snap_dir / "CRASH_REPORT.md").write_text(report)
    (snap_dir / "result.txt").write_text("FAILED")
    log(f"Crash report written: {CRASH_REPORT_FILE}")


def mark_deploy_success(snap_dir: Path, commit: str):
    """Mark snapshot as successful."""
    (snap_dir / "result.txt").write_text("SUCCESS")
    (snap_dir / "commit_after.txt").write_text(commit)
    # Clear crash report if exists
    if CRASH_REPORT_FILE.exists():
        CRASH_REPORT_FILE.unlink()


def get_latest_crash_report() -> str | None:
    if CRASH_REPORT_FILE.exists():
        return CRASH_REPORT_FILE.read_text()
    return None


def cleanup_old_snapshots(keep=20):
    """Keep only the last N deploy snapshots."""
    if not DEPLOYS_DIR.exists():
        return
    dirs = sorted(DEPLOYS_DIR.iterdir(), reverse=True)
    for d in dirs[keep:]:
        if d.is_dir():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()


# ─── DEPLOY MODE ───────────────────────────────────────────────

def deploy(message):
    log("=" * 50)
    log("SAFE DEPLOY")
    log("=" * 50)

    # Check for previous crash report
    crash = get_latest_crash_report()
    if crash:
        log("WARNING: Previous deploy crashed! Report at .llm_web_data/last_crash_report.md")

    last_good = get_last_good_commit()
    log(f"Last good: {(last_good or 'none')[:8]}, current: {(get_current_commit() or 'none')}")

    # Stage changes
    run(["git", "add", "-A"])
    status = run(["git", "status", "--porcelain"])
    had_changes = bool(status.stdout.strip())

    # Save black box snapshot BEFORE commit
    snap_dir = save_deploy_snapshot(message)

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
        mark_deploy_success(snap_dir, new_commit)
        cleanup_old_snapshots()
        log("DEPLOY SUCCESS")
        return True
    else:
        log("SERVICE DOWN after deploy!")
        if last_good and last_good != new_commit:
            write_crash_report(snap_dir, last_good, new_commit)
            if rollback_and_restart(last_good):
                log(f"ROLLBACK SUCCESS → {last_good[:8]}")
                log("Crash report saved for next session")
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
                # Save crash snapshot for watchdog-triggered rollback
                DEPLOYS_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                snap_dir = DEPLOYS_DIR / f"{ts}_watchdog"
                snap_dir.mkdir(exist_ok=True)

                # Capture diff between last good and current broken code
                diff = run(["git", "diff", last_good, current])
                files = run(["git", "diff", "--name-only", last_good, current])
                commit_log = run(["git", "log", "--oneline", f"{last_good}..{current}"])

                (snap_dir / "message.txt").write_text(f"Watchdog rollback: {current} → {last_good}")
                (snap_dir / "diff_staged.patch").write_text(diff.stdout)
                (snap_dir / "files.txt").write_text(files.stdout)
                (snap_dir / "commits.txt").write_text(commit_log.stdout)
                (snap_dir / "result.txt").write_text("WATCHDOG_ROLLBACK")

                write_crash_report(snap_dir, last_good, current)

                log(f"Current: {current[:8]} → rolling back to: {last_good[:8]}")
                if rollback_and_restart(last_good):
                    log("ROLLBACK SUCCESS — service restored")
                    log("Crash report saved for next session")
                else:
                    log("CRITICAL: Rollback failed, will retry next cycle")

            consecutive_failures = 0


# ─── MAIN ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Safe deploy & watchdog for llm-web")
    sub = parser.add_subparsers(dest="command")

    deploy_cmd = sub.add_parser("deploy", help="Deploy with black box recording")
    deploy_cmd.add_argument("-m", "--message", default="Auto-deploy: update llm-web")

    sub.add_parser("watchdog", help="Run watchdog with crash reports")
    sub.add_parser("health", help="Single health check")
    sub.add_parser("crashes", help="Show latest crash report")

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
    elif args.command == "crashes":
        report = get_latest_crash_report()
        if report:
            print(report)
        else:
            print("No crash reports.")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
