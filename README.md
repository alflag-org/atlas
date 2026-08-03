# Atlas

Atlas installs and runs versioned infrastructure operations on a host. A release declares its
commands, jobs, and systemd files in `release.yml`. Atlas validates the release, builds a shared
Python runtime, creates command shims, and records every execution.

Infrastructure repositories stay separate. Atlas runs an artifact in the caller's working
directory and records its Git state, but it does not change desired state, inventory, playbooks,
provider configuration, repository state, or secrets.

## Run Atlas

Configure the Python runtime and release sources in `/etc/atlas/config.yml`:

```yaml
runtime:
  python:
    version: "3.14.6"

releases:
  configuration-operations:
    source: "/srv/releases/configuration-operations"
    enabled: true
  infrastructure-operations:
    source: "/srv/releases/infrastructure-operations"
    enabled: true
```

Give the host a name in `/etc/atlas/host.yml`:

```yaml
name: control-01
site: kng01
environment: production
```

Install the releases and shared runtime:

```bash
atlas release install ./configuration-operations
atlas release install ./infrastructure-operations
atlas runtime install
atlas status
```

The first-party releases expose three commands:

| Command | Purpose |
| --- | --- |
| `atlas-ansible` | Run Ansible checks, diffs, inventory reads, and applies |
| `hostctl` | Plan and run a managed-host lifecycle |
| `imagectl` | Plan and run a machine-image lifecycle |

```bash
atlas command list --verbose
atlas run atlas-ansible diff site web01
atlas job list infrastructure-operations
```

See [Atlas reference](docs/reference.rst) for host configuration, release manifests, jobs,
systemd files, execution records, and recovery. See
[First-party controllers](docs/controllers.rst) for controller inputs and safety rules.

## Develop Atlas

Atlas supports Python 3.11 through 3.14. The checked-in environment uses Python 3.14.6.

```bash
mise install
mise run setup
mise run check
make clean-docs html SPHINXOPTS=-W
```

`mise run check` runs Ruff, the test suite with 100% line and branch coverage, and the package
build. The container checks exercise the installed package and bundled releases:

```bash
docker compose build
docker compose run --rm check
docker compose run --rm atlas atlas release list
```
