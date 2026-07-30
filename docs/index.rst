Atlas ドキュメント
==================

Atlas は Python ベースのスクリプトリリースを、ホスト上で安全に配布・更新・実行するための軽量なランタイムマネージャーです。
Python Fire コマンドの運用を主な対象にしつつ、ランタイム作成、リリースの検証とインストール、コマンド検出、shim 生成、実行ログ記録、ホストコンテキスト提供を一つの小さな CLI にまとめます。

Atlas が重視することは、スクリプト実行基盤に必要な境界を明確にすることです。
リリース成果物は ``VERSION`` と ``commands/`` を持つディレクトリとして扱い、インストール済みリリースは symlink でアクティブ化します。
コマンド名の衝突は黙って上書きせず、検出・shim 生成・実行・検索の各段階で失敗させます。

.. toctree::
   :maxdepth: 2
   :caption: 利用者向け

   concepts
   usage
   configuration
   script-releases
   operations

.. toctree::
   :maxdepth: 2
   :caption: Atlas 1.0 設計

   architecture
   adr/0001-release-artifacts-and-repository-boundaries
   migration

.. toctree::
   :maxdepth: 2
   :caption: 開発者向け

   api
   development
   writing-guide
   hosting
