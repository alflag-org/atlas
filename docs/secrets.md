# Secrets

`atlas run --materialize-secrets` で `secrets.yml` を読み、環境変数値をファイルへ書き出します。

## `secrets.yml`

| key | 型 | 必須 | 説明 |
|---|---|---|---|
| `secrets` | list | 任意 | secret 定義配列 |

secret 要素:

| key | 型 | 必須 | 説明 |
|---|---|---|---|
| `target` | string | 必須 | 出力先（`$ATLAS_ETC_DIR/secrets` 配下のみ） |
| `env` | string | 必須 | 参照する環境変数名 |
| `mode` | string(octal) | 任意 | パーミッション（既定 `0600`） |

## 制約

- `target` が許可ルート外、またはシンボリックリンクなら失敗。
- `env` 未指定、または環境変数が未設定なら失敗。
