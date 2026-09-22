"""Small sidecar that blocks only replica-network traffic in its namespace."""

from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

CHAIN = "MONGO_CONSISTENCY"
REPLICA_SUBNET = os.environ.get("REPLICA_SUBNET", "172.20.0.0/16")
MEMBER = os.environ.get("MEMBER", "unknown")
PORT = int(os.environ.get("CONTROL_PORT", "29092"))


def iptables(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["iptables", *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def interface_for_subnet(subnet: str) -> str:
    """Resolve the replica interface from the route table in this namespace."""

    result = subprocess.run(
        ["ip", "route", "show", "exact", subnet],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"cannot inspect route for {subnet}")
    for line in result.stdout.splitlines():
        fields = line.split()
        if "dev" in fields:
            index = fields.index("dev")
            if index + 1 < len(fields) and fields[index + 1]:
                return fields[index + 1]
    raise RuntimeError(f"no interface found for replica subnet {subnet}")


INTERFACE = os.environ.get("REPLICA_INTERFACE") or interface_for_subnet(REPLICA_SUBNET)


def ensure_chain() -> None:
    created = iptables("-N", CHAIN, check=False)
    if created.returncode not in {0, 1}:
        raise RuntimeError(created.stderr.strip() or "cannot create iptables chain")
    iptables("-F", CHAIN)
    iptables("-A", CHAIN, "-j", "DROP")


def jump_rule(direction: str) -> list[str]:
    interface_flag = "-i" if direction == "INPUT" else "-o"
    return [interface_flag, INTERFACE, "-j", CHAIN]


def add_jump(direction: str) -> None:
    rule = jump_rule(direction)
    if iptables("-C", direction, *rule, check=False).returncode != 0:
        iptables("-I", direction, "1", *rule)


def remove_jump(direction: str) -> None:
    rule = jump_rule(direction)
    while iptables("-C", direction, *rule, check=False).returncode == 0:
        iptables("-D", direction, *rule)


def rule_exists(direction: str) -> bool:
    return iptables("-C", direction, *jump_rule(direction), check=False).returncode == 0


def isolation_verified() -> bool:
    chain = iptables("-S", CHAIN, check=False)
    return (
        chain.returncode == 0
        and any(line.strip().endswith("-j DROP") for line in chain.stdout.splitlines())
        and rule_exists("INPUT")
        and rule_exists("OUTPUT")
    )


def isolate() -> None:
    ensure_chain()
    add_jump("INPUT")
    add_jump("OUTPUT")
    if not isolation_verified():
        raise RuntimeError("replication-path isolation rules could not be verified")


def heal() -> None:
    remove_jump("INPUT")
    remove_jump("OUTPUT")
    iptables("-F", CHAIN, check=False)
    if isolation_verified():
        raise RuntimeError("replication-path isolation rules remain after heal")


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
            self._send(
                {
                    "ok": True,
                    "member": MEMBER,
                    "interface": INTERFACE,
                    "replication_isolated": isolation_verified(),
                }
            )
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
                    "verified": (
                        isolation_verified() if action == "isolate" else not isolation_verified()
                    ),
                    "replication_isolated": isolation_verified(),
                }
            )
        except (ValueError, json.JSONDecodeError, OSError, subprocess.CalledProcessError) as error:
            self._send({"ok": False, "error": str(error)}, 500)

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
