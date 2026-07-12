開発
====

ローカル環境
------------

開発環境は mise を前提にしています。
mise が管理するのは、ローカル開発と CI で使う Python interpreter と外部 CLI です。
Python package dependency は ``pyproject.toml`` で管理します。
Atlas 本番環境の script runtime は引き続き pyenv と ``/opt/atlas/runtime`` を使い、mise へ移行しません。

.. code-block:: bash

   mise install
   python -m pip install -e '.[dev]'
   ruff check src tests
   python -m coverage run -m pytest -q
   python -m coverage report
   python -m build

``python -m coverage report`` は line coverage と branch coverage の両方で 100% を要求します。
新しい分岐や失敗経路を追加した場合は、production で必要な挙動としてテストも追加してください。

ドキュメント
------------

ドキュメントは Sphinx で生成します。

.. code-block:: bash

   make html

警告をエラーとして扱う確認は次を使います。

.. code-block:: bash

   make html SPHINXOPTS=-W

公開サイトの本文は日本語で書きます。
コード、コメント、docstring、commit message は英語で書きます。
文体とページ構成の詳しい基準は :doc:`writing-guide` にまとめています。

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

* CLI や runtime の挙動変更: 関連する pytest を先に実行し、最後に上記の Ruff、coverage、package build
* docs 変更: ``make html SPHINXOPTS=-W``
* packaging 変更: ``python -m build``
* script release 形式の変更: ``examples/basic-scripts-release`` と ``examples/companion-scripts-release`` を使った smoke test

公開 API の扱い
---------------

``atlas_core`` はインストール済みスクリプトが使う安定 API です。
互換性を壊す変更は慎重に扱い、``tests/test_core_public_api.py`` と ``tests/test_atlas_core_public.py`` を更新して意図を明示してください。

``atlas`` パッケージはホスト側 CLI 実装です。
ドキュメントでは maintainer 向けに参照を提供しますが、スクリプトから直接 import する前提にはしません。
