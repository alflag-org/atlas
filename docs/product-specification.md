# Atlas 製品仕様書

- 版: 0.1
- 位置づけ: 初期設計仕様（プロジェクト開始前）
- 最終更新日: 2026-05-04

---

## 1. 概要

Atlas は、複数のサーバー・仮想マシン・コンテナ・クラウド上ホストを、**分散型**で管理するためのインフラ運用基盤です。

Atlas は以下をホストに配布し、各ホストの自律運用を実現します。

- 共通運用スクリプト
- 設定ファイル/テンプレート
- ホスト情報
- 必要な秘密情報（参照にもとづく実体化）

Atlas は Ansible / Terraform / OpenTofu / Kubernetes / Docker を置き換えるものではなく、これらを必要に応じて呼び出す上位レイヤーです。

---

## 2. 目的

Atlas は次の状態を実現することを目的とします。

1. 各ホストが自分の役割を認識できる。
2. 各ホストが必要なファイル/スクリプトを取得できる。
3. 中央管理基盤が停止しても、最後に成功した状態で稼働継続できる。
4. 運用スクリプトを安全に追加・配布・実行できる。
5. 秘密情報を必要なホストのみに安全に配置できる。

---

## 3. 解決する課題

- ホスト役割の不明瞭化
- ホスト間の設定ドリフト
- スクリプト所在の属人化
- 秘密情報配置先の不透明化
- DNS/DBバックアップ手順の属人化
- 中央管理ホスト停止時の運用停止
- 変更失敗時の復旧困難

### 解決アプローチ

- ホスト情報の一元管理
- 共通リリースバンドル配布
- ホスト自律の pull / verify / apply
- 役割に応じた pack 有効化
- `atlas run` 経由の実行制御
- 最後に成功した状態の保持と rollback
- 秘密情報のバンドル外管理

---

## 4. 基本思想

1. **配布は広く、適用は絞る**  
   全ホストは同じリリースバンドルを取得し、各ホストは必要なパックのみ適用する。
2. **実体スクリプトを直接実行させない**  
   ユーザーは shim を実行し、内部で `atlas run <command>` を必ず通す。
3. **秘密情報をリリースバンドルに含めない**
4. **ホストは自律して動く**
5. **既存ツールを置き換えない**

---

## 5. 用語（抜粋）

- **リリースバンドル**: ホストへ配布するファイル一式
- **マニフェスト**: バンドル内容、バージョン、チェックサム、署名等の定義
- **パック**: 用途別のスクリプト/設定/コマンド定義
- **シム (shim)**: `atlas run` へ委譲する薄い入口
- **秘密情報の実体化**: 参照情報にもとづく秘密情報のローカル配置
- **最後に成功した状態**: 直近正常適用済みの状態
- **ロールバック**: 以前の正常状態に戻す操作

---

## 6. 構成要素

- ホスト一覧（inventory）
- ホスト側情報ファイル（`/etc/atlas/node.yml`）
- リリースバンドル
- パック
- コマンド自動発見
- シム自動生成
- 秘密情報実体化
- 状態管理
- ロールバック

---

## 7. データモデル（最小）

### 7.1 inventory host 例

```yaml
name: kng01-mgmt-dns-01
site: kng01
zone: mgmt
role: dns
provider: proxmox
runtime: virtual-machine
lifecycle: active
criticality: core

management:
  channel: stable
  auto_apply: true
  pull_interval: 1h

packs:
  - base
  - dns
  - monitoring
  - backup
```

### 7.2 node 情報 例

```yaml
name: kng01-mgmt-dns-01
site: kng01
zone: mgmt
role: dns
provider: proxmox
runtime: virtual-machine
lifecycle: active
packs:
  - base
  - dns
  - monitoring
```

### 7.3 state 情報 例

```yaml
node: kng01-mgmt-dns-01
current_version: 2026.05.04-001
previous_version: 2026.05.03-004
last_pull_at: "2026-05-04T12:00:00+09:00"
last_apply_at: "2026-05-04T12:00:12+09:00"
last_apply_status: success
```

---

## 8. リリースバンドル仕様

### 8.1 ファイル名例

`atlas-release-2026.05.04-001.tar.zst`

### 8.2 内容例

```text
manifest.yml
packs/
systemd/
schemas/
policies/
command-index.yml
```

### 8.3 含めるもの

- 共通スクリプト
- パック
- コマンド定義
- シム生成情報
- 設定テンプレート
- systemd unit/timer
- マニフェスト
- チェックサム/署名

### 8.4 含めないもの

- パスワード、秘密鍵、トークン、クラウド資格情報

---

## 9. コマンド実行モデル

### 9.1 実行方法

- 明示実行: `atlas run health`
- シム実行: `health`

### 9.2 実行前チェック

- コマンド存在
- 有効パック所属
- ホスト許可（role など）
- destructive 判定
- lock 競合
- 秘密情報実体化可否

### 9.3 実行中/実行後管理

- timeout
- lock
- stdout/stderr
- exit code
- 実行ログ保存

---

## 10. コマンド自動発見とシム

`packs/<pack>/bin/<command>` を追加すると、次回リリース作成時に自動発見されます。

- 生成物: `command-index.yml`
- 生成物: `/opt/atlas/shims/<command>`
- シム実体: `/opt/atlas/libexec/atlas-shim`

共通シムの概念:

```sh
#!/usr/bin/env sh
cmd="$(basename "$0")"
exec /usr/local/bin/atlas run "$cmd" "$@"
```

---

## 11. 秘密情報管理

### 11.1 原則

- 全ホストへ全秘密情報を配布しない
- リリースバンドルへ秘密情報を含めない
- 必要なホストだけが必要な秘密情報を取得
- 配置先/所有者/権限を明示
- 値そのものはログ出力しない

### 11.2 参照定義例

```yaml
secrets:
  - name: mysql-backup-credential
    source: secret-host
    ref: kng01-mgmt-secrets-01/mysql/backup/kng01-mgmt-db-01
    target: /etc/atlas/secrets/mysql-backup.env
    mode: "0600"
    owner: root
    group: root
```

### 11.3 想定取得元

- 秘密情報保管ホスト
- Bitwarden
- SOPS + age
- GitHub Actions secrets
- 将来の外部 secret manager

---

## 12. 状態管理とロールバック

状態ディレクトリ（例）:

```text
/var/lib/atlas/
  current/
  previous/
  releases/
  staging/
  state.yml
  logs/
  locks/
```

- `atlas rollback` で previous を復元
- 中央基盤に接続不能でも、ローカル保持状態から復旧可能

---

## 13. 既存ツールとの責任境界

### Atlas

- 配布、検証、展開
- pack 有効化判定
- 実行制御
- 秘密情報実体化
- 状態管理/ロールバック

### Ansible

- OS/ミドルウェア構成変更
- ユーザー、パッケージ、サービス、設定配置

### Terraform / OpenTofu

- クラウド/外部サービス資源管理（Cloudflare, AWS, GCP, Azure 等）

### Kubernetes / Docker

- コンテナ実行基盤（起動、更新、通信、再起動）

---

## 14. リポジトリ構成案

```text
atlas/
  inventory/
  packs/
  ansible/
  tofu/
  tools/
  systemd/
  docs/
```

---

## 15. ホスト側構成案

```text
/etc/atlas/
/opt/atlas/
/var/lib/atlas/
```

---

## 16. 主要 CLI

- `atlas status`
- `atlas pull`
- `atlas apply`
- `atlas pull --apply`
- `atlas rollback`
- `atlas run <command>`

---

## 17. 最小実用版（MVP）

### 17.1 初期実装対象

- inventory
- node 情報
- リリース取得/検証/適用
- base/dns/mysql pack
- コマンド発見
- shim 生成
- `atlas run`
- 状態管理
- rollback
- systemd timer

### 17.2 初期非対象

- Web UI
- 高度な台帳連携
- ホスト別バンドル
- 高度な secret 基盤
- 自動修復制御
- Kubernetes/Docker Swarm 深い統合
- クラウド全体自動構築

---

## 18. 非機能要件（補完）

> 本節は開始前の設計補完として追加。

- **可用性**: 中央障害時でもホスト単独運用継続
- **安全性**: 署名検証失敗時は apply を拒否
- **監査性**: 実行ログを時刻・ホスト・終了コード付きで保存
- **再現性**: 同一リリース番号は同一内容（イミュータブル）
- **性能目標（初期）**:
  - 小規模環境（~50ホスト）で pull/apply の運用が可能
  - pull 間隔は最小 1h を既定値とする

---

## 19. リスクと対策（補完）

1. **コマンド名衝突**
   - 対策: 予約語/OS衝突コマンドの静的チェック
2. **誤った role/packs 定義**
   - 対策: スキーマ検証 + CI lint
3. **秘密情報取得失敗**
   - 対策: 再試行、ロールバック、値非表示ログ
4. **壊れたリリース配布**
   - 対策: checksum + signature 必須
5. **破壊的コマンド誤実行**
   - 対策: destructive フラグ + 許可ポリシー

---

## 20. 初期マイルストーン（補完）

- M1: inventory/node schema 定義
- M2: release bundle 作成/検証
- M3: `atlas pull/apply/status`
- M4: pack discovery + shim 生成
- M5: `atlas run` 実行制御
- M6: rollback + systemd timer
- M7: secrets 実体化（1バックエンド）

---

## 21. 最終定義

Atlas は、ホスト情報にもとづいて共通リリースバンドルを分散配布し、各ホストが役割に応じてパックを適用し、許可された運用コマンドを安全に実行し、必要に応じて最後に成功した状態へロールバックできる分散型インフラ運用基盤である。
