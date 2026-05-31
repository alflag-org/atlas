Cloudflare Workers ホスティング
===============================

Atlas のドキュメントは静的 HTML として生成し、Cloudflare Workers の static assets として公開します。
公開先は ``https://atlas-docs.jp0.workers.dev/`` です。
Cloudflare の現行運用では、Pages の ``build output directory`` ではなく
``wrangler.jsonc`` の ``assets.directory`` が配信対象になります。

Workers Builds の設定
---------------------

Cloudflare Dashboard の Workers Builds では次の値を使います。

.. list-table::
   :header-rows: 1

   * - 項目
     - 値
   * - Worker name
     - ``atlas-docs``
   * - Production branch
     - ``master``
   * - Build command
     - ``make html``
   * - Deploy command
     - ``npx wrangler deploy``
   * - Root directory
     - ``.``

``make html`` は ``pip install -e '.[dev]'`` を実行したうえで、
Sphinx HTML を ``build/html`` に生成します。
Wrangler は ``wrangler.jsonc`` の ``assets.directory`` を読み、
同じ ``build/html`` を Worker の static assets としてアップロードします。

Cloudflare の公式移行ガイドでも、Pages の ``wrangler pages deploy`` から
Workers の ``wrangler deploy`` へ移行し、Pages の ``build output directory`` の代わりに
``assets.directory`` を使うことが案内されています。

ローカル確認
------------

公開前に、警告をエラーとして扱ってビルドします。

.. code-block:: bash

   make html SPHINXOPTS=-W

通常の開発フローでは次の task も使えます。

.. code-block:: bash

   mise run docs

Worker としての配信確認も必要なら、ローカルで次を実行します。

.. code-block:: bash

   npx wrangler dev

直接デプロイ
------------

Wrangler がインストール済みで認証も済んでいる場合、生成済みサイトを直接デプロイできます。

.. code-block:: bash

   make html
   npx wrangler deploy

通常運用では Workers Builds の Git integration を使います。
``master`` への push で production deployment が更新され、
production 以外の branch では preview version が作られます。
