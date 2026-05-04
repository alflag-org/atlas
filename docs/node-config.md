# Node Config (`node.yml`)

配置先はデフォルトで `/etc/atlas/node.yml` です。`ATLAS_ETC_DIR` で変更できます。

## キー

| key | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `name` | string | 任意 | `unknown` | ノード名 |
| `role` | string | 任意 | `""` | ロール名 |
| `packs` | list | 任意 | `[]` | 有効化する pack 一覧 |

## 最小例

```yaml
name: app-01
role: app
packs:
  - base
```
