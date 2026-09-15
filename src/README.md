# Experiment source

The package contains the history model, canonical serializer, offline checkers, and configuration loader. The generator and checker remain separate so that a saved history can be replayed without MongoDB.

The live PyMongo runner, command monitor, Compose harness, fault controller, and campaign entry points are implemented in the current slice. Offline analysis is added separately. The checkers accept saved histories and classify PASS, VIOLATION, UNAVAILABLE, INDETERMINATE, HARNESS_ERROR, and UNSUPPORTED.
