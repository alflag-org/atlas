# atlas

Atlas is a lightweight scripts runtime manager focused on Python runtime and Python Fire scripts.

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
