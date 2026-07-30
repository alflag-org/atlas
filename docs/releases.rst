Release authoring
=================

Layout
------

.. code-block:: text

   operations/
   ├── VERSION
   ├── release.yml
   ├── requirements.txt
   ├── commands/
   ├── jobs/
   ├── init/systemd/
   ├── modules/
   └── assets/

``release.yml`` is the only artifact-discovery source. Files that are not declared as commands
or jobs are never made executable by Atlas.

Manifest
--------

.. code-block:: yaml

   schema: atlas.release/v1
   name: operations

   commands:
     config-diff:
       runtime: python
       entrypoint: commands/config-diff.py

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
not overlap within a release. Public command names should follow ``<domain>-<verb>``; multi-target
composition commands use ``<domain>-<verb>-many``.

Dependencies and modules
------------------------

``atlas runtime install`` reads ``requirements.lock`` when present, otherwise
``requirements.txt``. The active release's ``modules/`` directory is added to ``PYTHONPATH``.
Modules from unrelated releases are not added.

Release code imports stable runtime context from ``atlas_core``:

.. code-block:: python

   from atlas_core import get_context

   context = get_context()
   print(context.artifact.release_name)
   print(context.artifact.operation_id)

Safety checks
-------------

Atlas rejects unknown keys, unsupported runtimes, missing files, absolute or traversing
entrypoints, symlinks anywhere in a release, malformed service references, invalid unit suffixes,
and duplicate public command names across active releases.
