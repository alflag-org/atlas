# atlas

Atlas is a lightweight scripts runtime manager focused on Python runtime and Python Fire scripts.

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
During the image build it installs the Python scripts runtime and installs `examples/scripts-release` as the current scripts release.

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

`runtime.python.version` in `/etc/atlas/config.yml` is currently an expected version string.
`atlas runtime install` creates the scripts venv with the current interpreter, then prints a warning when the configured value does not match.
Atlas does not yet install or select a Python interpreter version by itself.

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
