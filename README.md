# Atlas

Atlas runs locally installed Python and native automation programs. It selects Python
interpreters, creates a virtual environment per Python program, and exposes commands through
`atlas run` and generated shims. Each run receives host identity and execution context and
produces a JSONL record.

Install and update program sources and dependencies with their own tooling. Infrastructure
state, provider operations, inventory, workflows, and scheduling belong to those programs.

## Configuration

`/etc/atlas/config.yml` registers programs that already exist on the host:

```yaml
runtime:
  python:
    version: "3.13"

programs:
  provisioning:
    root: /srv/provisioning
    runtime:
      type: python
      python: "3.13"
      venv: provisioning
  pve-tools:
    root: /opt/pve-tools
    runtime:
      type: native
```

Python commands are discovered below `commands/**/*.py`. Native commands are executable files
below `commands/` or `bin/`. Nested paths become hyphenated command names, so
`commands/host/diff.py` becomes `host-diff`. Duplicate names fail closed.

The host identity is separate from program configuration:

```yaml
version: 1
host:
  id: control01
  role: control
  site: kanagawa01
```

`host.yml` contains only the identity of the host running Atlas. It is not an inventory or a
secret store.

## Execution

```bash
atlas runtime install
atlas venv create provisioning
atlas command list
atlas shim generate
export PATH="/opt/atlas/shims:$PATH"
atlas run host-diff web01
host-diff web01
```

Both invocations resolve the current registered program and enter the same executor. A Python
command runs with its program venv; a native command runs directly. The child receives
`ATLAS_CONTEXT_FILE` plus the `ATLAS_*` identifiers. The JSON context contains the host, Atlas
paths, program, command, working directory, and run identifiers, so non-Python programs can read
the same information without importing `atlas_core`.

Python programs can read the context through `atlas_core`:

```python
from atlas_core import get_context

context = get_context()
print(context.host.id, context.execution.run_id)
```

Execution facts are appended to `/var/lib/atlas/logs/runs.jsonl`. Atlas records the timestamp,
host, user, program, command, identifiers, duration, and exit status; command output remains the
responsibility of the invoking terminal, service manager, or CI system.

## External secrets

Programs can use `atlas_core.secrets` to retrieve logical secret names from an external
secret manager. Host-owned mappings and owner-only bootstrap credentials stay outside
automation repositories. `atlas secret check <logical-name> ...` verifies availability
without displaying values. See [the operator reference](docs/reference.rst) for setup,
program integration, and recovery.

## Independent daemons

Write a foreground Python daemon with `atlas_core.daemon`. The SDK handles readiness,
SIGTERM/SIGINT, and cooperative shutdown. Run the application directly with Atlas installed
in its Python environment. systemd is optional.

```bash
# From a checkout, with Atlas installed in this interpreter:
cd examples/daemon
python -m sampled
# Ctrl-C stops the example gracefully.
```

See [the Python API](docs/api.rst) for lifecycle and shutdown behavior, and
[the operator reference](docs/reference.rst#standalone-daemon-environment) for directory setup.

## Development

The repository uses mise:

```bash
mise run setup
mise run check
make clean-docs html SPHINXOPTS=-W
```
