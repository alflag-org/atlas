# Bundle Format（固定仕様）

`atlas build` が生成し、`atlas pull` / `atlas verify-bundle` が検証する仕様です。

## 外側 tar

| エントリ | 型 | 必須 | 説明 |
|---|---|---|---|
| `manifest.yml` | file (YAML) | 必須 | payload 名と checksum |
| `manifest.yml.minisig` | file | 必須 | manifest 署名 |
| `<payload>` | file (tar) | 必須 | 実体 payload |

## `manifest.yml`

| key | 型 | 必須 | 説明 |
|---|---|---|---|
| `payload` | string | 必須 | payload ファイル名 |
| `checksum` | string | 任意 | payload SHA-256 |

## payload（内側 tar）

| パス | 型 | 必須 | 説明 |
|---|---|---|---|
| `packs/` | dir | 実運用では必須 | command 自動発見元 |
| `command-index.yml` | file | 任意（apply 時に再生成） | command metadata |
| その他 | any | 任意 | pack files / systemd など |

## 失敗条件

| 操作 | 失敗条件 |
|---|---|
| pull/inspect/verify | `manifest.yml` 不在 |
| pull/inspect/verify | `manifest.yml.minisig` 不在 |
| pull/inspect/verify | `/etc/atlas/trust.d/atlas-release.pub` 不在 |
| pull/inspect/verify | `minisign` 不在・署名不正 |
| pull/verify | `checksum` 不一致 |
| pull | tar パストラバーサル・不正 uid/gid/mode |
