"""Jared command line (run as the service user).

  python3 -m jared serve                     run the MCP endpoint (jared.service)
  python3 -m jared check                     ask Paperless once with the stored token: is everything reachable?
  python3 -m jared update [--check] [--yes] [--to vX.Y.Z] [--force]
                                             install the newest release published by you on GitHub
  python3 -m jared config-check [--strict]   can this code read the installed config? (--strict: is all set up?)
  python3 -m jared version
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import config, updater
from .version import __version__


def _archive(paths: config.Paths):
    from .archive import Archive
    from .paperless import Client

    cfg = config.load_paperless(paths.config)
    return Archive(Client(cfg.url, config.load_token(paths.paperless_token)), cfg.link, cfg.deadline_field)


def cmd_serve(_: argparse.Namespace) -> int:
    from .bridge import Bridge, serve

    paths = config.Paths.default()
    tokens = paths.tokens()
    port = config.load_port(paths.config)
    bridge = Bridge(_archive(paths), update=config.load_update(paths.config),
                    last_result=lambda: updater.last_result(paths.state))
    server = serve(bridge, tokens, port)
    updater.mark_running(paths.state)
    print(f"jared {__version__} on 127.0.0.1:{port} for {', '.join(sorted(tokens))}")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    return 0


def cmd_config_check(a: argparse.Namespace) -> int:
    paths = config.Paths.default()
    tokens = paths.tokens()
    config.load_port(paths.config)
    update = config.load_update(paths.config)
    paperless = config.load_paperless(paths.config)
    if a.strict:
        config.load_token(paths.paperless_token)
    print(f"config ok (clients: {', '.join(sorted(tokens))}; updates {'on' if update else 'off'}; "
          f"paperless {paperless.url}; links {'on' if paperless.link else 'off'})")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    from .paperless import PaperlessError

    paths = config.Paths.default()
    try:
        print(_archive(paths).overview({}).split("\n", 1)[0])
    except PaperlessError as e:
        print(f"not ok: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_update(a: argparse.Namespace) -> int:
    """Install the newest release published by you on GitHub (see [update] in config.toml), together with the
    newest agent-tool-selfupdate that selfupdate.toml allows."""
    paths = config.Paths.default()
    cfg = config.load_update(paths.config)
    if cfg is None:
        print("update is off: add an [update] section (repo, publisher) to config.toml", file=sys.stderr)
        return 2
    try:
        p = updater.plan(cfg, to=a.to)
    except updater.UpdateRefused as e:
        print(f"no update: {e}", file=sys.stderr)
        return 1
    print(p.summary())
    if a.check:
        print(p.notes(500) if p.newer else "up to date")
        return 0
    if not (p.newer or a.force):
        print("nothing to do")
        return 0
    if not a.yes and input(f"install {p.target}? [y/N] ").strip().lower() != "y":
        return 1
    status = updater.update(cfg, paths.state, to=a.to, force=a.force)
    return 0 if status in ("updated", "current") else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="jared", description="Read-only MCP bridge from a chat agent to Paperless-ngx.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    cc = sub.add_parser("config-check")
    cc.add_argument("--strict", action="store_true", help="also fail when the Paperless token is missing")
    cc.set_defaults(fn=cmd_config_check)
    sub.add_parser("version").set_defaults(fn=lambda _: print(__version__) or 0)
    up = sub.add_parser("update")
    up.add_argument("--check", action="store_true", help="only say what would be installed")
    up.add_argument("--yes", action="store_true", help="do not ask before installing")
    up.add_argument("--to", help="install this tag instead of the newest release (only releases with a selfupdate.toml)")
    up.add_argument("--force", action="store_true", help="also install an older version (never the installed one)")
    up.set_defaults(fn=cmd_update)
    args = parser.parse_args()
    try:
        return args.fn(args)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
