# Atlas

Atlas runs locally installed Python, Perl, and native automation programs. It selects
interpreters, creates a dependency environment per program, and exposes commands through
`atlas run` and generated shims. Each run receives host identity and execution context and
produces a JSONL record.

Install and update program sources and dependencies with their own tooling. Infrastructure
state, provider operations, inventory, and scheduling belong to those programs.

## Configuration

`/etc/atlas/config.yml` registers programs that already exist on the host:

```yaml
runtime:
  python:
    version: "3.13"
  perl:
    version: "5.40.0"

programs:
  provisioning:
    root: /srv/provisioning
    runtime:
      type: python
      environment: provisioning
  inventory:
    root: /srv/inventory
    runtime:
      type: perl
      environment: inventory
  pve-tools:
    root: /opt/pve-tools
    runtime:
      type: native
```

Python and Perl commands are discovered below `commands/**/*.py` and `commands/**/*.pl`,
respectively. Native commands are executable files
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
atlas env create provisioning
atlas env create inventory
# From this checkout: install the Python SDK as a program dependency.
/opt/atlas/environments/python/provisioning/bin/python -m pip install ./src/python
# Install the Perl SDK as described in the operator reference.
atlas command list
atlas shim generate
export PATH="/opt/atlas/shims:$PATH"
atlas run host-diff web01
host-diff web01
```

Both invocations resolve the current registered program and enter the same executor. Python uses pyenv + venv; Perl uses plenv + local::lib. An administrator can also select an
explicit interpreter. Install dependencies with the program's tooling; Atlas creates empty
environments. A native command runs directly. The child receives
`ATLAS_CONTEXT_FILE` plus the `ATLAS_*` identifiers. The JSON context contains the host, Atlas
paths, program, command, working directory, and run identifiers, so non-Python programs can read
the same information.

Python programs can read the context through `atlas_core`:

```python
from atlas_core import get_context

context = get_context()
print(context.host.id, context.execution.run_id)
```

Perl programs use the independently installed SDK:

```perl
use Atlas::Context;
my $context = Atlas::Context->current;
print $context->{host}{id}, "\n";
```

See [the operator reference](docs/reference.rst) for interpreter setup, dependency installation,
and migration from the removed `atlas venv` command.

Execution facts are appended to `/var/lib/atlas/logs/runs.jsonl`. Atlas records the timestamp,
host, user, program, command, identifiers, duration, and exit status; command output remains the
responsibility of the invoking terminal, service manager, or CI system.

## Application logging

Use `atlas_core.logging.get_logger("hermes.dns")` for diagnostics in stderr and
`/var/log/atlas/<program>.log`. Configure a level or absolute path with
`configure_logging()`. Provision file access on the host; unavailable file output
warns and continues on stderr. See [the Python API](docs/api.rst#application-logging)
and [the operator reference](docs/reference.rst#application-diagnostics).

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
cd src/python/examples/daemon
python -m sampled
# Ctrl-C stops the example gracefully.
```

See [the Python API](docs/api.rst) for lifecycle and shutdown behavior, and
[the operator reference](docs/reference.rst#standalone-daemon-environment) for directory setup.

Host-owned daemon declarations can be installed and operated with `atlas daemon`.
Atlas Cluster shares host membership and metadata between independent `atlasd` peers.
Install `atlas[cluster]` on daemon hosts and configure a shared credential plus TCP/UDP endpoints.
Applications choose targets and manage their own execution, locking, and recovery.
See [the operator reference](docs/reference.rst#cluster-membership) for setup, local observations, and migration from the removed SSH Registry.

## Development

Python sources, tests, and packaging live in `src/python`; the Perl SDK lives in `src/perl`.
Both distributions build independently. Development requires a C compiler and a Perl interpreter
with `local::lib` (for example, Debian packages `build-essential` and `liblocal-lib-perl`).
The repository uses mise:

```bash
mise run setup
mise run check
make clean-docs html SPHINXOPTS=-W
```
