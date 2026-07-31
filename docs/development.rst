Development
===========

Local environment
-----------------

Atlas supports Python 3.11 through 3.14. Local development uses Python 3.14.6 from
``mise.toml``.

.. code-block:: bash

   mise install
   mise run setup
   mise run check

``mise run check`` runs Ruff against ``src``, ``operations``, and ``tests`` and enforces 100%
line and branch coverage in the test suite. It builds the source and wheel distributions.

Documentation
-------------

Documentation dependencies require Python 3.12 or newer.

.. code-block:: bash

   make html SPHINXOPTS=-W

The generated site is written to ``build/html``.

Docker
------

.. code-block:: bash

   docker compose build atlas check
   docker compose run --rm atlas
   docker compose run --rm check

The runtime image contains the installed Atlas package, the basic example release, its generated
shim, and its release dependencies. The check image runs the CLI smoke test, Ruff, pytest, and the
package build.
