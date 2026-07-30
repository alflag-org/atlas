Development
===========

Local checks
------------

.. code-block:: bash

   mise install
   mise run setup
   mise run lint
   mise run test
   mise run build
   mise run check

Ruff checks ``src``, ``operations``, and ``tests``. Coverage includes the host package,
``atlas_core``, first-party operation modules, and command entrypoints. Both line and branch
coverage must remain at 100%.

Documentation
-------------

.. code-block:: bash

   make html SPHINXOPTS=-W

The generated site is written to ``build/html``. Keep README, CLI examples, Docker smoke tests,
and Sphinx pages aligned whenever a public command, path, manifest field, or environment variable
changes.

Docker
------

.. code-block:: bash

   docker compose build atlas check
   docker compose run --rm atlas
   docker compose run --rm check

The runtime image installs ``examples/basic-release`` and builds the declared release
dependencies into the shared Python environment.

Change review
-------------

Run the narrowest relevant test first, then ``mise run check``. For release-contract changes,
also validate ``operations``, both example releases, command-only shim generation, and a job
instance. For init changes, test diff, atomic replacement, removal, mode/ownership, and
``daemon-reload`` failure.

The ``atlas_core`` package is deliberately small. Do not add Ansible, inventory, Terraform,
scheduler, subprocess, or logging-framework APIs to it.
