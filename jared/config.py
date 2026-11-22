"""Paths and the config file of the service user.

Everything lives in the XDG directories of that user: config and credentials in
$XDG_CONFIG_HOME/jared (default ~/.config/jared), state in $XDG_STATE_HOME/jared
(default ~/.local/state/jared). Set the two variables in the service unit to move them.

config.toml:

    [update]
    repo = "your-name/jared"         # where the releases are
    publisher = "your-github-login"  # only releases published by this login are installed

    [server]
    port = 8769                      # optional
    app_port = 8772                  # optional: the page behind app links (see app_links)

    [paperless]
    url = "http://127.0.0.1:8000"    # the Paperless-ngx API, on this machine only (http on loopback)
    link = "https://paperless.example.ts.net"  # optional: the address the user opens documents at
    deadline_field = "Kündbar bis"   # optional: the date custom field archiv_fristen lists
    app_links = true                 # optional: links open the Swift Paperless app through <link>/app/<number>
                                     # (needs link, and a proxy that sends /app to app_port; see jared/apppage.py)

Credentials, one file each (mode 600): token-<client> (the bearer token of a chat client) and paperless-token
(the API token of a Paperless account that may only VIEW).
"""
from __future__ import annotations

import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PORT = 8769
APP_PORT = 8772
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class UpdateConfig:
    repo: str
    publisher: str


@dataclass(frozen=True)
class PaperlessConfig:
    url: str = "http://127.0.0.1:8000"
    link: str = ""
    deadline_field: str = "Kündbar bis"
    app_links: bool = False


@dataclass(frozen=True)
class Paths:
    conf: Path
    state: Path

    @classmethod
    def default(cls, env=os.environ, home: Path | None = None) -> "Paths":
        home = home or Path.home()

        def base(var: str, fallback: str) -> Path:
            value = env.get(var, "")
            return Path(value) if value.startswith("/") else home / fallback  # XDG: relative values are ignored

        return cls(base("XDG_CONFIG_HOME", ".config") / "jared", base("XDG_STATE_HOME", ".local/state") / "jared")

    @property
    def config(self) -> Path:
        return self.conf / "config.toml"

    @property
    def paperless_token(self) -> Path:
        return self.conf / "paperless-token"

    def tokens(self) -> dict[str, str]:
        """Every chat client with a token file: {client name: token}."""
        found = {p.name[len("token-"):]: load_token(p) for p in sorted(self.conf.glob("token-*")) if p.is_file()}
        if not found:
            raise ConfigError(f"no token file (token-<client>) in {self.conf.name}")
        for name in found:
            if not NAME_RE.fullmatch(name):
                raise ConfigError(f"token-{name}: a client name is lowercase letters, digits and dashes")
        return found


def _load(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path.name}: {type(e).__name__}") from None


def _table(data: dict, key: str) -> dict | None:
    section = data.get(key)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError(f"[{key}] must be a table")
    return section


def load_update(path: Path) -> UpdateConfig | None:
    """The [update] section, or None when updates are not configured."""
    section = _table(_load(path), "update")
    if section is None:
        return None
    repo, publisher = section.get("repo"), section.get("publisher")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", repo):
        raise ConfigError("[update] repo must look like owner/name")
    if not isinstance(publisher, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", publisher):
        raise ConfigError("[update] publisher must be a GitHub login")
    return UpdateConfig(repo, publisher)


def _port(section: dict, key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
        raise ConfigError(f"[server] {key} must be a whole number from 1024 to 65535")
    return value


def load_port(path: Path) -> int:
    return _port(_table(_load(path), "server") or {}, "port", PORT)


def load_app_port(path: Path) -> int:
    """The port of the page behind app links; never the port of the MCP endpoint."""
    section = _table(_load(path), "server") or {}
    port = _port(section, "app_port", APP_PORT)
    if port == _port(section, "port", PORT):
        raise ConfigError("[server] app_port must differ from port")
    return port


def load_paperless(path: Path) -> PaperlessConfig:
    """The [paperless] section. The API must be on this machine: the token is sent there and nowhere else."""
    section = _table(_load(path), "paperless") or {}
    url = section.get("url", PaperlessConfig.url)
    link = section.get("link", "")
    field = section.get("deadline_field", PaperlessConfig.deadline_field)
    app_links = section.get("app_links", False)
    if not isinstance(url, str):
        raise ConfigError("[paperless] url must be text")
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http" or u.hostname not in LOOPBACK or u.username or u.query or u.fragment or u.path not in ("", "/"):
        raise ConfigError("[paperless] url must be http://127.0.0.1:<port> (the API on this machine)")
    if not isinstance(link, str) or (link and not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?/?", link)):
        raise ConfigError("[paperless] link must be an address like https://host:port")
    if not isinstance(field, str) or not 0 < len(field) <= 128:
        raise ConfigError("[paperless] deadline_field must be the name of a custom field")
    if not isinstance(app_links, bool):
        raise ConfigError("[paperless] app_links must be true or false")
    if app_links and not link:
        raise ConfigError("[paperless] app_links needs link (the address the app page is served under)")
    return PaperlessConfig(url.rstrip("/"), link.rstrip("/"), field, app_links)


def load_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
    except OSError:
        raise ConfigError(f"cannot read {path.name}") from None
    if len(token) < 32 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ConfigError(f"{path.name} is not a token (too short or odd characters)")
    return token
