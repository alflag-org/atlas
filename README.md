# Atlas

Atlas installs and runs versioned infrastructure operations on a host. A release declares its
commands, jobs, and systemd files in `release.yml`. Atlas validates the release, builds a shared
Python runtime, creates command shims, and records every execution.

Infrastructure repositories stay separate. Atlas runs an artifact in the caller's working
directory and records its Git state, but it does not change desired state, inventory, playbooks,
provider configuration, repository state, or secrets.

## Try Atlas

The repository includes a containerized example. It installs the sample and first-party release,
builds their shared runtime, and checks the public commands without touching host configuration:

```bash
docker compose build
docker compose run --rm atlas
```

## Install Atlas

Atlas supports Python 3.11 through 3.14 on Linux. This example keeps an operator-managed source
checkout at `/srv/atlas/source`:

```bash
git clone https://github.com/alflag-org/atlas.git /srv/atlas/source
python -m pip install /srv/atlas/source
```

Another durable checkout path is valid. Use that same path for the release sources below. Atlas
reads the checkout but does not pull, reset, or otherwise modify it.

`atlas release install`, `atlas release update`, and `atlas runtime install` require `pyenv` on
`PATH` and the operating-system packages needed to build the configured Python version. The account
running Atlas must be able to write its configured home, configuration, and state directories. The
defaults are `/opt/atlas`, `/etc/atlas`, and `/var/lib/atlas`. Keep `ATLAS_HOME=/opt/atlas` when
using the bundled systemd artifacts because they use `/opt/atlas/bin/atlas` as the stable launcher.

Each command or job runs in a separate child process with an exact argument vector and no shell.
Atlas preserves child streams, exit status, timeout behavior, signal forwarding, execution logs, and
parent/child run correlation. A timeout returns 124; a non-blocking job lock conflict returns 75.

Configure the release sources in `/etc/atlas/config.yml`:

```yaml
runtime:
  python:
    version: "3.14.6"

releases:
  operations:
    source: "/srv/atlas/source/operations"
    enabled: true
```

Give the host a name in `/etc/atlas/host.yml`:

```yaml
name: control-01
site: site-a
environment: production
```

`control-01`, `site-a`, and `/srv/atlas/source` are examples. Replace them with values for your
environment. A release source is separate from the installed copy: Atlas copies validated releases
to a content-addressed, never-replaced `/opt/atlas/releases/<release>/<version>-<content-digest>`
snapshot and atomically switches `/opt/atlas/current/<release>` to that snapshot. Atlas rechecks the
snapshot provenance before the child imports release code and forces `PYTHONDONTWRITEBYTECODE=1`.
Installation uses that child in validate-only mode to import each manifest target and inspect its
actual callable without invoking it; first-party module top-level code therefore runs during
installation and may have import-time side effects.
Snapshot modes are read-only for the normal runtime path, but a same-UID account can change them;
this is a selected-release correctness boundary, not a hostile same-UID sandbox. Do not use either
Atlas-managed directory as a release source.

Install the release. The install builds and publishes the runtime needed by the complete active
release set:

```bash
atlas release install /srv/atlas/source/operations
atlas status
```

Release installation and update do not require an existing shared runtime. Atlas selects the configured
`pyenv` Python, builds a clean candidate venv without system-site packages, copies the Atlas core
support package into it, installs Atlas's declared support requirements and the requirements of every
intended active release, and runs `pip check`. Candidate pip calls use an isolated environment,
explicit PyPI input, and no ambient pip configuration or `PIP_*` settings. It validates the exact staged
snapshots with that candidate before publishing the runtime generation and switching release links.
Host artifacts are published under the same transaction; a failed dependency install, callable
validation, or artifact publication restores the previous runtime, release links, mutable artifact
selection links, and stable launcher bytes. Only transaction-created artifact candidates are removed,
and only when their lease state is safe; pre-existing generations and lease files are never part of
rollback. The parent Atlas process only bootstraps the configured interpreter and never imports release
code. `atlas runtime install` can be used to rebuild the runtime for the currently active
snapshots; it validates every command and job with the candidate Python before switching the runtime
link and does not change release links.

Runtime and host-artifact generations are immutable after publication. The active links select one
concrete generation, and each release child captures both selections before it starts and owns the
leases until it exits. Parent lease descriptors are handed across exec and retained until the child
acknowledges its own leases. Prior generations are retained while a child holds a lease, so a lazy
import cannot lose its dependencies during a release replacement or a hard-killed waiting Atlas parent.
Nested private jobs inherit the parent release snapshot and generation selections. After the child
exits Atlas performs lease-aware best-effort garbage collection; a failed cleanup leaves that
generation for a later pass and does not change the active state.

The first-party release exposes three commands:

| Command | Purpose |
| --- | --- |
| `atlas-ansible` | Run Ansible checks, diffs, inventory reads, and applies |
| `hostctl` | Plan and run a managed-host lifecycle |
| `imagectl` | Plan and run a machine-image lifecycle |

```bash
atlas command list --verbose
atlas run atlas-ansible diff site web-01
atlas job list operations
```

See [Atlas reference](docs/reference.rst) for host configuration, release manifests, jobs,
systemd files, execution records, and recovery. See
[First-party controllers](docs/controllers.rst) for controller inputs and safety rules.

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development environment, required checks, and pull
request expectations.
