設定
====

設定ファイル
------------

Atlas は既定で ``/etc/atlas/config.yml`` を読みます。
このファイルには runtime と scripts の設定が必要です。

最小構成は以下です。

.. code-block:: yaml

   runtime:
     python:
       version: "3.12.3"

   scripts:
     source: sample-release

``scripts.source`` は互換性のために残っている単一リリース形式です。
内部的には ``scripts.releases.default`` として扱われます。
新規設定では複数リリース形式を推奨します。

複数リリース設定
----------------

.. code-block:: yaml

   runtime:
     python:
       version: "3.12.3"

   scripts:
     releases:
       common:
         source: common
       maintenance:
         source: maintenance
         enabled: true

     registries:
       common:
         source: "git+https://github.com/example/common-scripts.git#v0.1.0"
       maintenance:
         source: "git+https://github.com/example/maintenance-scripts.git#v0.1.0"

``atlas scripts update`` は ``enabled`` が true のリリースを対象にします。
``enabled`` を省略した場合は true です。

registry alias
--------------

``scripts.registries`` は名前から実際の source へのローカルな対応表です。
Atlas に組み込みの公開 registry はありません。
同じ alias を複数ホストで使うことで、ホストごとの設定から取得元を制御できます。

source の形式
-------------

``atlas scripts install <source>`` と ``scripts.releases.<name>.source`` は以下を受け付けます。

.. list-table::
   :header-rows: 1

   * - 形式
     - 例
     - 備考
   * - ローカルディレクトリ
     - ``examples/basic-scripts-release``
     - その場のディレクトリを検証してインストール
   * - ローカル archive
     - ``release.tar.gz`` / ``release.zip``
     - cache に展開して検証
   * - HTTP(S) archive
     - ``https://example.com/release.tar.gz``
     - 30 秒 timeout でダウンロード
   * - git repository
     - ``git+https://github.com/example/repo.git#v1.0.0``
     - ``#ref`` は branch/tag/commit を指定
   * - registry alias
     - ``common``
     - ``scripts.registries`` で解決

ホストプロファイル
------------------

``/etc/atlas/host.yml`` はスクリプトに渡すホストメタデータです。
``name`` は必須で、空でない文字列でなければなりません。

.. code-block:: yaml

   name: worker-01
   site: nrt
   zone: nrt-a
   role: batch
   environment: production
   runtime_kind: baremetal
   tags:
     - trusted
     - nightly

``site``、``zone``、``role``、``environment``、``runtime_kind`` は任意ですが、存在する場合は文字列である必要があります。
``tags`` は省略、null、または文字列の配列を指定できます。

環境変数
--------

Atlas の配置は以下の環境変数で上書きできます。

.. list-table::
   :header-rows: 1

   * - 変数
     - 既定値
     - 用途
   * - ``ATLAS_HOME``
     - ``/opt/atlas``
     - runtime、scripts、shim、launcher の基点
   * - ``ATLAS_ETC_DIR``
     - ``/etc/atlas``
     - 設定ファイルとホストプロファイル
   * - ``ATLAS_VAR_DIR``
     - ``/var/lib/atlas``
     - logs と cache
   * - ``ATLAS_RUNTIME_DIR``
     - ``$ATLAS_HOME/runtime``
     - scripts venv の配置
   * - ``ATLAS_SCRIPTS_CURRENT_DIR``
     - ``$ATLAS_HOME/scripts/current``
     - アクティブリリース symlink の配置
   * - ``ATLAS_HOST_FILE``
     - ``$ATLAS_ETC_DIR/host.yml``
     - ``atlas_core.get_host()`` が読む host profile

スクリプト実行時には Atlas が追加で ``ATLAS_SCRIPT_NAME``、``ATLAS_SCRIPT_RELEASE_NAME``、``ATLAS_SCRIPT_VERSION``、``ATLAS_SCRIPTS_DIR`` を設定します。
