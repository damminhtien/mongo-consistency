"""Small sidecar that blocks only replica-network traffic in its namespace."""

from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

CHAIN = "MONGO_CONSISTENCY"
INTERFACE = os.environ.get("REPLICA_INTERFACE", "eth1")
MEMBER = os.environ.get("MEMBER", "unknown")
PORT = int(os.environ.get("CONTROL_PORT", "29092"))


def iptables(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["iptables", *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def ensure_chain() -> None:
    created = iptables("-N", CHAIN, check=False)
    if created.returncode not in {0, 1}:
        raise RuntimeError(created.stderr.strip() or "cannot create iptables chain")
    iptables("-F", CHAIN)
    iptables("-A", CHAIN, "-j", "DROP")


def add_jump(direction: str, port_flag: str) -> None:
    rule = ["-i" if direction == "INPUT" else "-o", INTERFACE, "-p", "tcp", port_flag, "27017", "-j", CHAIN]
    if iptables("-C", direction, *rule, check=False).returncode != 0:
        iptables("-I", direction, "1", *rule)


def remove_jump(direction: str, port_flag: str) -> None:
    rule = ["-i" if direction == "INPUT" else "-o", INTERFACE, "-p", "tcp", port_flag, "27017", "-j", CHAIN]
    while iptables("-C", direction, *rule, check=False).returncode == 0:
        iptables("-D", direction, *rule)


def isolate() -> None:
    ensure_chain()
    add_jump("INPUT", "--dport")
    add_jump("OUTPUT", "--sport")


def heal() -> None:
    remove_jump("INPUT", "--dport")
    remove_jump("OUTPUT", "--sport")
    iptables("-F", CHAIN, check=False)


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._send({"ok": False, "error": "not found"}, 404)
            return
        try:
            iptables("-L", "INPUT", check=True)
            self._send({"ok": True, "member": MEMBER, "interface": INTERFACE})
        except subprocess.CalledProcessError as error:
            self._send({"ok": False, "error": error.stderr.strip()}, 503)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/apply":
            self._send({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            action = payload.get("action")
            if action == "isolate":
                isolate()
            elif action == "heal":
                heal()
            else:
                self._send({"ok": False, "error": "action must be isolate or heal"}, 400)
                return
            self._send(
                {
                    "ok": True,
                    "member": MEMBER,
                    "action": action,
                    "event_id": payload.get("event_id"),
                }
            )
        except (ValueError, json.JSONDecodeError, OSError, subprocess.CalledProcessError) as error:
            self._send({"ok": False, "error": str(error)}, 500)

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
