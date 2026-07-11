"""SQLite / cursorDiskKV reading methods for ``CursorExporter``.

A mixin of the database-reading and KV→conversation-building methods, lifted out
of ``CursorExporter`` so the class file stays focused. ``_read_sqlite_db`` and
``_read_cursor_disk_kv`` open a SQLite file passed in by path; the rest transform
the resulting composer/bubble dicts into conversation structures, delegating the
per-message shape work to ``cursor_parse``. None read ``self.storage_path``.

The methods stay methods (not free functions) so callers that access them as
class/instance attributes — including tests that build a bare exporter with
``__new__`` and the ``export_all`` orchestrator that stubs ``_read_cursor_disk_kv``
and ``_build_conversations_from_kv`` on the instance — keep resolving to the same
objects via the assembled class's MRO. Moved byte-for-byte from ``cursor.py``.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import cursor_parse

logger = logging.getLogger(__name__)


class CursorKVMixin:
    """SQLite key-value reading + KV→conversation building methods."""

    def _read_sqlite_db(self, db_path: Path) -> Dict[str, Any]:
        """Read key-value pairs from a Cursor SQLite database."""
        data = {}

        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            if "ItemTable" in tables:
                cursor.execute("SELECT key, value FROM ItemTable")
                for key, value in cursor.fetchall():
                    if value and isinstance(value, str):
                        try:
                            if value.strip().startswith(("{", "[")):
                                data[key] = json.loads(value)
                            else:
                                data[key] = value
                        except json.JSONDecodeError:
                            data[key] = value
                    else:
                        data[key] = value

            conn.close()
        except sqlite3.Error:
            pass
        except Exception:
            logger.warning("Failed to read SQLite database at %s", db_path, exc_info=True)

        return data

    def _read_cursor_disk_kv(self, db_path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Read conversation data from Cursor's cursorDiskKV table.

        Returns:
            Tuple of (composers, bubbles)
        """
        composers = {}
        bubbles = {}

        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'")
            if not cursor.fetchone():
                conn.close()
                return composers, bubbles

            cursor.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
            for key, value in cursor.fetchall():
                try:
                    composer_id = key.replace("composerData:", "")
                    data = json.loads(value)
                    composers[composer_id] = data
                except (json.JSONDecodeError, Exception):
                    pass

            cursor.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'")
            for key, value in cursor.fetchall():
                try:
                    parts = key.split(":")
                    if len(parts) >= 3:
                        bubble_id = parts[2]
                        composer_id = parts[1]
                        data = json.loads(value)
                        data["_composerId"] = composer_id
                        bubbles[f"{composer_id}:{bubble_id}"] = data
                except (json.JSONDecodeError, Exception):
                    pass

            conn.close()

        except sqlite3.Error:
            pass
        except Exception:
            logger.warning("Failed to read cursorDiskKV from %s", db_path, exc_info=True)

        return composers, bubbles

    def _build_conversations_from_kv(
        self,
        composers: Dict[str, Any],
        bubbles: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Build conversation structures from composer and bubble data."""
        conversations = []

        for composer_id, composer in composers.items():
            headers = composer.get("fullConversationHeadersOnly", [])
            if not headers:
                continue

            messages = []
            for idx, header in enumerate(headers):
                bubble_id = header.get("bubbleId")
                if not bubble_id:
                    continue

                bubble = bubbles.get(f"{composer_id}:{bubble_id}", {})
                messages.append(
                    self._build_kv_message(bubble_id, bubble, header, idx)
                )

            conversations.append({
                "id": composer_id,
                "title": composer.get("name", "Untitled"),
                "created_at": composer.get("createdAt"),
                "updated_at": composer.get("lastUpdatedAt"),
                "messages": messages,
                "metadata": self._build_kv_metadata(composer),
            })

        return conversations

    @staticmethod
    def _kv_role(bubble: Dict[str, Any], header: Dict[str, Any]) -> str:
        """Map a Cursor bubble/header ``type`` (1/2) onto a canonical role."""
        return cursor_parse.kv_role(bubble, header)

    @staticmethod
    def _build_kv_message(
        bubble_id: str,
        bubble: Dict[str, Any],
        header: Dict[str, Any],
        idx: int,
    ) -> Dict[str, Any]:
        """Build one conversation message from a composer header + its bubble."""
        return cursor_parse.build_kv_message(bubble_id, bubble, header, idx)

    @staticmethod
    def _build_kv_metadata(composer: Dict[str, Any]) -> Dict[str, Any]:
        """Extract the conversation metadata block from a composer record."""
        return cursor_parse.build_kv_metadata(composer)
