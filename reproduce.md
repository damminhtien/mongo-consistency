# Reproduce the main experiments

This guide applies to the `mongo-consistency-submission.zip` attached to GitHub
release `v1.0.5`. It reruns the MongoDB campaigns from the packaged source and
writes new raw histories; the report PDF is already included in the archive.

## Requirements

- Docker Engine 29.8.0 and Docker Compose 5.5.1
- Python 3.14.7 and GNU Make
- Internet access for pulling the pinned container images

Extract the ZIP and run the commands below from the directory containing its
`Makefile`. Keep the packaged files under `source/` unchanged so the recorded
source provenance remains valid.

```sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r source/requirements.txt
```

## Campaigns

Run the main configuration and fault campaigns, then the registered mechanism
replay:

```sh
make experiment
make rq2
make rq3-preflight
make rq3
```

Each target initializes the replica set when needed. `make experiment` runs the
256 planned histories (96 normal and 160 adversarial) across the four client
consistency properties. `make rq2` runs the failure and partition schedules.
Run `make rq3-preflight` before `make rq3`; the preflight output is an input to
the mechanism replay.

Raw histories and campaign manifests are written under
`source/results/raw/`. The setup command records the detected runtime in
`source/results/setup/toolchain.json`. Fault timing can vary between runs, so
compare the recorded history outcomes and campaign manifests rather than
expecting identical traces.
