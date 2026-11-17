"""Resource sidecar: serves this machine's resource use to a Deskmate status page.

Every machine the status page should chart runs one, the machine of the status page itself included:
the status page measures nothing on its own, so what it shows does not depend on its OS. The sidecar
follows the processes its own config lists, reads them (Linux: /proc and cgroups; Windows and macOS:
psutil) and the GPU, and answers `GET /samples` with the current numbers. It keeps no history: the
status page pulls it every interval and stores what it gets.

    python3 -m status_page.resources_agent --config ~/.config/deskmate/resources-agent.toml
    python3 -m status_page.resources_agent --new-token my-status-page     # make a client's token

Each client (a status page instance) has its own token, so one can be revoked without touching the rest.
The sidecar keeps only a hash of each token. The full contract, and how to write a sidecar of your own
for an OS or device this one does not cover, is docs/SPEC_RESOURCE_SIDECAR.md. Standard library only
(plus psutil for that backend); it needs status_page/__init__.py, resources_collect.py and this file,
and Python 3.11+.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from status_page.resources_collect import (
    BACKENDS,
    GPU_PROVIDERS,
    BackendError,
    Collector,
    Target,
)

PROTOCOL_VERSION = 1
CACHE_S = 5.0  # a second request this soon gets the same answer, so a busy caller cannot hammer docker or nvidia-smi
CLIENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(ValueError):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(client: str) -> tuple[str, str]:
    """(the token to give the client, the config table that lets the sidecar recognise it)."""
    if not CLIENT_NAME.match(client):
        raise ConfigError(
            "a client name is letters, digits, - and _ (at most 64)"
        )
    token = secrets.token_urlsafe(32)
    return token, f'[client.{client}]\ntoken_sha256 = "{hash_token(token)}"\n'


@dataclass
class AgentConfig:
    host: str = "127.0.0.1"
    port: int = 8781
    open: bool = (
        False  # allow listening beyond this machine with no clients configured
    )
    backend: str = "auto"
    gpu: str = "auto"
    gpu_container: str = ""
    clients: dict[str, str] = field(
        default_factory=dict
    )  # client name -> sha256 of its token
    targets: dict[str, Target] = field(default_factory=dict)


def _secret(value: str) -> str:
    if value.startswith("env:"):
        name = value[4:]
        resolved = os.environ.get(name)
        if not resolved:
            raise ConfigError(
                f"environment variable {name!r} referenced by the config is not set"
            )
        return resolved
    return value


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def parse_config(raw: dict) -> AgentConfig:
    known = {
        "host",
        "port",
        "open",
        "backend",
        "gpu",
        "gpu_container",
        "client",
        "process",
    }
    extra = set(raw) - known
    if "token" in extra:
        raise ConfigError(
            "a single shared 'token' is gone: give each client its own [client.<name>] table"
        )
    if extra:
        raise ConfigError(f"unknown settings: {', '.join(sorted(extra))}")
    cfg = AgentConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8781)),
        open=bool(raw.get("open", False)),
        backend=str(raw.get("backend", "auto")),
        gpu=str(raw.get("gpu", "auto")),
        gpu_container=str(raw.get("gpu_container", "")),
    )
    if cfg.backend not in BACKENDS:
        raise ConfigError("backend must be " + ", ".join(BACKENDS))
    if cfg.gpu not in GPU_PROVIDERS:
        raise ConfigError("gpu must be " + ", ".join(GPU_PROVIDERS))
    if cfg.gpu == "xpu-smi" and not cfg.gpu_container:
        raise ConfigError(
            "gpu = xpu-smi needs gpu_container (xpu-smi runs inside that container)"
        )
    if not 1 <= cfg.port <= 65535:
        raise ConfigError("port must be 1-65535")
    for name, entry in (raw.get("client") or {}).items():
        if not CLIENT_NAME.match(str(name)):
            raise ConfigError(
                f"client {name!r}: a name is letters, digits, - and _ (at most 64)"
            )
        digest = entry.get("token_sha256")
        token = entry.get("token")
        if (digest is None) == (token is None) or set(entry) - {
            "token",
            "token_sha256",
        }:
            raise ConfigError(
                f"client.{name}: set exactly one of token_sha256 or token"
            )
        digest = (
            str(digest).lower()
            if digest is not None
            else hash_token(_secret(str(token)))
        )
        if not SHA256_HEX.match(digest):
            raise ConfigError(
                f"client.{name}: token_sha256 is 64 hex digits (make one with --new-token {name})"
            )
        if digest in cfg.clients.values():
            raise ConfigError(
                f"client.{name}: has the same token as another client"
            )
        cfg.clients[str(name)] = digest
    for name, entry in (raw.get("process") or {}).items():
        try:
            cfg.targets[str(name)] = Target.from_config(dict(entry))
        except ValueError as exc:
            raise ConfigError(f"process.{name}: {exc}") from exc
    if not cfg.targets:
        raise ConfigError("no [process.<name>] tables: nothing to follow")
    if not _is_loopback(cfg.host) and not cfg.clients and not cfg.open:
        raise ConfigError(
            f"listening on {cfg.host} with no [client.<name>] would show this machine's usage to anyone who can "
            "reach it: add a client (--new-token NAME makes one), or open = true if the firewall is the protection"
        )
    return cfg


def load_config(path: Path) -> AgentConfig:
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_config(raw)


def client_for(clients: dict[str, str], presented: str) -> str | None:
    """The name of the client whose token this is, or None. Compares every client, so how long it takes
    does not say which one was close."""
    digest = hash_token(presented).encode()
    found = None
    for name, stored in clients.items():
        if hmac.compare_digest(digest, stored.encode()):
            found = name
    return found


def make_server(cfg: AgentConfig) -> ThreadingHTTPServer:
    collector = Collector(cfg.targets, cfg.gpu, cfg.gpu_container, cfg.backend)
    lock = threading.Lock()
    cache: dict = {"at": 0.0, "rows": []}
    seen: set[str] = set()

    def rows() -> list[dict]:
        with lock:
            if time.monotonic() - cache["at"] > CACHE_S:
                cache["rows"] = asyncio.run(collector.collect())
                cache["at"] = time.monotonic()
            return cache["rows"]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if cfg.clients:
                header = self.headers.get("Authorization", "")
                name = (
                    client_for(cfg.clients, header[7:])
                    if header.startswith("Bearer ")
                    else None
                )
                if name is None:
                    return self._send(401, {"error": "unauthorized"})
                if (
                    name not in seen
                ):  # who is asking, once, so the journal shows which clients are in use
                    seen.add(name)
                    print(
                        f"resources_agent: client {name} connected from {self.client_address[0]}",
                        flush=True,
                    )
            if self.path == "/samples":
                return self._send(
                    200,
                    {
                        "version": PROTOCOL_VERSION,
                        "now": time.time(),
                        "rows": rows(),
                    },
                )
            if self.path == "/health":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def log_message(
            self, format: str, *args
        ) -> None:  # quiet: one line per poll would fill the journal
            pass

    return ThreadingHTTPServer((cfg.host, cfg.port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--config", type=Path, help="the sidecar's TOML config")
    parser.add_argument(
        "--new-token",
        metavar="CLIENT",
        help="make a token for a client and print the config to add",
    )
    args = parser.parse_args()
    try:
        if args.new_token:
            token, table = new_token(args.new_token)
            print(
                f"Token for client '{args.new_token}'. Give it to that client now; it is not stored anywhere here:"
            )
            print(f"\n    {token}\n")
            print(
                "Add this to the sidecar's config (it holds only a hash) and restart the sidecar:\n"
            )
            print(table)
            return
        if not args.config:
            parser.error("--config is required")
        cfg = load_config(args.config.expanduser())
        server = make_server(cfg)
    except (ConfigError, BackendError) as exc:
        raise SystemExit(f"resources_agent: {exc}") from exc
    names = ", ".join(cfg.targets)
    print(
        f"resources_agent: following {names} on {cfg.host}:{cfg.port} ({server_backend(cfg)}; "
        f"{len(cfg.clients)} client(s))",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


def server_backend(cfg: AgentConfig) -> str:
    from status_page.resources_collect import make_backend

    return make_backend(cfg.backend).name


if __name__ == "__main__":
    main()
