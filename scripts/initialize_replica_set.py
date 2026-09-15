"""Initialize and verify the three-member MongoDB replica set."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from pymongo import MongoClient
from pymongo.errors import AutoReconnect, ConfigurationError, OperationFailure


def wait_for_members(seed_uri: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = MongoClient(
                seed_uri,
                connectTimeoutMS=2000,
                serverSelectionTimeoutMS=2000,
                retryReads=False,
                retryWrites=False,
            )
            status = client.admin.command("replSetGetStatus")
            members = status.get("members", [])
            primary_count = sum(member.get("stateStr") == "PRIMARY" for member in members)
            secondary_count = sum(member.get("stateStr") == "SECONDARY" for member in members)
            if len(members) == 3 and primary_count == 1 and secondary_count == 2:
                client.close()
                return status
            last_error = RuntimeError(f"unexpected replica status: {status}")
            client.close()
        except (AutoReconnect, ConfigurationError, OperationFailure) as error:
            last_error = error
        time.sleep(1)
    raise RuntimeError(f"replica set did not become stable: {last_error}")


def initialize(seed_host: str, timeout_seconds: float) -> dict[str, Any]:
    direct_uri = f"mongodb://{seed_host}:27017/?directConnection=true"
    client = MongoClient(
        direct_uri,
        connectTimeoutMS=2000,
        serverSelectionTimeoutMS=2000,
        retryReads=False,
        retryWrites=False,
    )
    try:
        client.admin.command("ping")
        try:
            client.admin.command("replSetGetConfig")
        except OperationFailure as error:
            if error.code not in {94, 93}:
                raise
            configuration = {
                "_id": "rs0",
                "members": [
                    {"_id": 0, "host": "mongo1:27017", "tags": {"member": "mongo1"}},
                    {"_id": 1, "host": "mongo2:27017", "tags": {"member": "mongo2"}},
                    {"_id": 2, "host": "mongo3:27017", "tags": {"member": "mongo3"}},
                ],
            }
            client.admin.command("replSetInitiate", configuration)
    finally:
        client.close()
    return wait_for_members(
        "mongodb://mongo1:27017,mongo2:27017,mongo3:27017/?replicaSet=rs0",
        timeout_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-host", default="mongo1")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    status = initialize(args.seed_host, args.timeout_seconds)
    payload = {
        "replica_set": status.get("set"),
        "members": [
            {"name": member.get("name"), "state": member.get("stateStr")}
            for member in status.get("members", [])
        ],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
