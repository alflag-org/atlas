設計と概念
============

.. note::

   このページは現行の Atlas 0.3 scripts runtime を説明します。後続 PR で実装する
   Atlas 1.0 の構成は :doc:`architecture`、導入順序は :doc:`migration` を参照してください。

Atlas の役割
------------

Atlas は、ホストに配置されたスクリプトリリースを実行可能なコマンドとして公開します。
アプリケーションサーバーやジョブスケジューラではなく、次の責務に範囲を絞っています。

* スクリプト実行用 Python ランタイムの作成
* スクリプトリリースの検証、インストール、更新
* リリース内の Python コマンド検出
* ``/opt/atlas/shims`` への shim 生成
* 実行時環境変数と ``atlas_core`` コンテキストの提供
* 実行結果の JSONL ログ記録

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

スクリプトリリースは ``VERSION`` と ``commands/`` を持つディレクトリです。
``commands/`` 配下の ``.py`` ファイルは、相対パスからコマンド名へ変換されます。
たとえば ``commands/admin/restart.py`` は ``admin-restart`` になります。

コマンド名は小文字英数字と ``-`` を使う形に制限されます。
``atlas`` と ``script-runner`` は予約名です。
リリース名は小文字英数字、``_``、``-`` を使えますが、``current``、``releases``、``tmp`` などの管理名は使えません。

失敗時の基本方針
----------------

Atlas は曖昧な状態を許容せず、失敗を明示します。

* コマンド名が複数リリースで衝突した場合は失敗
* リリース内に symlink が含まれる場合は失敗
* archive 展開時に path traversal や symlink がある場合は失敗
* runtime Python が見つからない場合は失敗
* ``host.yml`` や ``config.yml`` の型が不正な場合は失敗

この方針により、更新途中の不完全な状態や意図しないコマンド上書きを避けます。
