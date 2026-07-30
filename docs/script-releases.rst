Command and job script releases
===============================

Required files
--------------

The manifest stage of Atlas requires ``VERSION`` and ``release.yml``. Command files, release-local
modules, and dependency files remain ordinary release contents. Declared commands are public;
declared jobs are reached only through ``atlas job``.

.. code-block:: text

   sample/
     VERSION
     release.yml
     commands/
       hello.py
       admin/
         restart.py
     jobs/
       inventory-refresh.py
     modules/
       sample_helpers/
         __init__.py
     requirements.txt

``VERSION`` must contain a non-empty value that is safe as one filesystem segment. A release
cannot contain symlinks.

Manifest
--------

The current schema is ``atlas.release/v1``:

.. code-block:: yaml

   schema: atlas.release/v1
   name: sample
   commands:
     hello:
       runtime: python
       entrypoint: commands/hello.py
     admin-restart:
       runtime: python
       entrypoint: commands/admin/restart.py
   jobs:
     inventory-refresh:
       runtime: python
       entrypoint: jobs/inventory-refresh.py
       default_timeout_seconds: 300

The YAML parser rejects duplicate keys. The manifest rejects unknown keys, unsupported schemas,
unsupported runtimes, invalid names, missing entrypoints, absolute paths, parent traversal,
symlinks, and non-Python command entrypoints. A Python file that is present below ``commands/``
but absent from the manifest is not published.

Command and job names cannot overlap within one release. Only commands receive shims. A job may
set a positive ``default_timeout_seconds`` value; a job instance may override it.

Release and command names use this grammar:

.. code-block:: text

   [a-z][a-z0-9]*(?:-[a-z0-9]+)*

``atlas``, ``script-runner``, and ``artifact-runner`` are reserved command names. Public command
names normally follow ``<domain>-<verb>`` as described in :doc:`architecture`.

The manifest ``name`` is canonical. ``atlas scripts install --name`` may assert the expected name
but cannot rename a release. A configured ``scripts.releases`` key must therefore match the source
manifest name.

Release-local modules
---------------------

When ``modules/`` exists, Atlas places the selected release's module directory first on
``PYTHONPATH``. Module directories from other active releases follow it. Release helpers should be
normal Python packages below ``modules/``.

.. code-block:: python

   from sample_helpers.formatting import format_message

   def main(name: str = "world") -> None:
       print(format_message(name))

First-party operations release
------------------------------

``operations/`` follows the same manifest contract but remains a release artifact separate from
the Atlas core wheel. Its ``VERSION`` and ``release.yml`` declare six public configuration
commands. The release contains no environment inventory, playbooks, roles, or Git-management
logic; callers supply an independent Ansible project as the working directory.

The tag release workflow archives the contents of ``operations/`` as
``atlas-operations-<version>.tar.gz``. Runtime dependencies come from
``operations/requirements.txt`` and are installed only during the explicit runtime setup step.

Dependencies
------------

``atlas runtime install`` installs ``requirements.lock`` or ``requirements.txt`` from active
releases into the scripts environment. When both exist, ``requirements.lock`` wins. Reinstall the
runtime after changing release dependencies.

.. code-block:: bash

   atlas scripts update sample
   atlas runtime install

Stable runtime API
------------------

Installed commands and jobs import ``atlas_core`` rather than modules below ``atlas``:

.. code-block:: python

   from atlas_core import get_context

   def main() -> None:
       context = get_context()
       print(context.host.name)
       print(context.script.release_name)

``atlas_core`` is synchronized into the scripts runtime. The ``atlas`` package remains an
implementation detail of the host CLI.

Run records
-----------

``atlas run`` and ``atlas job`` append one JSON object per execution to
``/var/lib/atlas/logs/runs.jsonl``. The current record includes run, parent, and operation IDs,
timestamp, release, artifact type and name, arguments, version, cwd, Git context, timeout, lock,
exit status, and duration.
Values passed through option names containing ``password``, ``token``, ``secret``, or ``key`` are
redacted. The host's normal logging system remains responsible for rotation and collection.
