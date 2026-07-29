# MCP

One server, two explicit process modes. **`thread-archive`** (`archive-mcp`)
serves the read-only `thread_search` / `thread_read` tools. The process is also
read-only by default. Setting `THREAD_ARCHIVE_MCP_INGEST=1` opts it into local
lazy catch-up ingest, throttled and cross-process-safe via the ingest-owner
lock. This server exposes no write surface. Client
config with catch-up enabled:

```json
{
  "mcpServers": {
    "thread-archive": {
      "command": "archive-mcp",
      "env": {
        "THREAD_ARCHIVE_HOME": "~/.thread/archive",
        "THREAD_ARCHIVE_MCP_INGEST": "1"
      }
    }
  }
}
```

The shared HTTP server daemon is read-only by default too. Install it with
`thread-archive service install --mcp --mcp-ingest` only when it should own catch-up;
leave the flag off when the watcher already owns ingestion.
