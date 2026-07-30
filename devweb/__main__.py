"""``python -m devweb`` — run the dev panels in the foreground.

A foreground process on purpose. These are instruments you open while working on
the archive, so the natural lifetime is the terminal you started them in; there
is no always-on story here and no service unit to install. The archive's own
daemons keep running whether or not this is up — nothing depends on it.
"""

from __future__ import annotations

import argparse
import sys
import threading

from . import DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m devweb",
        description="The archive's dev panels: retrieval report, telemetry, search lab.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind host (non-loopback refused unless "
                             "THREAD_ARCHIVE_WEB_NONLOCAL=1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"bind port (default {DEFAULT_PORT})")
    parser.add_argument("--open", action="store_true",
                        help="open the panels in a browser once they are up")
    args = parser.parse_args(argv)

    from .server import serve

    try:
        httpd = serve(host=args.host, port=args.port)
    except (ValueError, OSError) as e:
        print(f"devweb: {e}", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{args.port}"
    print(f"dev panels on {url}  ({url}/retrieval · {url}/telemetry · {url}/lab)")
    print("ctrl-c to stop")
    if args.open:
        import webbrowser

        webbrowser.open(f"{url}/lab")
    try:
        # serve() runs the loop on a daemon thread, so hold the foreground here.
        threading.Event().wait()
    except KeyboardInterrupt:
        print()  # keep the ^C off the next shell prompt
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
