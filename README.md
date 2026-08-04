# Atlas

Atlas installs and runs versioned infrastructure operations on a host. A release declares its
commands, jobs, and systemd files in `release.yml`. Atlas validates the release, builds a shared
Python runtime, creates command shims, and records every execution.

Infrastructure repositories stay separate. Atlas runs an artifact in the caller's working
directory and records its Git state, but it does not change desired state, inventory, playbooks,
provider configuration, repository state, or secrets.

## Try Atlas

The repository includes a containerized example. It installs the sample and first-party release,
builds their shared runtime, and prints the available commands without touching host configuration:

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

`atlas runtime install` requires `pyenv` on `PATH` and the operating-system packages needed to
build the configured Python version. The account running Atlas must be able to write its configured
home, configuration, and state directories. The defaults are `/opt/atlas`, `/etc/atlas`, and
`/var/lib/atlas`. Keep `ATLAS_HOME=/opt/atlas` when using the bundled systemd artifacts because
they use `/opt/atlas/bin/atlas` as the stable launcher.

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
to `/opt/atlas/releases/<release>/<version>` and switches
`/opt/atlas/current/<release>` to the active version. Do not use either Atlas-managed directory as
a release source.

Install the releases and shared runtime:

```bash
atlas release install /srv/atlas/source/operations
atlas runtime install
atlas status
```

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
