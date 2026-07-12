#!/bin/sh
# LaunchAgent entrypoint for the nightly protection pipeline.
#
# A network DEST's SMB mount drops between runs (sleep, logout, NAS reboot),
# so before running the pipeline this asks macOS to remount it. `open <url>`
# goes through NetAuthAgent, which mounts non-interactively only when the
# share's password is saved in the login keychain — connect once in Finder
# (⌘K) with "Remember this password in my keychain" to seed it. If the mount
# still isn't up after the wait, the pipeline runs anyway: its backup stage
# fails cleanly (/Volumes is root-owned, so nothing lands on the local disk)
# and POSTs to the notify URL — a silent skip would look identical to health.
#
# Usage: run-nightly.sh DEST MOUNT_URL [archive-nightly args...]
#   DEST       backup destination dir (passed through to `archive nightly`)
#   MOUNT_URL  smb:// URL whose volume contains DEST; empty string to skip
#              the mount step (local-disk DESTs)
set -u
DEST=$1; shift
MOUNT_URL=$1; shift

# The volume is mounted iff DEST's parent dir exists (the backup stage itself
# creates DEST on first run, so test the parent, not DEST).
if [ -n "$MOUNT_URL" ] && [ ! -d "$(dirname "$DEST")" ]; then
    open "$MOUNT_URL"
    i=0
    while [ $i -lt 30 ] && [ ! -d "$(dirname "$DEST")" ]; do
        sleep 2
        i=$((i + 1))
    done
fi

exec "$(dirname "$0")/../.venv/bin/archive" nightly "$DEST" "$@"
