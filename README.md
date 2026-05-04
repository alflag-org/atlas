# atlas

Atlas product-spec の最小実装（継続拡張中）。

## 実装済み
- `atlas pull`: bundle 展開、manifest checksum 検証、payload 展開
- `atlas apply`: active 切替、runtime state 更新、shim 自動生成
- `atlas run`: command-index メタに基づく pack/role/destructive チェック、lock 付き実行、ログ保存
- `atlas rollback` / `atlas status`
- `/etc/atlas/node.yml` 読み込み

## 残件（次段）
- secrets 実体化フレームワーク
- command-index / manifest の YAML サポート
- 署名検証
- systemd timer 連携
