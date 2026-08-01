# Atlas

Atlas runs versioned infrastructure operations on a host. Each release declares its commands,
jobs, and systemd files in `release.yml`. Atlas installs the release, builds a shared Python
runtime, creates shims for its commands, and records each execution.

Infrastructure repositories remain separate. Atlas uses the caller's working directory and
records its Git state, but does not change repository state, desired state, inventory, playbooks,
provider configuration, or secrets.

## Configure the host

`/etc/atlas/config.yml` selects the Python runtime and release sources:

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

`/etc/atlas/host.yml` provides the host identity exposed to release code:

```yaml
name: control-01
site: kng01
environment: production
```

See [Configuration](docs/configuration.rst) for supported fields and release source formats.

## Install and run a release

```bash
atlas release install ./configuration-operations
atlas release install ./infrastructure-operations
atlas runtime install
atlas release shims
atlas status

atlas command list --verbose
atlas run configctl diff site web01

atlas job list configuration-operations
atlas job run configuration-operations inventory-refresh -- --site default
```

Only commands declared by the manifest receive shims. Jobs are invoked through `atlas job`.
Release installation and multi-release updates restore the previous active releases and shims when
validation, activation, or shim generation fails.

The repository includes two first-party releases. `configuration-operations` exposes `configctl`
and owns the Ansible jobs. `infrastructure-operations` exposes `hostctl`, `imagectl`, `providerctl`,
and `operationctl`; provider and lifecycle phases remain private Atlas jobs. See
[Operation controllers](docs/controllers.rst) for the process and security boundaries.

## Documentation

- [Runtime behavior](docs/runtime.rst)
- [CLI usage](docs/usage.rst)
- [Configuration](docs/configuration.rst)
- [Release authoring](docs/releases.rst)
- [Jobs and job instances](docs/jobs.rst)
- [First-party command surface](docs/command-surface.rst)
- [Operation controllers](docs/controllers.rst)
- [Host operations](docs/operations.rst)
- [Proxmox operations](docs/proxmox.rst)
- [Managed host lifecycle](docs/hostctl.rst)
- [Python API](docs/api.rst)

## Development

Atlas supports Python 3.11 through 3.14. The checked-in development environment uses Python
3.14.6.

```bash
mise install
mise run setup
mise run check
make html SPHINXOPTS=-W
```

`mise run check` runs Ruff, the test suite with 100% line and branch coverage, and the package
build.
The Docker targets exercise the installed runtime and example release:

```bash
docker compose build
docker compose run --rm check
docker compose run --rm atlas atlas release list
```
