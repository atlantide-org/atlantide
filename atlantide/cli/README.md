# atlantide.cli

The `atlantide` command line: a thin wrapper over `Engine` plus the rendering,
option, and error plumbing around it.

| Module | Purpose |
| --- | --- |
| `main.py` | The Typer app and the resource-facing commands: `plan`, `apply`, `destroy`, `refresh`, `import`, `graph`, `build`, `verify`, `deploy`, `resources`, `schema`. Also the `secret` group and the engine/provider wiring. |
| `init.py` | `init`: scaffolds a project, then compiles what it wrote before reporting success. |
| `templates.py` | The starter projects `init` writes, as inline strings — package data would be missing from the PyInstaller binary and CI could not see it. |
| `state.py` | The `state` group: `check`, `migrate`, `unlock`. |
| `component.py` | The `component` group: `add`, `lock`, `vendor`, `verify`. |
| `target.py` | Resolves the invocation's context: which profile, which project, which state backend. |
| `project.py` | Per-project defaults read from `atlantide.toml`, including `[profile.<name>]` overlays. |
| `options.py` | Option types and confirmation prompts shared by more than one command. |
| `render.py` | Human-readable Rich rendering of plans, reports, and drift. |
| `progress.py` | Live per-node progress table for apply, deploy, and destroy. |
| `diagram.py` | Graphviz dot and Mermaid export for `graph`. |
| `json_out.py` | Machine-readable `--json` output. |
| `introspect.py` | Resource-type introspection behind `resources` and `schema`. |
| `errors.py` | Async-run bridging, `ExceptionGroup` flattening, diagnostics, exit codes. |
| `console.py` | The shared Rich console. |

Every command that touches state announces which state it is: with a shared
backend, "no changes" and "wrong target" are otherwise indistinguishable until
something is destroyed. Mutating commands require confirmation, and sensitive
values are redacted at this boundary.
