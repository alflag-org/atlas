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

## 移行ガイド（v0.2系 → 次期版）
- 設定ファイル読み込みは移行期間中、`*.yml` を優先し、未存在時のみ `*.json` をフォールバックします。
  - 例: `/etc/atlas/node.yml` が無ければ `/etc/atlas/node.json` を読み込みます。
- state/log/lock のデフォルト配置先は `/var/lib/atlas` になりました（旧: `/opt/atlas`）。

### 自動移行コマンド
1. 事前確認（dry-run）

   ```bash
   atlas migrate-layout --dry-run
   ```

2. 実行

   ```bash
   atlas migrate-layout --execute
   ```

`migrate-layout` は旧パスの以下を対象に、移行先とアクション（move/skip）を表示します。
- `/opt/atlas/state` → `/var/lib/atlas/state`
- `/opt/atlas/logs` → `/var/lib/atlas/logs`
- `/opt/atlas/locks` → `/var/lib/atlas/locks`

移行先がすでに存在する場合は安全のため `skip` されます。
