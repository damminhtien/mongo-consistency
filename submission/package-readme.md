# MongoDB consistency project submission

This archive contains the report sources and the repository material needed to
inspect or reproduce the recorded experiments. Campaign manifests identify
which runs are complete and record their provenance. Derived tables and plots
are rebuilt from canonical histories; smoke and pilot outputs are not treated
as main-campaign evidence.

## Rebuild the report and archive

Use a checkout with the Python dependencies and LaTeX tools installed:

~~~bash
python3 -m pip install -r requirements-dev.txt
make test
make check-docs
make check-schemas
make check-runner-isolation
make analyse
make submission
~~~

The build writes the report PDF, a filtered source/evidence archive, and a
SHA-256 manifest under `output/`. The manifest records the base Git revision
and working-tree state; its checksums identify the exact packaged source
snapshot under `source/`.

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

To derive report tables from saved histories without starting MongoDB, run:

~~~bash
make analyse
make rq4-analyse
make submission
~~~

## Contents and metadata

The archive includes the assignment summary, project plan, experimental
protocol, report plan, configuration and schedule inputs, schemas, source code,
tests, analysis scripts, campaign manifests and histories included in the
source snapshot, report source, bibliography, and package manifest. The full
artifact map is in Appendix D of the report.

Replace pending team identifiers in `submission/metadata.mk` and run
`make check-release-ready` before upload. The report's AI Usage Statement
describes the assistance used to organize and edit the report; the project
group remains responsible for checking the sources, measurements, commands,
explanations, and final PDF.
