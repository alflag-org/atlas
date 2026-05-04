# Packs

## ディレクトリ規約

- 実行コマンド: `packs/<pack>/bin/<command>`
- 配置ファイル: `packs/<pack>/files/...`（`atlas apply` 時に `/` 配下へコピー）

## pack 適用

- node の `packs` に含まれる pack だけが `atlas run` 実行対象になります。
- `files` は `apply` 時に有効 pack のみ展開されます。
