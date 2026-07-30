# MCP

One server, two transports. It serves the read-only `thread_search` /
`thread_read` tools plus `thread_help`, which serves their long-form manual on
demand — the two tools ship a compact description because a description is
charged to every session that lists them, called or not. There is no write
surface.

## Per-client, over stdio

The default: each MCP client starts its own `archive-mcp` process.

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

## Shared, over HTTP

One always-on server many agents point at, so a single resident embedding model
serves all of them instead of one multi-GB process per client:

```bash
thread-archive service install --mcp        # launchd on macOS, systemd --user on Linux
```

The agent runs `archive-mcp --http --host 127.0.0.1 --port 8788`
(`--http-host` / `--http-port` on the install move it). Clients reach it by URL
rather than by command:

```json
{
  "mcpServers": {
    "thread-archive": { "url": "http://127.0.0.1:8788/mcp" }
  }
}
```

The HTTP transport is the same unauthenticated full read as the stdio one, over
a loopback port, so it rejects a non-loopback `Host` (DNS-rebinding defense) and
refuses a non-loopback bind unless `THREAD_ARCHIVE_MCP_NONLOCAL` is set
deliberately. See [SECURITY.md](../SECURITY.md).

## Catch-up ingest

The process is read-only by default in both transports.
`THREAD_ARCHIVE_MCP_INGEST=1` opts it into local lazy catch-up ingest —
throttled, and cross-process-safe via the ingest-owner lock, so it degrades to a
no-op lock probe when the always-on watcher owns ingestion.

Turn it on where nothing else keeps the archive current: `setup` sets it on
generated stdio entries when the watcher is skipped, and the shared server takes
it from `thread-archive service install --mcp --mcp-ingest`. Leave it off when
the watcher is installed.
