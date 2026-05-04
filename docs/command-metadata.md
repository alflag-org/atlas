# Command Metadata（固定仕様）

`command-index.yml` の 1 コマンド定義仕様です（`commands.<name>`）。

## フィールド仕様

| key | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `path` | string | 必須 | - | 実行ファイルの相対パス |
| `pack` | string | 任意 | `""` | この command が属する pack |
| `allowed_roles` | list[string] | 任意 | `[]` | 実行許可 role 一覧 |
| `roles` | list[string] | 任意 | - | 互換キー（読込時に `allowed_roles` へ変換） |
| `destructive` | bool | 任意 | `false` | 破壊的操作フラグ |
| `timeout_sec` | int | 任意 | リクエスト値 | 上限 timeout |
| `timeout` | int | 任意 | - | 互換キー（読込時に `timeout_sec` へ変換） |
| `lock` | string | 任意 | command 名 | ロック名 |

## 失敗条件

| タイミング | 失敗条件 |
|---|---|
| `atlas run` 前 | command 未定義 |
| `atlas run` 前 | `pack` が node packs に含まれない |
| `atlas run` 前 | `allowed_roles` に node role がない |
| `atlas run` 前 | `destructive=true` かつ `--allow-destructive` 未指定 |
| `atlas run` 前 | `path` の実体が存在しない |
| `atlas apply` validate | `path` が staging 外を指す |
| `atlas apply` validate | 実行権限がない |
