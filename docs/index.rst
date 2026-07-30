Atlas ドキュメント
==================

Atlas は Python ベースのリリースを、ホスト上で安全に配布・更新・実行するための軽量なランタイムマネージャーです。
manifest に宣言した command と job を同じ実行経路で扱い、service の systemd artifact を検証・比較・配置・削除し、ランタイム作成、リリースの検証とインストール、shim 生成、相関付き実行ログ、ホストコンテキスト提供を一つの小さな CLI にまとめます。
同じ repository の ``operations/`` では、独立した first-party release として Ansible project を操作する command を提供します。

Atlas が重視することは、スクリプト実行基盤に必要な境界を明確にすることです。
リリース成果物は ``VERSION`` と ``release.yml`` を持つディレクトリとして扱い、manifest に明示された command だけを公開します。
インストール済みリリースは symlink でアクティブ化します。
コマンド名の衝突は黙って上書きせず、検出・shim 生成・実行・検索の各段階で失敗させます。

.. toctree::
   :maxdepth: 2
   :caption: 利用者向け

   concepts
   usage
   configuration
   script-releases
   jobs
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
