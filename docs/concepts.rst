設計と概念
============

.. note::

   このページは command、job、service manifest と systemd init artifact を実装した段階の
   scripts runtime を説明します。後続 PR で変更する Atlas 1.0 の filesystem terminology は
   :doc:`architecture`、導入順序は :doc:`migration` を参照してください。

Atlas の役割
------------

Atlas は、ホストに配置されたスクリプトリリースを実行可能なコマンドとして公開します。
アプリケーションサーバーやジョブスケジューラではなく、次の責務に範囲を絞っています。

* スクリプト実行用 Python ランタイムの作成
* スクリプトリリースの検証、インストール、更新
* manifest に宣言された Python command と job の検証
* manifest に宣言された service と systemd init artifact の検証、diff、配置、削除
* ``/opt/atlas/shims`` への shim 生成
* job instance の timeout、lock、working directory、environment file の解決
* 実行時環境変数と ``atlas_core`` コンテキストの提供
* command と job の相関付き JSONL ログ記録

Atlas は pyenv や OS パッケージのインストールまでは行いません。
ホストの Python バージョン管理は pyenv に任せ、Atlas はその Python を使ってスクリプト用 venv を作ります。

主要ディレクトリ
----------------

既定の配置は以下です。環境変数で一部を上書きできます。

.. list-table::
   :header-rows: 1

   * - パス
     - 用途
   * - ``/etc/atlas``
     - ``config.yml`` と ``host.yml``
   * - ``/opt/atlas``
     - ランタイム、shim、launcher、インストール済みリリース
   * - ``/var/lib/atlas``
     - 実行ログ、キャッシュ、ランタイム状態
   * - ``/opt/atlas/scripts/releases``
     - リリース本体の保存先
   * - ``/opt/atlas/scripts/current``
     - アクティブリリースへの symlink 群
   * - ``/opt/atlas/shims``
     - ユーザーやサービスが ``PATH`` に追加するコマンド shim

リリースとコマンド
------------------

スクリプトリリースは ``VERSION`` と ``release.yml`` を持つディレクトリです。
``release.yml`` の ``commands`` に command 名、``python`` runtime、release root からの entrypoint を明示します。
``commands/`` に置いただけの Python file は公開されません。

release 名と command 名は ``[a-z][a-z0-9]*(?:-[a-z0-9]+)*`` に制限されます。
``atlas``、``script-runner``、``artifact-runner`` は command の予約名です。

失敗時の基本方針
----------------

Atlas は曖昧な状態を許容せず、失敗を明示します。

* コマンド名が複数リリースで衝突した場合は失敗
* ``release.yml`` に unknown key、重複 YAML key、未対応 schema/runtime がある場合は失敗
* リリース内に symlink が含まれる場合は失敗
* service が存在しない command/job を参照する場合や systemd unit が安定した Atlas launcher を使わない場合は失敗
* archive 展開時に path traversal や symlink がある場合は失敗
* runtime Python が見つからない場合は失敗
* ``host.yml`` や ``config.yml`` の型が不正な場合は失敗

この方針により、更新途中の不完全な状態や意図しないコマンド上書きを避けます。
