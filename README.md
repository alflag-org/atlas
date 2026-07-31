# Atlas

Atlas is a host-side runtime for operating independent infrastructure repositories through small
UNIX commands. It installs versioned releases, exposes only declared commands on `PATH`, runs
non-public jobs, records correlated execution logs, and installs Atlas-owned systemd artifacts.

Atlas does not own Ansible inventory or playbooks, Chef policy, Terraform definitions,
site-specific desired state, Git state for infrastructure repositories, or secret values.

## Filesystem contract

```text
/etc/atlas/
├── config.yml
├── host.yml
├── jobs.d/
└── env/

/opt/atlas/
├── bin/
│   ├── atlas
│   └── artifact-runner
├── runtime/
├── releases/
├── current/
├── shims/
├── lib/
└── tmp/

/var/lib/atlas/
├── logs/
├── locks/
└── cache/
```

The former `/opt/atlas/scripts` layout and `atlas scripts` CLI are not supported. Upgrade by
performing a clean installation and reinstalling releases from their declared sources.

## Development

```bash
mise install
mise run setup
mise run check
make html SPHINXOPTS=-W
```

`mise run check` runs Ruff, the test suite with 100% line and branch coverage, and the Python
package build.

The Docker development and runtime targets use the same final filesystem and CLI contracts:

```bash
docker compose build
docker compose run --rm check
docker compose run --rm atlas atlas release list
```

## Host configuration

`/etc/atlas/config.yml` is strict: unknown keys and legacy `scripts` configuration are rejected.

```yaml
runtime:
  python:
    version: "3.14.6"

releases:
  operations:
    source: "https://github.com/alflag-org/atlas/releases/download/v1.0.0/atlas-operations-1.0.0.tar.gz"
    enabled: true
```

Each configured source must resolve directly to an Atlas release directory or archive. Supported
source forms are:

- local directory or `file://` directory
- `.tar`, `.tar.gz`, `.tgz`, or `.zip` archive
- HTTP(S) archive
- `git+<repository>#<ref>`

Atlas does not clone, pull, or switch branches in the infrastructure repository used as the
command working directory. Git access above applies only to Atlas release acquisition.

## Release contract

Every release has a non-empty `VERSION` and a strict `release.yml`.

```yaml
schema: atlas.release/v1
name: operations

commands:
  config-diff:
    runtime: python
    entrypoint: commands/config-diff.py

jobs:
  inventory-refresh:
    runtime: python
    entrypoint: jobs/inventory-refresh.py
    default_timeout_seconds: 300

services:
  inventory-refresh:
    job: inventory-refresh
    init:
      systemd:
        service: init/systemd/inventory-refresh.service
        timer: init/systemd/inventory-refresh.timer
```

Only manifest commands receive shims. Jobs remain off `PATH` and run through `atlas job`.
Entrypoints must be relative Python files inside the release. Atlas rejects unknown manifest keys,
unsupported runtimes, path traversal, symlinks, invalid references, and cross-release command
collisions.

Release installation keeps the previous version directory and active link recoverable until
launcher and shim refresh succeeds. A failed install or multi-release update restores the previous
directories, links, and command shims, including when an existing version is being replaced.

## CLI

```bash
atlas status
atlas runtime status
atlas runtime install

atlas release install ./operations
atlas release update [release]
atlas release list [--verbose]
atlas release shims

atlas command list [--verbose]
atlas which config-diff
atlas run config-diff site web01

atlas job list [release]
atlas job inspect <release> <job>
atlas job run <release> <job> [-- <args>...]

atlas job instance list
atlas job instance inspect <instance>
atlas job instance run <instance>

atlas init list [release]
atlas init diff <release> <service>
atlas init install <release> <service>
atlas init remove <release> <service>
```

`atlas init install` and `remove` manage only manifest-declared Atlas service artifacts. They
atomically replace standardized `atlas-<release>-<service>.service` and `.timer` files and run
`systemctl daemon-reload`. They never enable, start, stop, or restart services.

## Job instances

Named job invocations live in `/etc/atlas/jobs.d/<instance>.yml`.

```yaml
schema: atlas.job-instance/v1
release: operations
job: inventory-refresh
user: ops
working_directory: /home/ops/repos/provisioning
arguments:
  - --site
  - default
environment_files:
  - /etc/atlas/env/provisioning.env
timeout_seconds: 300
lock: provisioning-inventory-refresh
```

Atlas does not invoke `sudo`. A direct instance run fails when `user` differs from the caller. For
a unit that runs a job instance, Atlas verifies that the native `User=` setting matches the
instance user and that the instance references the service's release and job. Every managed
service must have exactly one `ExecStart` through `/opt/atlas/bin/atlas`; a command-backed service
must invoke its declared command, and a job-backed service must invoke a matching job instance.
Instance locks use non-blocking OS advisory locks under `/var/lib/atlas/locks`. Timeouts terminate
the complete child process group, then kill it if graceful termination does not complete.

## First-party operation artifacts

The repository contains a separately packaged `operations` release:

```text
config-validate <playbook>
config-check <playbook> <target>
config-diff <playbook> <target>
config-apply <playbook> <target>
inventory-show
config-diff-many <playbook> [target...]
inventory-refresh --site <site>  # non-public job

proxmox-status <provider>
vm-create-plan <provider> <input>
vm-create-apply <provider> [plan] --confirm <plan-id>
vm-create-verify <provider> [plan-or-evidence]
vm-create-rollback <provider> [evidence] --confirm <plan-id>
vm-template-create-plan <provider> <input>
vm-template-create-apply <provider> [plan] --confirm <plan-id>
vm-template-create-verify <provider> [plan-or-evidence]
vm-template-create-rollback <provider> [evidence] --confirm <plan-id>
operation-artifact-validate [artifact]
operation-artifact-inspect [artifact]
```

These artifacts treat the caller's current directory as an Ansible project. Commands require
`ansible.cfg` and `playbooks/<name>.yml`; the refresh job additionally requires
`inventories/<site>/hosts.yml`. They do not install dependencies, change Git state, or discover
projects. `config-diff-many` calls the public `config-diff` executable as a child process; it does
not import the primitive implementation.

The Proxmox commands require explicit, strict provider and operation input files. Plan and
evidence JSON are emitted only on stdout. Apply and rollback bind the provider and input file
digests, require the exact plan ID, and never discover Ares configuration or Daedalus inventory.
See [Reviewed Proxmox operations](docs/reviewed-operations.rst).

```bash
cd /home/ops/repos/provisioning
config-validate site
config-diff site web01
printf '%s\n' web01 web02 | config-diff-many site
config-apply site web01
atlas job run operations inventory-refresh -- --site default
```

The release workflow publishes `atlas-operations-<version>.tar.gz` alongside the Atlas wheel.
Global Registry installation remains unavailable until that service exposes a documented
software-release resource and mutation API; Atlas does not treat a host-local alias map as registry
integration.

## Execution records

Every command and job appends one JSON object to `/var/lib/atlas/logs/runs.jsonl`. Records include:

- `run_id`, `parent_run_id`, and `operation_id`
- release, version, artifact type, artifact name, redacted arguments, and working directory
- exit code, duration, timeout outcome, and lock name
- Git root, commit, dirty state, and branch when the working directory belongs to a Git repository

Child shims inherit the parent run and operation identifiers. Atlas places `/opt/atlas/shims` and
the runtime environment at the front of child `PATH`, preserves the caller's remaining `PATH`, and
executes with `shell=False`. The command line printed to stderr uses the same redacted arguments as
the run record.

Release code should use `atlas_core`, not host-side modules under `atlas`:

```python
from atlas_core import get_context

context = get_context()
print(context.artifact.name)
print(context.artifact.operation_id)
print(context.host.name)
```

## Documentation

The Sphinx documentation is built into `build/html` and hosted as Cloudflare Workers static assets
using `wrangler.jsonc`.
