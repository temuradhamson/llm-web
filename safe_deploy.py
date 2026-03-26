#!/usr/bin/env python3
"""
Safe deploy script for llm-web.

Usage:
    python3 safe_deploy.py                # commit, restart, health check, rollback if broken
    python3 safe_deploy.py --message "fix bug"  # custom commit message
    python3 safe_deploy.py --check-only   # just health check, no deploy

Algorithm:
    1. Save current working commit hash as "last known good"
    2. Stage & commit all changes
    3. Push to GitHub
    4. Restart service via PM2
    5. Wait for health check (port 8921)
    6. If healthy → done, new commit is the "last known good"
    7. If NOT healthy → git reset to last good commit, restart again
"""

import argparse
import subprocess
import sys
import time
import urllib.request
import json
from pathlib import Path

PROJECT_DIR = Path("/mnt/c/Users/xtech/Projects/llm_web")
SERVICE_NAME = "llm-web"
HEALTH_URL = "http://localhost:8921/health"
HEALTH_TIMEOUT = 15  # seconds to wait for service
HEALTH_RETRIES = 5   # number of retries
LAST_GOOD_FILE = PROJECT_DIR / ".last_good_commit"


def run(cmd, cwd=PROJECT_DIR, check=True):
    """Run a shell command and return output."""
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        print(f"  WARN: {' '.join(cmd)} → exit {result.returncode}")
        if result.stderr.strip():
            print(f"  stderr: {result.stderr.strip()}")
    return result


def get_current_commit():
    r = run(["git", "rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def get_last_good_commit():
    if LAST_GOOD_FILE.exists():
        return LAST_GOOD_FILE.read_text().strip()
    return get_current_commit()


def save_last_good_commit(sha):
    LAST_GOOD_FILE.write_text(sha + "\n")


def health_check():
    """Check if the service is healthy."""
    for attempt in range(1, HEALTH_RETRIES + 1):
        try:
            req = urllib.request.Request(HEALTH_URL, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                if data.get("ok"):
                    return True
        except Exception:
            pass
        if attempt < HEALTH_RETRIES:
            print(f"  Health check {attempt}/{HEALTH_RETRIES} failed, retrying in {HEALTH_TIMEOUT // HEALTH_RETRIES}s...")
            time.sleep(HEALTH_TIMEOUT // HEALTH_RETRIES)
    return False


def pm2_restart():
    """Stop and start the service."""
    print("  Stopping service...")
    run(["pm2", "stop", SERVICE_NAME], check=False)
    time.sleep(1)
    print("  Starting service...")
    run(["pm2", "start", SERVICE_NAME], check=False)
    time.sleep(2)


def git_commit_and_push(message):
    """Stage all changes, commit, and push."""
    run(["git", "add", "-A"])

    # Check if there's anything to commit
    status = run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        print("  No changes to commit.")
        return False

    run(["git", "commit", "-m", message])
    print("  Pushing to GitHub...")
    push = run(["git", "push", "origin", "main"], check=False)
    if push.returncode != 0:
        print("  Push failed (non-fatal, will continue)")
    return True


def rollback(target_sha):
    """Reset to a known good commit."""
    print(f"  Rolling back to {target_sha[:8]}...")
    run(["git", "reset", "--hard", target_sha])


def deploy(message="Auto-deploy: update llm-web"):
    print("=" * 50)
    print("SAFE DEPLOY — llm-web")
    print("=" * 50)

    # Step 1: Save last known good
    last_good = get_last_good_commit()
    current = get_current_commit()
    print(f"\n[1/6] Last good commit: {(last_good or 'none')[:8]}")
    print(f"       Current commit:  {(current or 'none')[:8]}")

    # Step 2: Commit changes
    print(f"\n[2/6] Committing changes...")
    had_changes = git_commit_and_push(message)
    new_commit = get_current_commit()

    if had_changes:
        print(f"       New commit: {new_commit[:8]}")
    else:
        print("       No new changes, restarting with current code.")

    # Step 3: Restart
    print(f"\n[3/6] Restarting {SERVICE_NAME}...")
    pm2_restart()

    # Step 4: Health check
    print(f"\n[4/6] Health check...")
    healthy = health_check()

    if healthy:
        # Step 5: Success
        print(f"\n[5/6] Service is HEALTHY!")
        save_last_good_commit(new_commit)
        print(f"[6/6] Saved {new_commit[:8]} as last known good.")
        print(f"\n{'=' * 50}")
        print("DEPLOY SUCCESS")
        print(f"{'=' * 50}")
        return True
    else:
        # Step 5: Rollback
        print(f"\n[5/6] Service is DOWN! Rolling back...")
        if last_good and last_good != new_commit:
            rollback(last_good)
            print(f"\n[6/6] Restarting with last good code...")
            pm2_restart()

            # Verify rollback worked
            if health_check():
                print(f"\n{'=' * 50}")
                print(f"ROLLBACK SUCCESS — reverted to {last_good[:8]}")
                print(f"{'=' * 50}")
            else:
                print(f"\n{'=' * 50}")
                print("CRITICAL: Rollback also failed!")
                print(f"{'=' * 50}")
        else:
            print("  No previous good commit to rollback to!")
            print(f"\n{'=' * 50}")
            print("DEPLOY FAILED — manual intervention needed")
            print(f"{'=' * 50}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Safe deploy for llm-web")
    parser.add_argument("-m", "--message", default="Auto-deploy: update llm-web",
                        help="Commit message")
    parser.add_argument("--check-only", action="store_true",
                        help="Only run health check")
    args = parser.parse_args()

    if args.check_only:
        ok = health_check()
        print("HEALTHY" if ok else "DOWN")
        sys.exit(0 if ok else 1)

    success = deploy(args.message)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
