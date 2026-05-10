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
