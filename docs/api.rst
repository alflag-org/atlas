Python API
==========

API の境界
----------

Atlas には 2 つの Python package があります。

``atlas_core``
   インストール済みスクリプトから利用する安定 API です。
   ``get_context()``、``get_host()``、``get_paths()`` と、それらが返す dataclass を公開します。

``atlas``
   ホスト側 CLI とリリース管理の内部実装です。
   maintainer 向けに参照を生成しますが、スクリプトリリースから直接 import する対象ではありません。

スクリプト向け安定 API
----------------------

.. automodule:: atlas_core
   :members:
   :undoc-members:

コンテキスト
~~~~~~~~~~~~

.. automodule:: atlas_core.context
   :members:
   :undoc-members:

ホストプロファイル
~~~~~~~~~~~~~~~~~~

.. automodule:: atlas_core.host
   :members:
   :undoc-members:

パス情報
~~~~~~~~

.. automodule:: atlas_core.paths
   :members:
   :undoc-members:

利用例
------

.. code-block:: python

   from atlas_core import get_context

   def main() -> None:
       ctx = get_context()
       if ctx.host.has_tag("trusted"):
           print(f"{ctx.script.name} runs on {ctx.host.name}")

``get_context()`` は Atlas が ``atlas run`` または shim 経由で設定した環境変数を前提にします。
通常の Python インタプリタから直接呼ぶ場合は、必要な環境変数を渡してテストしてください。

.. code-block:: python

   from atlas_core import get_context

   ctx = get_context({
       "ATLAS_SCRIPT_NAME": "hello",
       "ATLAS_SCRIPT_RELEASE_NAME": "sample",
       "ATLAS_SCRIPT_VERSION": "1.0.0",
       "ATLAS_SCRIPTS_DIR": "/opt/atlas/scripts/current/sample",
       "ATLAS_HOST_FILE": "/etc/atlas/host.yml",
   })

maintainer 向け内部 API
-----------------------

以下は Atlas CLI とホスト側処理を構成する内部モジュールです。
リリーススクリプトからの利用は想定していません。

.. currentmodule:: atlas

.. autosummary::
   :toctree: generated
   :recursive:

   cli
   catalog
   config
   errors
   execution
   files
   init
   job_instances
   jobs
   launchers
   locks
   manifests
   paths
   releases
   runtime
   sources
   yamlutil
