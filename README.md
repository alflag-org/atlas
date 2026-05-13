# atlas

Atlas is a lightweight scripts runtime manager focused on Python runtime and Python Fire scripts.
It provides runtime installation, scripts release installation, command discovery, host context,
shim generation, and JSONL execution logging without adding domain-specific orchestration.

## Development Environment (mise)

```bash
mise install
mise run setup
mise run check
```

Task overview:

- `mise run setup`: install dev dependencies (`pip install -e '.[dev]'`) and build tool.
- `mise run lint`: run `ruff check src tests`.
- `mise run test`: run `pytest -q`.
- `mise run build`: run `python -m build`.
- `mise run check`: run lint + test + build.

## Local Environment With Docker

```bash
docker compose build atlas
docker compose run --rm atlas
```

The Docker image provisions an Atlas environment with `/etc/atlas`, `/opt/atlas`, and `/var/lib/atlas`.
During the image build it installs `pyenv`, installs the configured Python scripts runtime,
and installs `examples/scripts-release` as the current scripts release.

Useful commands:

```bash
docker compose run --rm atlas atlas status
docker compose run --rm atlas atlas runtime status
docker compose run --rm atlas atlas scripts list
docker compose run --rm atlas atlas run sample hello --name=docker
docker compose run --rm check
```

`docker compose run --rm check` validates the containerized Atlas environment and then runs the same checks as `mise run check`: Ruff, pytest, and package build.
Use `docker compose run --build --rm atlas` or `docker compose run --build --rm check` after source changes.

## Main commands

- `atlas status`
- `atlas runtime status`
- `atlas runtime install`
- `atlas scripts install <source>`
- `atlas scripts update`
- `atlas scripts list`
- `atlas scripts shims`
- `atlas run <command-name> [args...]`
- `atlas which <command-name>`

## Runtime Version Semantics

`runtime.python.version` in `/etc/atlas/config.yml` is the Python version Atlas uses for the scripts runtime.
By default, `atlas runtime install` uses `pyenv install -s <version>` to ensure the interpreter exists,
then creates the scripts virtual environment under `/opt/atlas/runtime/python/envs/scripts`.

Atlas does not install `pyenv` or OS build dependencies by itself. Install them before running `atlas runtime install`.
This keeps Python version management (`pyenv`) separate from package isolation (`venv`).

```yaml
runtime:
  python:
    version: "3.12.3"
```

## Production Operation

Install Atlas under a dedicated operational prefix and keep these directories writable by the account that runs Atlas:

- `/etc/atlas` for `config.yml` and `host.yml`
- `/opt/atlas` for runtime, shims, launchers, and installed scripts releases
- `/var/lib/atlas` for logs, cache, and runtime state

For a pyenv-based host, make `pyenv` available on `PATH` for the Atlas service or shell before running:

```bash
atlas runtime status
atlas runtime install
atlas scripts install <source>
atlas scripts shims
```

Add `/opt/atlas/shims` to `PATH` for users or services that should invoke release commands directly.
Execution logs are appended to `/var/lib/atlas/logs/runs.jsonl`; rotate or collect that file with the host's normal log tooling.
If `atlas runtime install` fails, check `atlas runtime status` first: it reports whether `pyenv` is visible to Atlas.

## Scripts Sources

`atlas scripts install <source>` accepts:

- local release directory
- local `.tar`, `.tar.gz`, `.tgz`, or `.zip` archive
- HTTP(S) archive URL
- git repository source as `git+<repo-url>#<ref>`; `#<ref>` is optional
- registry alias defined in `/etc/atlas/config.yml`

Registry aliases are local configuration, not built-in Atlas domain logic:

```yaml
scripts:
  source: sample-release
  registries:
    sample-release:
      source: "git+https://github.com/example/scripts-release.git#v1.0.0"
```

`atlas scripts update` resolves `scripts.source` again and reinstalls it atomically, including when the release `VERSION` is unchanged.

## Example

```bash
atlas scripts install examples/scripts-release
atlas scripts list
atlas which sample
atlas run sample hello --name=takuya
atlas run group-nested-sample show-context
```

## Host Profile

`/etc/atlas/host.yml` is required for script execution.

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

mkdir -p "$ATLAS_ETC_DIR"
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
atlas run sample hello --name=test
```
