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

First-party operations release
------------------------------

repository の ``operations/`` は Atlas core wheel と別に version を持つ release artifact です。
``release.yml`` は次の command だけを公開します。

.. list-table::
   :header-rows: 1

   * - command
     - child process
   * - ``config-validate <playbook>``
     - ``ansible-playbook playbooks/<playbook>.yml --syntax-check``
   * - ``config-check <playbook> <target>``
     - ``ansible-playbook playbooks/<playbook>.yml --limit <target> --check``
   * - ``config-diff <playbook> <target>``
     - ``ansible-playbook playbooks/<playbook>.yml --limit <target> --check --diff``
   * - ``config-apply <playbook> <target>``
     - ``ansible-playbook playbooks/<playbook>.yml --limit <target>``
   * - ``inventory-show``
     - ``ansible-inventory --graph``
   * - ``config-diff-many <playbook> [target...]``
     - target ごとに public ``config-diff`` executable を一回ずつ実行

command は current working directory を Ansible project root として扱い、通常ファイルの
``ansible.cfg`` と ``playbooks/<name>.yml`` を要求します。playbook 名は
``[a-z][a-z0-9_-]*`` に限定し、absolute path、parent traversal、symlink を受け付けません。
``config-apply`` は target を省略できず、暗黙の all-host apply、確認 prompt、``--yes`` を
追加しません。stdout、stderr、exit code は Ansible child process からそのまま引き継ぎます。

``config-diff-many`` は argv の target、次に stdin の target を読み、空行を無視して初出順に
重複を除きます。一件が失敗しても残りを直列実行し、最初の non-zero exit code を返します。
Ansible を直接起動せず ``config-diff`` shim を呼ぶため、各 child run は親と同じ
``operation_id`` を記録します。

この段階の CLI で source checkout を試す場合は、operations release をインストールしてから
release dependency を runtime へ反映します。

.. code-block:: bash

   atlas scripts install ./operations
   atlas runtime install
   cd /path/to/provisioning
   config-validate site
   config-diff site web01

``operations/requirements.txt`` は ``ansible-core`` を宣言します。command 実行中に
``ansible-galaxy install``、collection update、``pip install``、repository の
``clone`` / ``pull`` / ``checkout`` は行いません。

release tag の workflow は ``atlas-operations-<version>.tar.gz`` を Atlas core package と
別に生成します。Global Registry からの取得は、Global Registry が software-release API を
公開するまで利用できません。Atlas 側で未定義の resource kind や host-local alias を
registry integration として追加しません。

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

各 record は ``run_id``、``parent_run_id``、``operation_id``、artifact type/name、cwd、
Git context、timeout、lock、exit code を記録します。job instance の lock file は
``/var/lib/atlas/locks`` に置かれ、取得できない場合は待機せず exit code 75 で失敗します。

障害時の確認順
--------------

``atlas runtime install`` が失敗する場合:

1. ``atlas runtime status`` で pyenv の可視性と Python version を確認します。
2. ``runtime.python.version`` が pyenv で install 可能な値か確認します。
3. OS の Python build 依存が揃っているか確認します。

コマンドが見つからない場合:

1. ``atlas scripts list --verbose`` でコマンドが manifest に宣言されているか確認します。
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
