Release authoring
=================

Layout
------

.. code-block:: text

   configuration-operations/
   ├── VERSION
   ├── release.yml
   ├── requirements.txt
   ├── commands/
   ├── jobs/
   ├── init/systemd/
   └── modules/

``release.yml`` is the only artifact-discovery source. Files that are not declared as commands
or jobs are never made executable by Atlas.

Manifest
--------

.. code-block:: yaml

   schema: atlas.release/v1
   name: configuration-operations

   commands:
     configctl:
       runtime: python
       entrypoint: commands/configctl.py

   jobs:
     inventory-refresh:
       runtime: python
       entrypoint: jobs/inventory-refresh.py
       default_timeout_seconds: 300

   services:
     inventory-refresh:
       job: inventory-refresh
       init:
         systemd:
           service: init/systemd/inventory-refresh.service
           timer: init/systemd/inventory-refresh.timer

Identifiers use lowercase letters, digits, and single hyphens. Command and job namespaces may
not overlap within a release.

Dependencies and modules
------------------------

``atlas runtime install`` reads ``requirements.lock`` when present, otherwise
``requirements.txt``. The selected release's ``modules/`` directory is placed first on
``PYTHONPATH``. Module directories from the other active releases follow in release-name order,
then Atlas's runtime package path and the caller's existing ``PYTHONPATH``.

Release code imports runtime context from ``atlas_core``:

.. code-block:: python

   from atlas_core import get_context

   context = get_context()
   print(context.artifact.release_name)
   print(context.artifact.operation_id)

Safety checks
-------------

Atlas rejects unknown keys, unsupported runtimes, missing files, absolute or traversing
entrypoints, symlinks anywhere in a release, malformed service references, invalid unit suffixes,
duplicate public command names across active releases, and systemd ``ExecStart`` values that do
not use the Atlas host launcher for the declared command or a matching job instance.

Reserved public command names are ``atlas`` and ``artifact-runner``.
