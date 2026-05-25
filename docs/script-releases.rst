スクリプトリリース
==================

リリース構造
------------

Atlas が扱うリリースは、少なくとも ``VERSION`` と ``commands/`` を含むディレクトリです。

.. code-block:: text

   my-release/
     VERSION
     commands/
       hello.py
       admin/
         restart.py
     modules/
       my_release_helpers/
         __init__.py
     requirements.txt

``VERSION`` は空でない文字列である必要があります。
``commands/`` は必須です。
``modules/`` と requirements ファイルは任意です。

コマンド名の決定
----------------

``commands/`` 配下の ``.py`` ファイルは、相対パスの各 segment を ``-`` で結合したコマンド名になります。

.. list-table::
   :header-rows: 1

   * - ファイル
     - コマンド名
   * - ``commands/hello.py``
     - ``hello``
   * - ``commands/admin/restart.py``
     - ``admin-restart``
   * - ``commands/db/backup/full.py``
     - ``db-backup-full``

各 segment は ``^[a-z][a-z0-9-]*$`` に一致する必要があります。
``atlas`` と ``script-runner`` は予約済みです。

リリース内モジュール
--------------------

``modules/`` が存在する場合、Atlas は対象リリースの ``modules/`` を ``PYTHONPATH`` の先頭へ追加します。
他のアクティブリリースの ``modules/`` も後続に追加されます。
リリース固有の helper は、標準的な Python package として ``modules/`` 配下へ置いてください。

.. code-block:: python

   from my_release_helpers.formatting import format_message

   def main(name: str = "world") -> None:
       print(format_message(name))

依存関係
--------

``atlas runtime install`` は各リリースの ``requirements.lock`` または ``requirements.txt`` を検出して scripts venv に入れます。
両方ある場合は ``requirements.lock`` が優先されます。

リリース更新後に Python 依存が変わった場合は、runtime を再インストールしてください。

.. code-block:: bash

   atlas scripts update common
   atlas runtime install

安全性の制約
------------

Atlas はリリース内の symlink を許可しません。
archive からインストールする場合も、絶対パス、path traversal、symlink、hard link を拒否します。

これは、配布物が Atlas の管理ディレクトリ外を書き換えたり、意図しないファイルを参照したりすることを避けるためです。

atlas_core の利用
-----------------

インストール済みスクリプトは ``atlas`` 内部モジュールではなく ``atlas_core`` を import してください。

.. code-block:: python

   from atlas_core import get_context

   def main() -> None:
       ctx = get_context()
       print(ctx.host.name)
       print(ctx.script.release_name)

``atlas_core`` は scripts runtime に同期される安定 API です。
``atlas`` パッケージはホスト側 CLI 実装の内部 API として扱ってください。

実行ログ
--------

``atlas run`` は実行ごとに ``/var/lib/atlas/logs/runs.jsonl`` へ 1 行の JSON を追記します。
記録される主な項目は timestamp、release、script、args、version、exit_code、duration_ms です。

引数に ``password``、``token``、``secret``、``key`` などを含むオプション名がある場合、値は ``***`` にマスクされます。
ログファイルのローテーションや収集はホスト側の通常のログ基盤で行ってください。
