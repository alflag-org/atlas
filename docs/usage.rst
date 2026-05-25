使い方
======

基本コマンド
------------

Atlas の CLI は ``atlas`` コマンドから操作します。

.. code-block:: bash

   atlas status
   atlas runtime status
   atlas runtime install
   atlas scripts install examples/basic-scripts-release --name sample
   atlas scripts update
   atlas scripts list --verbose
   atlas scripts shims
   atlas which sample
   atlas run sample hello --name=takuya

状態確認
--------

``atlas status`` は設定ファイル、ホストプロファイル、アクティブなリリース、検出済みコマンド数、shim などの配置を表示します。
まずこのコマンドでホストの見え方を確認します。

.. code-block:: bash

   atlas status

``atlas runtime status`` は pyenv の利用可否、設定された Python バージョン、scripts venv の有無を表示します。

.. code-block:: bash

   atlas runtime status

ランタイム作成
--------------

``atlas runtime install`` は ``/etc/atlas/config.yml`` の ``runtime.python.version`` を読み、pyenv の Python から scripts venv を作成します。
pyenv 本体と Python build に必要な OS 依存は、事前にホストへ入れておく必要があります。

.. code-block:: bash

   atlas runtime install

既にインストール済みリリースがある場合、リリース内の ``requirements.lock`` または ``requirements.txt`` も scripts venv に取り込まれます。

リリースのインストール
----------------------

ローカルディレクトリをリリース名 ``sample`` としてインストールする例です。

.. code-block:: bash

   atlas scripts install examples/basic-scripts-release --name sample

インストール時には以下が行われます。

* リリース構造と ``VERSION`` の検証
* ``commands/`` 配下の Python コマンド検出
* リリース内 symlink の拒否
* ``/opt/atlas/scripts/releases/<name>/<version>`` への配置
* ``/opt/atlas/scripts/current/<name>`` symlink の更新
* ``atlas_core`` の同期
* launcher と shim の生成

設定済みリリースの更新
----------------------

``config.yml`` の ``scripts.releases`` にある有効なリリースをすべて更新します。

.. code-block:: bash

   atlas scripts update

特定のリリースだけを更新する場合はリリース名を指定します。

.. code-block:: bash

   atlas scripts update common

更新中に失敗した場合、更新対象の ``current`` symlink は可能な限り元の状態へ戻されます。

コマンド一覧と検索
------------------

コマンド名だけを一覧します。

.. code-block:: bash

   atlas scripts list

詳細表示では、コマンド名、リリース名、リリースバージョン、実体のスクリプトパスを表示します。

.. code-block:: bash

   atlas scripts list --verbose

特定コマンドの実体パスを確認します。

.. code-block:: bash

   atlas which sample

実行
----

``atlas run`` は scripts venv の Python で対象スクリプトを実行します。
引数はそのままスクリプトへ渡されます。

.. code-block:: bash

   atlas run sample hello --name=takuya

実行時には ``ATLAS_SCRIPT_NAME``、``ATLAS_SCRIPT_RELEASE_NAME``、``ATLAS_SCRIPT_VERSION``、``ATLAS_SCRIPTS_DIR`` などの環境変数が設定されます。
スクリプト側では直接環境変数を読むより、安定 API である ``atlas_core.get_context()`` を使うことを推奨します。

shim 経由の実行
---------------

``/opt/atlas/shims`` を ``PATH`` に追加すると、検出済みコマンドを直接実行できます。

.. code-block:: bash

   export PATH="/opt/atlas/shims:$PATH"
   sample --name=takuya

shim は内部で ``atlas run <command-name>`` を呼び出します。
shim を再生成する場合は次を実行します。

.. code-block:: bash

   atlas scripts shims
