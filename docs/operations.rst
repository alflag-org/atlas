運用
====

初期セットアップ
----------------

本番ホストでは、Atlas を実行するユーザーが以下のディレクトリへ必要な権限を持つようにします。

* ``/etc/atlas``
* ``/opt/atlas``
* ``/var/lib/atlas``

pyenv と Python build に必要な OS パッケージは Atlas の外で準備します。
その後、設定ファイルとホストプロファイルを配置して runtime を作成します。

.. code-block:: bash

   atlas runtime status
   atlas runtime install

リリース更新手順
----------------

標準的な更新手順は以下です。

.. code-block:: bash

   atlas scripts update
   atlas runtime install
   atlas scripts shims
   atlas status

``atlas scripts update`` は各リリースの ``current`` symlink を更新します。
``atlas runtime install`` は scripts venv を一時ディレクトリに作ってから差し替えるため、既存 venv を直接上書きしません。

PATH 設定
---------

ユーザーや service からリリースコマンドを直接呼びたい場合、``/opt/atlas/shims`` を ``PATH`` に追加します。

.. code-block:: bash

   export PATH="/opt/atlas/shims:$PATH"

shim はコマンドごとに symlink として生成され、共通の ``script-runner`` を経由して ``atlas run`` を呼びます。

ログ管理
--------

実行ログは ``/var/lib/atlas/logs/runs.jsonl`` に追記されます。
Atlas はログローテーションを行わないため、systemd、logrotate、外部 log collector などホスト側の標準機構で管理してください。

障害時の確認順
--------------

``atlas runtime install`` が失敗する場合:

1. ``atlas runtime status`` で pyenv の可視性と Python version を確認します。
2. ``runtime.python.version`` が pyenv で install 可能な値か確認します。
3. OS の Python build 依存が揃っているか確認します。

コマンドが見つからない場合:

1. ``atlas scripts list --verbose`` でコマンドが検出されているか確認します。
2. ``atlas which <command>`` で実体パスを確認します。
3. ``/opt/atlas/shims`` が ``PATH`` に入っているか確認します。
4. コマンド名衝突の例外が出ていないか確認します。

スクリプト内で host 情報が読めない場合:

1. ``/etc/atlas/host.yml`` が存在するか確認します。
2. ``host.yml`` が mapping で、``name`` が空でない文字列か確認します。
3. ``ATLAS_HOST_FILE`` を上書きしている場合、そのパスを確認します。

バックアップと復旧
------------------

Atlas の状態復旧に重要なのは以下です。

* ``/etc/atlas/config.yml``
* ``/etc/atlas/host.yml``
* ``/opt/atlas/scripts/releases``
* ``/opt/atlas/scripts/current``
* 必要に応じて ``/var/lib/atlas/logs``

``scripts/current`` は symlink 群なので、復旧時はリンク先が ``scripts/releases`` 内の実体へ向いていることを確認してください。
