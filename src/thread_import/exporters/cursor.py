"""
Cursor IDE Chat Exporter

Extracts chat history and plans from Cursor IDE's local storage.

USAGE (CLI):
    python -m packages.chat_import.exporters.cursor --list
    python -m packages.chat_import.exporters.cursor --workspace HASH
    python -m packages.chat_import.exporters.cursor --output chats.zip

USAGE (Library):
    from packages.chat_import.exporters.cursor import CursorExporter

    exporter = CursorExporter()
    workspaces = exporter.list_workspaces()
    result = exporter.export_all(output_path="chats.zip")

STORAGE LOCATIONS:
    Chats:
        - macOS: ~/Library/Application Support/Cursor/User/workspaceStorage/
        - Linux: ~/.config/Cursor/User/workspaceStorage/
        - Windows: %APPDATA%\\Cursor\\User\\workspaceStorage\\
    Plans:
        - All platforms: ~/.cursor/plans/
"""

import json
import os
import platform
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._cursor_kv_mixin import CursorKVMixin
from ._cursor_parse_mixin import CursorParseMixin


@dataclass
class ExportResult:
    """Result of an export operation."""
    output_path: Path
    conversation_count: int
    message_count: int
    image_count: int
    plan_count: int = 0
    workspaces: List[Dict[str, Any]] = field(default_factory=list)


class CursorExporter(CursorKVMixin, CursorParseMixin):
    """Exports chat data from Cursor IDE's local storage.

    Database reading + KV→conversation building live in ``CursorKVMixin``;
    loose composer/chat-blob parsing lives in ``CursorParseMixin``. Both are
    assembled here so every method stays a ``CursorExporter`` attribute (callers
    and tests reach them as ``exporter._read_cursor_disk_kv`` etc.).
    """

    def __init__(self, storage_path: Optional[Path] = None):
        """
        Initialize the exporter.

        Args:
            storage_path: Custom storage path, or None to auto-detect
        """
        self.storage_path = storage_path or self._get_storage_path()

    @staticmethod
    def _get_storage_path() -> Optional[Path]:
        """Get the Cursor workspace storage path for the current OS."""
        system = platform.system()

        if system == "Darwin":  # macOS
            path = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
        elif system == "Linux":
            path = Path.home() / ".config" / "Cursor" / "User" / "workspaceStorage"
        elif system == "Windows":
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                path = Path(appdata) / "Cursor" / "User" / "workspaceStorage"
            else:
                return None
        else:
            return None

        return path if path.exists() else None

    @staticmethod
    def _get_global_storage_path() -> Optional[Path]:
        """Get the Cursor global storage path."""
        system = platform.system()

        if system == "Darwin":
            path = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage"
        elif system == "Linux":
            path = Path.home() / ".config" / "Cursor" / "User" / "globalStorage"
        elif system == "Windows":
            appdata = os.environ.get("APPDATA", "")
            if appdata:
                path = Path(appdata) / "Cursor" / "User" / "globalStorage"
            else:
                return None
        else:
            return None

        return path if path.exists() else None

    @staticmethod
    def _get_plans_path() -> Optional[Path]:
        """Get the Cursor plans storage path (~/.cursor/plans/)."""
        system = platform.system()

        if system == "Darwin":
            path = Path.home() / ".cursor" / "plans"
        elif system == "Linux":
            path = Path.home() / ".cursor" / "plans"
        elif system == "Windows":
            path = Path.home() / ".cursor" / "plans"
        else:
            return None

        return path if path.exists() else None

    def _collect_plans(self) -> List[Dict[str, Any]]:
        """Collect all plan files from ~/.cursor/plans/."""
        plans = []
        plans_path = self._get_plans_path()

        if not plans_path or not plans_path.exists():
            return plans

        for plan_file in plans_path.glob("*.plan.md"):
            try:
                content = plan_file.read_text(encoding="utf-8")
                stat = plan_file.stat()

                # Parse filename: name_hash.plan.md
                stem = plan_file.stem.replace(".plan", "")
                parts = stem.rsplit("_", 1)
                if len(parts) == 2:
                    name = parts[0].replace("_", " ")
                    plan_hash = parts[1]
                else:
                    name = stem
                    plan_hash = None

                plans.append({
                    "filename": plan_file.name,
                    "name": name,
                    "hash": plan_hash,
                    "content": content,
                    "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "size_bytes": stat.st_size,
                })
            except (IOError, OSError):
                pass

        # Sort by modified time, most recent first
        plans.sort(key=lambda p: p.get("modified_at", ""), reverse=True)
        return plans

    def _find_workspace_info(self, workspace_dir: Path) -> Dict[str, Any]:
        """Extract workspace info from workspace.json if available."""
        workspace_json = workspace_dir / "workspace.json"
        info = {
            "hash": workspace_dir.name,
            "path": str(workspace_dir),
        }

        if workspace_json.exists():
            try:
                with open(workspace_json) as f:
                    data = json.load(f)
                    folder = data.get("folder", "")
                    if folder:
                        if folder.startswith("file://"):
                            folder = folder[7:]
                        info["workspace_uri"] = folder
                        info["workspace_name"] = Path(folder).name
            except (json.JSONDecodeError, IOError):
                pass

        return info

    def _collect_workspace_images(self) -> Dict[str, Path]:
        """Collect all images from workspace storage folders."""
        images = {}

        if not self.storage_path:
            return images

        for workspace_dir in self.storage_path.iterdir():
            if not workspace_dir.is_dir():
                continue

            images_dir = workspace_dir / "images"
            if not images_dir.exists():
                continue

            for img_file in images_dir.iterdir():
                if img_file.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                    name = img_file.stem
                    parts = name.split('-')
                    if len(parts) >= 5:
                        potential_uuid = '-'.join(parts[-5:])
                        if len(potential_uuid) == 36:
                            images[potential_uuid] = img_file
                            continue
                    images[name] = img_file

        return images

    def _export_workspace(self, workspace_info: Dict[str, Any]) -> Dict[str, Any]:
        """Export chat data from a single workspace."""
        result = {
            "workspace": workspace_info,
            "conversations": [],
            "raw_chat_data": {},
        }

        state_db = Path(workspace_info.get("state_db", ""))
        if state_db.exists():
            db_data = self._read_sqlite_db(state_db)
            chat_data = self._extract_chat_data(db_data)

            result["raw_chat_data"]["state"] = chat_data

            for key, value in chat_data.items():
                conversations = self._parse_composer_data(value)
                result["conversations"].extend(conversations)

        for db_path in workspace_info.get("other_dbs", []):
            db_file = Path(db_path)
            if db_file.exists():
                db_data = self._read_sqlite_db(db_file)
                chat_data = self._extract_chat_data(db_data)

                result["raw_chat_data"][db_file.stem] = chat_data

                for key, value in chat_data.items():
                    conversations = self._parse_composer_data(value)
                    result["conversations"].extend(conversations)

        return result

    def list_workspaces(self) -> List[Dict[str, Any]]:
        """List all available Cursor workspaces with chat data."""
        workspaces = []

        if not self.storage_path or not self.storage_path.exists():
            return workspaces

        for workspace_dir in self.storage_path.iterdir():
            if not workspace_dir.is_dir():
                continue

            state_db = workspace_dir / "state.vscdb"
            if not state_db.exists():
                continue

            info = self._find_workspace_info(workspace_dir)
            info["state_db"] = str(state_db)

            for db_file in workspace_dir.glob("*.vscdb"):
                if db_file.name != "state.vscdb":
                    info.setdefault("other_dbs", []).append(str(db_file))

            images_dir = workspace_dir / "images"
            if images_dir.exists():
                info["images_dir"] = str(images_dir)
                info["image_count"] = len(list(images_dir.glob("*")))

            workspaces.append(info)

        return workspaces

    def export_all(
        self,
        output_path: Optional[Path] = None,
        workspace_filter: Optional[str] = None,
        include_raw: bool = True,
    ) -> ExportResult:
        """
        Export chat data from all (or filtered) workspaces.

        Args:
            output_path: Output ZIP file path (auto-generated if None)
            workspace_filter: Partial hash to filter workspaces
            include_raw: Include raw database data in export

        Returns:
            ExportResult with export details
        """
        if not self.storage_path:
            raise RuntimeError("Could not find Cursor storage path")

        all_conversations = self._collect_kv_conversations()
        workspaces, workspace_data = self._gather_workspace_data(workspace_filter, include_raw)

        # Deduplicate conversations by ID and sort (most recent first)
        unique_conversations = self._dedupe_and_sort_conversations(all_conversations)

        images = self._collect_workspace_images()
        plans = self._collect_plans()

        output_path = self._resolve_output_path(output_path)
        total_messages = sum(len(c.get("messages", [])) for c in unique_conversations)

        self._write_export_zip(
            output_path,
            unique_conversations=unique_conversations,
            workspaces=workspaces,
            workspace_data=workspace_data,
            images=images,
            plans=plans,
            total_messages=total_messages,
        )

        return ExportResult(
            output_path=output_path,
            conversation_count=len(unique_conversations),
            message_count=total_messages,
            image_count=len(images),
            plan_count=len(plans),
            workspaces=workspace_data,
        )

    def _collect_kv_conversations(self) -> List[Dict[str, Any]]:
        """Read conversations from globalStorage's cursorDiskKV (primary source)."""
        assert self.storage_path is not None  # guaranteed by export_all
        # Read from globalStorage's cursorDiskKV table (primary source)
        global_storage_db = self.storage_path.parent / "globalStorage" / "state.vscdb"
        if not global_storage_db.exists():
            return []

        composers, bubbles = self._read_cursor_disk_kv(global_storage_db)
        if not composers:
            return []

        return self._build_conversations_from_kv(composers, bubbles)

    def _gather_workspace_data(
        self,
        workspace_filter: Optional[str],
        include_raw: bool,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """List (and filter) workspaces and summarize each one's export.

        Returns the (filtered) workspace list and the per-workspace summary
        records that feed both metadata.json and the ExportResult.
        """
        workspaces = self.list_workspaces()
        if workspace_filter:
            workspaces = [ws for ws in workspaces if workspace_filter in ws.get("hash", "")]

        workspace_data = []
        for ws in workspaces:
            result = self._export_workspace(ws)
            workspace_data.append({
                "info": ws,
                "conversation_count": len(result["conversations"]),
                "raw_data": result["raw_chat_data"] if include_raw else None,
            })

        return workspaces, workspace_data

    @staticmethod
    def _resolve_output_path(output_path: Optional[Path]) -> Path:
        """Default the output path to a timestamped file under exports/."""
        if output_path is None:
            timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            exports_dir = Path("exports")
            exports_dir.mkdir(exist_ok=True)
            return exports_dir / f"cursor-chats-{timestamp}.zip"
        return Path(output_path)

    def _write_export_zip(
        self,
        output_path: Path,
        *,
        unique_conversations: List[Dict[str, Any]],
        workspaces: List[Dict[str, Any]],
        workspace_data: List[Dict[str, Any]],
        images: Dict[str, Path],
        plans: List[Dict[str, Any]],
        total_messages: int,
    ) -> None:
        """Write the export ZIP (chats/metadata/plans JSON + image & plan files)."""
        # Create ZIP archive
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            conversations_file = temp_path / "cursor_chats.json"
            with open(conversations_file, "w") as f:
                json.dump({
                    "provider": "cursor",
                    "export_version": "2.0",
                    "export_date": datetime.now().isoformat(),
                    "conversations": unique_conversations,
                }, f, indent=2, default=str)

            metadata_file = temp_path / "metadata.json"
            with open(metadata_file, "w") as f:
                json.dump({
                    "export_date": datetime.now().isoformat(),
                    "platform": platform.system(),
                    "workspace_count": len(workspaces),
                    "conversation_count": len(unique_conversations),
                    "message_count": total_messages,
                    "image_count": len(images),
                    "plan_count": len(plans),
                    "workspaces": workspace_data,
                }, f, indent=2, default=str)

            # Write plans JSON
            plans_file = temp_path / "cursor_plans.json"
            with open(plans_file, "w") as f:
                json.dump({
                    "provider": "cursor",
                    "export_version": "1.0",
                    "export_date": datetime.now().isoformat(),
                    "plans": plans,
                }, f, indent=2, default=str)

            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(conversations_file, "cursor_chats.json")
                zf.write(metadata_file, "metadata.json")
                zf.write(plans_file, "cursor_plans.json")

                for img_id, img_path in images.items():
                    zf.write(img_path, f"images/{img_path.name}")

                # Also include raw plan markdown files
                for plan in plans:
                    plans_path = self._get_plans_path()
                    if plans_path:
                        plan_file = plans_path / plan["filename"]
                        if plan_file.exists():
                            zf.write(plan_file, f"plans/{plan['filename']}")

    @staticmethod
    def _dedupe_and_sort_conversations(
        all_conversations: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Deduplicate conversations by id and sort most-recent-first.

        Keeps the first occurrence of each id; conversations without an id are
        all kept. Sort key is ``updated_at`` then ``created_at`` then 0, reversed.
        """
        # Deduplicate conversations by ID
        seen_ids = set()
        unique_conversations = []
        for conv in all_conversations:
            conv_id = conv.get("id")
            if conv_id and conv_id not in seen_ids:
                seen_ids.add(conv_id)
                unique_conversations.append(conv)
            elif not conv_id:
                unique_conversations.append(conv)

        # Sort by updated_at (most recent first)
        unique_conversations.sort(
            key=lambda c: c.get("updated_at") or c.get("created_at") or 0,
            reverse=True
        )
        return unique_conversations

    def inspect_db(self, db_path: Path) -> Dict[str, Any]:
        """
        Inspect a Cursor database and return available keys.

        Returns:
            Dict with 'chat_keys' and 'other_keys' lists
        """
        if not db_path.exists():
            raise FileNotFoundError(f"Database not found: {db_path}")

        data = self._read_sqlite_db(db_path)

        chat_patterns = ["chat", "composer", "conversation", "ai", "cursor"]
        chat_keys = []
        other_keys = []

        for key in sorted(data.keys()):
            key_lower = key.lower()
            is_chat = any(p in key_lower for p in chat_patterns)
            if is_chat:
                chat_keys.append(key)
            else:
                other_keys.append(key)

        return {
            "total_keys": len(data),
            "chat_keys": chat_keys,
            "other_keys": other_keys,
            "data": data,
        }


def main():
    """CLI entry point."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Export Cursor IDE chat history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m packages.chat_import.exporters.cursor --list
    python -m packages.chat_import.exporters.cursor --workspace abc123
    python -m packages.chat_import.exporters.cursor --output chats.zip
        """,
    )

    parser.add_argument("--list", "-l", action="store_true", help="List available workspaces")
    parser.add_argument("--workspace", "-w", help="Export specific workspace (partial hash match)")
    parser.add_argument("--output", "-o", type=Path, help="Output file path")
    parser.add_argument("--inspect", "-i", type=Path, help="Inspect a specific database file")
    parser.add_argument("--no-raw", action="store_true", help="Exclude raw database data")
    parser.add_argument("--storage-path", "-s", type=Path, help="Custom Cursor storage path")

    args = parser.parse_args()

    exporter = CursorExporter(storage_path=args.storage_path)

    if args.inspect:
        result = exporter.inspect_db(args.inspect)
        print(f"\nFound {result['total_keys']} keys:\n")

        if result['chat_keys']:
            print("Chat-related keys:")
            for key in result['chat_keys']:
                print(f"  - {key}")

        print(f"\nOther keys ({len(result['other_keys'])} total):")
        for key in result['other_keys'][:20]:
            print(f"  - {key}")
        if len(result['other_keys']) > 20:
            print(f"  ... and {len(result['other_keys']) - 20} more")
        return

    if not exporter.storage_path:
        print("Could not find Cursor storage path.", file=sys.stderr)
        sys.exit(1)

    print(f"Cursor storage: {exporter.storage_path}")

    if args.list:
        workspaces = exporter.list_workspaces()
        if not workspaces:
            print("No Cursor workspaces found.")
            return

        print(f"\nFound {len(workspaces)} workspace(s):\n")
        print(f"{'Hash':<20} {'Workspace Name':<40} {'Path'}")
        print("-" * 100)

        for ws in workspaces:
            ws_hash = ws.get("hash", "")[:18]
            ws_name = ws.get("workspace_name", "Unknown")[:38]
            ws_path = ws.get("workspace_uri", "Unknown path")
            print(f"{ws_hash:<20} {ws_name:<40} {ws_path}")
        return

    result = exporter.export_all(
        output_path=args.output,
        workspace_filter=args.workspace,
        include_raw=not args.no_raw,
    )

    print(f"\nExported {result.conversation_count} conversations to {result.output_path}")
    print(f"Total messages: {result.message_count}")
    print(f"Images: {result.image_count}")
    print(f"Plans: {result.plan_count}")


if __name__ == "__main__":
    main()
