# MongoDB consistency project submission

The submission archive contains the compiled report PDF and the runtime code
needed to reproduce the MongoDB campaigns. The checked-in LaTeX source and
analysis tools remain in the repository and are not copied into the archive.
Campaign manifests identify complete runs and record their provenance.

## Rebuild the report and archive

Use a checkout with the Python dependencies and LaTeX tools installed:

~~~bash
python3 -m pip install -r requirements-dev.txt
make test
make check-docs
make check-schemas
make check-runner-isolation
make submission
~~~

The build writes the report PDF, a filtered runtime-code archive, and a
SHA-256 manifest under `output/`. The manifest records the base Git revision
and working-tree state; its checksums identify the packaged snapshot under
`source/`.

## Reproduce the campaigns

Docker is required for setup and live campaigns. From a clean checkout with
the frozen prediction, protocol, and runner provenance:

~~~bash
make setup
make smoke
make pilot
make experiment
make rq2
~~~

Smoke checks the execution machinery and is not scientific evidence. Pilot
histories remain separate from the main campaign. RQ1 runs sequentially. RQ2
uses a separate manifest and groups histories that share a fault event.

## Contents and metadata

The archive includes the experimental protocol, configuration and schedule
inputs, schemas, runtime source, and package manifest. It does not include
LaTeX sources, bibliography files, analysis scripts, tests, campaign histories,
or local build output.

Replace pending team identifiers in `submission/metadata.mk` and run
`make check-release-ready` before upload. The report's AI Usage Statement
describes the assistance used to organize and edit the report; the project
group remains responsible for checking the sources, measurements, commands,
explanations, and final PDF.
