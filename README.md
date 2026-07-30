# atlas

Atlas is a lightweight runtime manager for Python-based script releases, especially Python Fire commands.
It installs the runtime, installs script releases, discovers commands, loads host context,
generates shims, and records execution logs in JSONL without adding extra orchestration.

## Atlas 1.0 design status

The current checkout still implements the Atlas 0.3 scripts runtime described below. The accepted
Atlas 1.0 target separates manifest-declared commands, jobs, services, and init artifacts while
keeping environment-specific desired state in independent repositories. This documentation-only
change does not alter runtime behavior.

- [Target architecture](docs/architecture.rst)
- [Architecture decision](docs/adr/0001-release-artifacts-and-repository-boundaries.rst)
- [Staged migration and rollback](docs/migration.rst)

## Development Environment (mise)

```bash
mise install
mise run setup
mise run check
```

Available tasks:

- `mise run setup`: install development dependencies (`pip install -e '.[dev]'`) and build tooling.
- `mise run lint`: run `ruff check src tests`.
- `mise run test`: run `python -m coverage run -m pytest -q` and enforce 100% line and branch coverage with `python -m coverage report`.
- `mise run build`: run `python -m build`.
- `mise run docs`: build Sphinx HTML documentation under `build/html`.
- `mise run check`: run lint + test + build.

## Documentation Hosting

Sphinx documentation is hosted on Cloudflare Workers static assets, not Cloudflare Pages. Build the site with `make html`, which writes HTML to `build/html`, and deploy it with `npx wrangler deploy`. The Worker configuration is tracked in `wrangler.jsonc`, and `assets.directory` is the source of truth for the deployed output directory.

## Local Environment With Docker

```bash
docker compose build atlas check
docker compose run --rm atlas
```

The Dockerfile defines separate targets for the two main jobs:

- `runtime`: non-root Atlas runtime image with `/etc/atlas`, `/opt/atlas`, `/var/lib/atlas`, pyenv metadata, the scripts runtime, and `examples/basic-scripts-release`.
- `dev`: development image for checks and builds, with Ruff, pytest, and package build tooling.

Useful commands:

```bash
docker compose run --rm atlas atlas status
docker compose run --rm atlas atlas runtime status
docker compose run --rm atlas atlas scripts list
docker compose run --rm atlas atlas run sample hello --name=docker
docker compose run --rm check
```

`docker compose run --rm check` validates the containerized Atlas environment, runs a sample script through the scripts runtime, and then runs the same checks as `mise run check`: Ruff, pytest, and package build.
Use `docker compose run --build --rm atlas` or `docker compose run --build --rm check` after source changes.

## Internal Shape

Atlas keeps the command path small:

- `atlas.commands` discovers release commands and validates their names.
- `atlas.sources` resolves local, archive, HTTP(S), git, and registry sources.
- `atlas.releases` validates and atomically installs scripts releases.
- `atlas.runtime` handles pyenv-backed scripts runtime installation and status.
- `atlas.runner` executes one command and appends one JSONL run record.
- `atlas.launchers` manages generated launchers and shims.

## Main commands

- `atlas status`
- `atlas runtime status`
- `atlas runtime install`
- `atlas scripts install <source> [--name <release-name>]`
- `atlas scripts update [release-name]`
- `atlas scripts list [--verbose]`
- `atlas scripts shims`
- `atlas run <command-name> [args...]`
- `atlas which <command-name>`

## Runtime Version Semantics

`runtime.python.version` in `/etc/atlas/config.yml` sets the Python version used for the scripts runtime.
By default, `atlas runtime install` runs `pyenv install -s <version>` to ensure the interpreter is available,
then creates the scripts virtual environment under `/opt/atlas/runtime/python/envs/scripts`.

Atlas does not install `pyenv` or OS build dependencies on its own. Install them before running `atlas runtime install`.
This keeps Python version management (`pyenv`) separate from package isolation (`venv`).

```yaml
runtime:
  python:
    version: "3.12.3"
```

## Production Operation

Install Atlas under a dedicated prefix and keep these directories writable by the account that runs it:

- `/etc/atlas` for `config.yml` and `host.yml`
- `/opt/atlas` for runtime, shims, launchers, and installed scripts releases
- `/var/lib/atlas` for logs, cache, and runtime state

On a pyenv-based host, make sure `pyenv` is on `PATH` for the Atlas service or shell before running:

```bash
atlas runtime status
atlas runtime install
atlas scripts update common
atlas scripts shims
```

Add `/opt/atlas/shims` to `PATH` for users or services that need to invoke release commands directly.
Execution logs are appended to `/var/lib/atlas/logs/runs.jsonl`; rotate or collect that file with the host's usual log tooling.
If `atlas runtime install` fails, check `atlas runtime status` first. It reports whether `pyenv` is visible to Atlas.
Scripts releases are installed under `/opt/atlas/scripts/releases/<release-name>/<version>` and activated through
`/opt/atlas/scripts/current/<release-name>` symlinks. Atlas does not add an implicit namespace or precedence across releases:
command collisions fail closed in `scripts list`, `scripts shims`, `run`, and `which`.

## Scripts Sources

`atlas scripts install <source>` accepts:

- local release directory
- local `.tar`, `.tar.gz`, `.tgz`, or `.zip` archive
- HTTP(S) archive URL
- git repository source as `git+<repo-url>#<ref>`; `#<ref>` is optional
- registry alias defined in `/etc/atlas/config.yml`

Registry aliases come from local configuration rather than built-in Atlas logic:

```yaml
scripts:
  releases:
    common:
      source: common
  registries:
    common:
      source: "git+https://github.com/example/basic-scripts-release.git#v1.0.0"
```

Regular multi-release configuration:

```yaml
scripts:
  releases:
    common:
      source: common
    kitsunebi:
      source: kitsunebi

  registries:
    common:
      source: "git+https://github.com/example/common-scripts.git#v0.1.0"
    kitsunebi:
      source: "git+https://github.com/example/kitsunebi-scripts.git#v0.1.0"
```

Legacy single-release configuration is still supported for compatibility:

```yaml
scripts:
  source: sample-release
  auto_update: false
  registries:
    sample-release:
      source: "git+https://github.com/example/basic-scripts-release.git#v1.0.0"
```

Internally, Atlas treats legacy `scripts.source` as `scripts.releases.default`.

Full config with runtime + multi-release:

```yaml
runtime:
  python:
    version: "3.12.3"

scripts:
  releases:
    common:
      source: common
    kitsunebi:
      source: kitsunebi

  registries:
    common:
      source: "git+https://github.com/example/common-scripts.git#v0.1.0"
    kitsunebi:
      source: "git+https://github.com/example/kitsunebi-scripts.git#v0.1.0"
```

`atlas scripts update` resolves each enabled configured release and reinstalls it atomically per update operation.
`atlas scripts update <release-name>` updates only that configured release.
If command names collide across active releases, Atlas fails closed in `scripts list`, `scripts shims`, `run`, and `which`.

## Example

The repository includes two local example releases:

- `examples/basic-scripts-release`: a standalone release with a top-level command, a nested command, and a release-local module. Use it for single-release smoke tests and command-discovery examples.
- `examples/companion-scripts-release`: a second independent release with a small command surface. Use it alongside `basic-scripts-release` when checking multi-release configuration, updates, status output, and command routing.

```bash
atlas scripts install examples/basic-scripts-release --name sample
atlas scripts install examples/companion-scripts-release --name sample2
atlas runtime install
atlas scripts list --verbose
atlas which sample
atlas run sample hello --name=hoge
atlas run sample2 show-release
```

## atlas_core Public API

`atlas_core` is the stable runtime library for installed scripts. Scripts should import from `atlas_core`, not from Atlas internals under `atlas`.

```python
from atlas_core import get_context

ctx = get_context()
```

`get_context()` is the preferred entry point. It returns host metadata, Atlas paths, and the currently running script release:

- `ctx.host`: `HostProfile` loaded from `host.yml`
- `ctx.paths`: `AtlasPaths` derived from Atlas environment variables
- `ctx.script`: `ScriptInfo` for the active script command and release

`host.yml` must be a YAML mapping with a non-empty string `name`. Optional `site`, `zone`, `role`, `environment`, and `runtime_kind` values must be strings when present. `tags` may be absent or null, or a list of strings.

```yaml
name: kng01-mgmt-dns-01
site: kng01
zone: mgmt
role: dns
environment: home
runtime_kind: lxc
tags:
  - sample
  - local
```

`get_context()` consumes these environment variables during script execution:

- `ATLAS_SCRIPT_NAME` is required.
- `ATLAS_SCRIPT_RELEASE_NAME` is required.
- `ATLAS_SCRIPTS_DIR` is required and identifies the active script release root.
- `ATLAS_SCRIPT_VERSION` is optional and defaults to an empty string.
- `ATLAS_HOST_FILE` selects `host.yml` and defaults to `/etc/atlas/host.yml`.
- `ATLAS_HOME`, `ATLAS_ETC_DIR`, `ATLAS_VAR_DIR`, `ATLAS_RUNTIME_DIR`, and `ATLAS_SCRIPTS_CURRENT_DIR` define public runtime paths.

`atlas_core` intentionally does not expose install, update, source resolution, command discovery, runtime management, subprocess, Ansible, inventory, IPAM, network, or logging framework APIs.

## Host Profile

`/etc/atlas/host.yml` is required to run scripts.

```yaml
name: kng01-mgmt-dns-01
site: kng01
zone: mgmt
role: dns
environment: home
runtime_kind: lxc
tags:
  - sample
  - local
```

## Local Test With `ATLAS_HOME`

```bash
export ATLAS_HOME="$PWD/.tmp/opt/atlas"
export ATLAS_ETC_DIR="$PWD/.tmp/etc/atlas"
export ATLAS_VAR_DIR="$PWD/.tmp/var/lib/atlas"
export ATLAS_RUNTIME_DIR="$ATLAS_HOME/runtime"
export ATLAS_SCRIPTS_DIR="$ATLAS_HOME/scripts/current"
export ATLAS_SCRIPTS_CURRENT_DIR="$ATLAS_HOME/scripts/current"

mkdir -p "$ATLAS_ETC_DIR"
cat > "$ATLAS_ETC_DIR/config.yml" <<'YAML'
runtime:
  python:
    version: "3.12.3"
scripts:
  releases:
    sample:
      source: sample
    sample2:
      source: sample2
  registries:
    sample:
      source: "file://examples/basic-scripts-release"
    sample2:
      source: "file://examples/companion-scripts-release"
YAML

cat > "$ATLAS_ETC_DIR/host.yml" <<'YAML'
name: local-host
site: dev
zone: local
role: test
environment: dev
runtime_kind: vm
tags:
  - local
YAML

atlas scripts install examples/basic-scripts-release --name sample
atlas scripts install examples/companion-scripts-release --name sample2
atlas scripts update
atlas scripts list --verbose
python -m venv "$ATLAS_RUNTIME_DIR/python/envs/scripts"
"$ATLAS_RUNTIME_DIR/python/envs/scripts/bin/python" -m pip install --upgrade pip
"$ATLAS_RUNTIME_DIR/python/envs/scripts/bin/python" -m pip install fire PyYAML
atlas run sample hello --name=test
atlas run sample2 show-release
```

Use `atlas runtime install` instead of the manual `python -m venv` steps when pyenv is installed and should provide the configured Python version for scripts.
