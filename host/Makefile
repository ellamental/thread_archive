# thread-archive watcher — the live-ingest LaunchAgent.
#
# Runs `archive watch` straight from the repo venv: poll the local AI-tool
# stores (~/.claude, ~/.codex, ~/.grok, ~/.gemini, Cursor, OpenCode) and import
# new conversation content incrementally into the JSONL truth + SQLite index.
# No .app, no code-signing, no TCC — every watched store is a non-TCC path —
# and no backend: each source watcher calls the local importer directly.
#
# Day-to-day:
#   make install-agent     # (re)materialize the plist + (re)load the agent
#   make restart           # reload after a code edit (the venv runs source directly)
#   make logs
#   make uninstall-agent
#
# The `archive` console script comes from `pip install -e .` (or `.[embeddings]`)
# into the repo venv. If $(ENTRY) is missing, install the package first.

PLIST_SRC := com.thread-archive.watcher.plist
LABEL     := com.thread-archive.watcher
PLIST     := $(HOME)/Library/LaunchAgents/$(LABEL).plist
UID       := $(shell id -u)
REPO      := $(abspath $(CURDIR)/..)
ENTRY     := $(REPO)/.venv/bin/archive
# Logs live next to the data (the archive home), not in the repo.
LOG_DIR   := $(HOME)/.thread/archive/logs

# Scheduled backup+verify job (daily). DEST is the backup destination — pass it on
# the command line and point it at a DIFFERENT disk/machine:
#   make install-backup-agent DEST=/Volumes/Backup/thread_archive
BACKUP_PLIST_SRC := com.thread-archive.backup.plist
BACKUP_LABEL     := com.thread-archive.backup
BACKUP_PLIST     := $(HOME)/Library/LaunchAgents/$(BACKUP_LABEL).plist

.PHONY: all install-agent uninstall-agent restart logs status manifest install-backup-agent uninstall-backup-agent

all: install-agent

# Materialize the LaunchAgent plist from the committed template (substituting
# __REPO__ / __LOGDIR__ / __HOME__), pre-create the log dir (launchd won't mkdir
# StandardOutPath's parent — a missing dir fails the load silently), then reload.
install-agent:
	@test -x $(ENTRY) || { echo "missing $(ENTRY) — run 'pip install -e .' into the repo venv first"; exit 1; }
	@mkdir -p $(LOG_DIR) $(dir $(PLIST))
	sed -e 's#__REPO__#$(REPO)#g' -e 's#__LOGDIR__#$(LOG_DIR)#g' -e 's#__HOME__#$(HOME)#g' \
	    $(PLIST_SRC) > $(PLIST)
	-launchctl bootout gui/$(UID)/$(LABEL) 2>/dev/null
	@sleep 3   # let bootout settle before bootstrap (avoids 'Bootstrap failed: 5: Input/output error')
	launchctl bootstrap gui/$(UID) $(PLIST)
	$(REPO)/.venv/bin/python -m thread_archive.manifest
	@echo "installed (RunAtLoad=true; the watcher starts automatically)."
	@echo "to reload after a code edit:  make restart"
	@echo "to tail:                      make logs"

# (Re)write the family manifest alone — <home>/product.json, the discovery
# record consumers glob for (spec: docs/spec/product-json.md in the thread
# monorepo). install-agent already does this; use this target after a version
# bump or a path change without reloading the agent.
manifest:
	@test -x $(ENTRY) || { echo "missing $(ENTRY) — run 'pip install -e .' into the repo venv first"; exit 1; }
	$(REPO)/.venv/bin/python -m thread_archive.manifest

uninstall-agent:
	-launchctl bootout gui/$(UID)/$(LABEL)
	rm -f $(PLIST)
	@echo "uninstalled"

# Code edits go live on a plain restart (the venv runs source directly).
# Plist changes need a full reload — re-run `make install-agent` for those.
restart:
	launchctl kickstart -k gui/$(UID)/$(LABEL)
	@echo "restarted $(LABEL)"

status:
	@launchctl print gui/$(UID)/$(LABEL) 2>/dev/null | grep -E 'state =|pid =|program =' || echo "$(LABEL): not loaded"

logs:
	tail -F $(LOG_DIR)/watcher-stdout.log $(LOG_DIR)/watcher-stderr.log

# Backup+verify scheduled job. Requires DEST=<path> (a different disk/machine).
install-backup-agent:
	@test -n "$(DEST)" || { echo "pass a destination: make install-backup-agent DEST=/Volumes/Backup/thread_archive"; exit 1; }
	@test -x $(ENTRY) || { echo "missing $(ENTRY) — run 'pip install -e .' into the repo venv first"; exit 1; }
	@mkdir -p $(LOG_DIR) $(dir $(BACKUP_PLIST))
	sed -e 's#__REPO__#$(REPO)#g' -e 's#__LOGDIR__#$(LOG_DIR)#g' -e 's#__DEST__#$(DEST)#g' \
	    $(BACKUP_PLIST_SRC) > $(BACKUP_PLIST)
	-launchctl bootout gui/$(UID)/$(BACKUP_LABEL) 2>/dev/null
	@sleep 3
	launchctl bootstrap gui/$(UID) $(BACKUP_PLIST)
	@echo "installed $(BACKUP_LABEL) — backs up to $(DEST) daily at 04:00, then verifies."

uninstall-backup-agent:
	-launchctl bootout gui/$(UID)/$(BACKUP_LABEL)
	rm -f $(BACKUP_PLIST)
	@echo "uninstalled $(BACKUP_LABEL)"
