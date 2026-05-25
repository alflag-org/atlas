開発
====

ローカル環境
------------

開発環境は mise を前提にしています。

.. code-block:: bash

   mise install
   mise run setup
   mise run check

利用できる主な task は以下です。

.. list-table::
   :header-rows: 1

   * - task
     - 内容
   * - ``mise run setup``
     - ``pip install -e '.[dev]'`` で開発依存をインストール
   * - ``mise run lint``
     - ``ruff check src tests``
   * - ``mise run test``
     - ``pytest -q``
   * - ``mise run build``
     - ``python -m build``
   * - ``mise run docs``
     - ``make html`` で Sphinx HTML を ``build/html`` に生成
   * - ``mise run check``
     - lint、test、build

ドキュメント
------------

ドキュメントは Sphinx で生成します。

.. code-block:: bash

   mise run docs

警告をエラーとして扱う確認は次を使います。

.. code-block:: bash

   make html SPHINXOPTS=-W

公開サイトの本文は日本語で書きます。
コード、コメント、docstring、commit message は英語で書きます。

Docker
------

Docker では runtime 用 image と check 用 image を分けています。

.. code-block:: bash

   docker compose build atlas check
   docker compose run --rm atlas
   docker compose run --rm check

``docker compose run --rm check`` はコンテナ内 Atlas 環境で sample script を実行し、Ruff、pytest、package build を実行します。

変更時の確認
------------

変更の種類に応じて、狭い確認から広い確認へ進めます。

* CLI や runtime の挙動変更: 関連する pytest を先に実行し、最後に ``mise run check``
* docs 変更: ``make html SPHINXOPTS=-W``
* packaging 変更: ``python -m build`` または ``mise run check``
* script release 形式の変更: ``examples/basic-scripts-release`` と ``examples/companion-scripts-release`` を使った smoke test

公開 API の扱い
---------------

``atlas_core`` はインストール済みスクリプトが使う安定 API です。
互換性を壊す変更は慎重に扱い、``tests/test_core_public_api.py`` と ``tests/test_atlas_core_public.py`` を更新して意図を明示してください。

``atlas`` パッケージはホスト側 CLI 実装です。
ドキュメントでは maintainer 向けに参照を提供しますが、スクリプトから直接 import する前提にはしません。
