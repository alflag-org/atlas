Documentation guide
===================

Write operating documentation from implemented behavior. State the exact command, path, schema,
failure condition, or rollback action a reader needs. Separate current behavior from future work.

Use established Atlas terms consistently:

- Atlas release
- command
- job
- job instance
- service
- init artifact
- external infrastructure repository

Do not reintroduce ``scripts release`` as a synonym. Mention the old term only in migration
material. New public identifiers use lowercase words separated by single hyphens and command names
prefer ``<domain>-<verb>``.

Before publishing, run:

.. code-block:: bash

   make html SPHINXOPTS=-W

Confirm every new page is in the toctree, examples match current argument parsing, and temporary
plans or unimplemented promises are absent.
