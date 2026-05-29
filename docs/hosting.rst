Cloudflare Pages ホスティング
=============================

Atlas のドキュメントは静的 HTML として生成し、Cloudflare Pages に公開できます。
リポジトリには ``wrangler.jsonc`` を置き、Pages が公開する出力ディレクトリをコード側にも残しています。

Pages の設定
------------

Cloudflare Pages プロジェクトでは次の値を使います。

.. list-table::
   :header-rows: 1

   * - 項目
     - 値
   * - Project name
     - ``atlas-docs``
   * - Production branch
     - ``master``
   * - Build command
     - ``python -m pip install -e '.[dev]' && python -m sphinx -b html docs docs/_build/html``
   * - Build output directory
     - ``docs/_build/html``
   * - Environment variable
     - ``PYTHON_VERSION=3.12``

build command は Atlas と開発用依存を入れてから、Sphinx HTML を生成します。
Shibuya theme は ``.[dev]`` に含まれているため、Pages 側で追加のテーマ設定は不要です。

ローカル確認
------------

公開前に、警告をエラーとして扱ってビルドします。

.. code-block:: bash

   python -m sphinx -W -b html docs docs/_build/html

通常の開発フローでは次の task も使えます。

.. code-block:: bash

   mise run docs

直接デプロイ
------------

Wrangler がインストール済みで認証も済んでいる場合、生成済みサイトを直接デプロイできます。

.. code-block:: bash

   npx wrangler pages deploy docs/_build/html --project-name atlas-docs

通常運用では Git integration を使います。
``master`` から production deployment を作り、pull request では preview deployment を確認します。
