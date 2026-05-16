# atlas

Atlas is a lightweight runtime manager for Python-based script releases, especially Python Fire commands.
It installs the runtime, installs script releases, discovers commands, loads host context,
generates shims, and records execution logs in JSONL without adding extra orchestration.

## Development Environment (mise)

```bash
mise install
mise run setup
mise run check
```

Available tasks:

- `mise run setup`: install development dependencies (`pip install -e '.[dev]'`) and build tooling.
- `mise run lint`: run `ruff check src tests`.
- `mise run test`: run `pytest -q`.
- `mise run build`: run `python -m build`.
- `mise run check`: run lint + test + build.

## Local Environment With Docker

```bash
docker compose build atlas check
docker compose run --rm atlas
```

The Dockerfile defines separate targets for the two main jobs:

- `runtime`: non-root Atlas runtime image with `/etc/atlas`, `/opt/atlas`, `/var/lib/atlas`, pyenv metadata, the scripts runtime, and `examples/scripts-release`.
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
atlas scripts install <source> --name default
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
  source: sample-release
  registries:
    sample-release:
      source: "git+https://github.com/example/scripts-release.git#v1.0.0"
```

Legacy single-release configuration stays supported:

```yaml
scripts:
  source: sample-release
  auto_update: false
  registries:
    sample-release:
      source: "git+https://github.com/example/scripts-release.git#v1.0.0"
```

Internally, Atlas treats that as `scripts.releases.default`.

The regular multi-release form is:

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

`atlas scripts update` resolves each enabled configured release again and reinstalls it atomically, even if the release `VERSION`
has not changed. `atlas scripts update <release-name>` updates only that configured release.

## Example

```bash
atlas scripts install examples/scripts-release --name sample
atlas scripts install examples/scripts-release-2 --name sample2
atlas runtime install
atlas scripts list --verbose
atlas which sample
atlas run sample hello --name=takuya
atlas run sample2 show-release
```

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
      source: "file://examples/scripts-release"
    sample2:
      source: "file://examples/scripts-release-2"
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

atlas scripts install examples/scripts-release
atlas scripts update
atlas scripts list --verbose
python -m venv "$ATLAS_RUNTIME_DIR/python/envs/scripts"
"$ATLAS_RUNTIME_DIR/python/envs/scripts/bin/python" -m pip install --upgrade pip
"$ATLAS_RUNTIME_DIR/python/envs/scripts/bin/python" -m pip install fire PyYAML
atlas run sample hello --name=test
atlas run sample2 show-release
```

Use `atlas runtime install` instead of the manual `python -m venv` steps when pyenv is installed and should provide the configured Python version for scripts.
