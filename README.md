# atlas

Atlas の現行実装ドキュメントです（**実装済み機能のみ記載**）。

## 実装済み機能

- `atlas build`: `packs/` を走査して `command-index.yml` を生成し bundle を作成
- `atlas inspect-bundle` / `atlas verify-bundle`: 署名・checksum を検証
- `atlas update`: bundle 展開と checksum 検証
- `atlas apply`: validate → activate → pack files 展開 → shim 生成
- `atlas run`: command metadata に基づく `pack/role/destructive/timeout/lock` 制御
- `atlas rollback` / `atlas status`
- `atlas install-systemd` / `atlas uninstall-systemd`
- `atlas run --materialize-secrets` による secrets 実体化

## 導入手順

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 最短利用

`docs/getting-started.md` の手順をそのまま実行してください。

## bundle 作成

```bash
atlas build <release_dir> <bundle_path>
```

詳細: `docs/bundle-format.md`, `docs/release.md`

## コマンド追加

1. `packs/<pack>/bin/<command>` を追加して実行権限を付与
2. node の `packs` に `<pack>` を追加
3. `atlas build` → `atlas update` → `atlas apply`

詳細: `docs/command-metadata.md`, `docs/packs.md`

## systemd 有効化

```bash
sudo atlas install-systemd
```

詳細: `docs/systemd.md`

## セキュリティ方針

実装済みの範囲は `docs/security.md` を参照してください。


## v0.1 の適用範囲
- `atlas apply` は `packs/<pack>/files` の配置と shim 生成のみを行います。
- `templates` / `hooks` / systemd unit lifecycle は未対応です。
