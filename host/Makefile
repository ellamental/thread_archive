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
LOG_DIR   := $(HOME)/.thread_archive/logs

.PHONY: all install-agent uninstall-agent restart logs status

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
	@echo "installed (RunAtLoad=true; the watcher starts automatically)."
	@echo "to reload after a code edit:  make restart"
	@echo "to tail:                      make logs"

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
