# atlantide.providers.random

Values generated once at apply and pinned in state: ids, suffixes, passwords.

Config is a pure function of its inputs and cannot generate randomness itself,
so anything that must be unique-but-stable is declared as a resource. The value
is produced by the provider on create and echoed back on read, which keeps the
node a NOOP on every later plan.

| Module | Purpose |
| --- | --- |
| `resources.py` | `Uuid`, `Password` (sensitive `result`, sealed at rest), `Id`. All carry `keepers`: arbitrary values that force regeneration when they change. |
| `provider.py` | `RandomProvider`: generates on create, returns the pinned value on read. |
