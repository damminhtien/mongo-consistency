# MongoDB consistency submission

This archive contains `report.pdf`, the runtime source and scripts under
`source/`, and the commands needed to reproduce the MongoDB replica-set
experiments. The report source, tests, and local build output are not part of
the archive. Campaign histories are created under `source/results/` when the
commands run.

## Requirements

- Docker Engine 29.8.0 and Docker Compose 5.5.1
- Python 3.14.7 for the host setup and coordinator commands
- GNU Make

## Reproduce the main experiments

Run these commands from the directory containing this README after Docker is
ready:

```text
make setup
make smoke
make pilot
make experiment
make rq2
```

`experiment` is the main normal-operation campaign (RQ1); `rq2` is the main
failure and partition campaign. Smoke validates the execution machinery and
pilot histories are kept separate from the main evidence. The runner writes
histories under `source/results/raw/` and setup provenance under
`source/results/setup/`. The Compose runner has no Docker socket and receives
only the result, figure, and setup-provenance mounts defined in
`source/compose.yaml`.

The optional registered mechanism replay is available when it is part of the
assessment scope:

```text
make rq3-preflight
make rq3
```

Resume an interrupted campaign without changing its frozen inputs with, for
example, `make rq3 RQ3_ARGS=--resume`.

The SHA-256 `manifest.txt` records the source revision and checksums for every
file in the archive. Main campaigns require the clean, committed snapshot
recorded there; setup uses that manifest when the archive has been extracted
without a `.git` directory.
