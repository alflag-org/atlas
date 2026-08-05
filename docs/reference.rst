Atlas reference
===============

Install Atlas
-------------

Atlas supports Python 3.11 through 3.14 on Linux. This guide uses
``/srv/atlas/source`` for an operator-managed checkout:

.. code-block:: bash

   git clone https://github.com/alflag-org/atlas.git /srv/atlas/source
   python -m pip install /srv/atlas/source

Another durable checkout path is valid. Use that same path in release source configuration. Atlas
reads the checkout but does not pull, reset, or otherwise modify it.

The host needs Git for Git-backed release sources and execution context. ``atlas runtime install``
also requires ``pyenv`` on ``PATH`` and the operating-system packages needed to build the configured
Python version. The account running Atlas must be able to write the configured home, configuration,
and state directories.

Configure an Atlas host
-----------------------

Atlas reads ``/etc/atlas/config.yml``. The schema is strict and rejects unknown keys.

.. note::

   Resource names and release source values in these examples are placeholders. Replace them with
   values for your environment.

.. code-block:: yaml

   runtime:
     python:
       version: "3.14.6"

   releases:
     operations:
       source: "/srv/atlas/source/operations"
       enabled: true

     maintenance:
       source: "https://example.test/maintenance-1.2.0.tar.gz"
       enabled: false

``atlas release update`` updates every enabled entry. Naming one entry updates it even when
``enabled`` is false. A source may be a local directory, ``file:`` URL, local archive, HTTP(S)
archive, or ``git+https://github.com/example/operations.git#v1.0.0``.

``/etc/atlas/host.yml`` supplies metadata to release code. ``name`` is required. ``site``, ``zone``,
``role``, ``environment``, and ``runtime_kind`` are optional strings; ``tags`` is a list of strings.

.. code-block:: yaml

   name: control-01
   site: site-a
   zone: management
   role: control
   environment: production
   runtime_kind: vm
   tags:
     - trusted

These environment variables change host-side paths:

.. list-table::
   :header-rows: 1

   * - Variable
     - Default
   * - ``ATLAS_HOME``
     - ``/opt/atlas``
   * - ``ATLAS_ETC_DIR``
     - ``/etc/atlas``
   * - ``ATLAS_VAR_DIR``
     - ``/var/lib/atlas``
   * - ``ATLAS_RUNTIME_DIR``
     - ``$ATLAS_HOME/runtime``
   * - ``ATLAS_TMP_DIR``
     - ``$ATLAS_HOME/tmp``

Keep source and installed paths separate
----------------------------------------

The default paths have distinct owners and purposes:

.. list-table::
   :header-rows: 1

   * - Path
     - Managed by
     - Purpose
   * - ``/srv/atlas/source``
     - Operator
     - Example checkout containing release sources; another durable path is valid.
   * - ``/etc/atlas``
     - Operator
     - Host configuration, job instances, and child-process environment files.
   * - ``/opt/atlas/releases/<release>/<version>-<content-digest>``
     - Atlas
     - Validated, digest-addressed snapshot of one release source; an installed snapshot is never replaced.
   * - ``/opt/atlas/current/<release>``
     - Atlas
     - Symbolic link selecting the active installed release.
   * - ``/opt/atlas/bin`` and ``/opt/atlas/shims``
     - Atlas
     - Stable launchers and public command shims.
   * - ``/opt/atlas/runtime``
     - Atlas
     - Shared Python runtime for active releases.
   * - ``/var/lib/atlas``
     - Atlas
     - Execution records, locks, and source or build caches.

Do not configure ``/opt/atlas/releases`` or ``/opt/atlas/current`` as a release source. Atlas
replaces content below those directories during installation and activation. The bundled systemd
artifacts and adapter use ``/opt/atlas/bin/atlas`` as the stable launcher, so hosts that install
them must keep the default ``ATLAS_HOME=/opt/atlas``.

Install and update releases
---------------------------

The manifest supplies a release name; ``atlas release install`` has no name override.

.. code-block:: bash

   atlas release install /srv/atlas/source/operations
   atlas release list --verbose
   atlas runtime install
   atlas status

   atlas release update
   atlas release update operations

Atlas validates every requested source, copies it to a staged digest-addressed snapshot below
``$ATLAS_HOME/releases``, revalidates the staged tree, and then atomically switches the link below
``$ATLAS_HOME/current``. Installed snapshots are never replaced, so a running child keeps its
selected tree while a later install activates a new snapshot. A failed install or update leaves the
active set unchanged.
Runtime installation restores the active environment when dependency installation or validation
fails.

``atlas runtime install`` reads ``requirements.lock`` when a release provides it, otherwise
``requirements.txt``. Only manifest commands receive shims below ``/opt/atlas/shims``. Jobs remain
available through ``atlas job``.

Write a release manifest
------------------------

``release.yml`` is the only source for executable discovery. Undeclared files are not executable
through Atlas.

.. code-block:: text

   operations/
   ├── VERSION
   ├── release.yml
   ├── requirements.txt
   ├── init/systemd/
   └── modules/
       ├── atlas_configuration_operations/
       ├── atlas_host_operations/
       ├── atlas_image_operations/
       └── atlas_operations/

.. code-block:: yaml

   schema: atlas.release/v1
   name: operations

   commands:
     atlas-ansible:
       target: atlas_configuration_operations.controller:main

   jobs:
     inventory-refresh:
       target: atlas_configuration_operations.inventory_refresh:main
       default_timeout_seconds: 300

   services:
     inventory-refresh:
       job: inventory-refresh
       init:
         systemd:
           service: init/systemd/inventory-refresh.service
           timer: init/systemd/inventory-refresh.timer

Identifiers use lowercase letters, digits, and single hyphens. Command and job names may not
overlap within a release. ``atlas`` and ``artifact-runner`` are reserved command names.

Atlas rejects unknown manifest keys, malformed or missing targets, targets outside the selected
release, missing dotted parent package initializers, ambiguous module paths, symlinks, malformed
service references, invalid unit suffixes, and duplicate public command names across active
releases. A target uses the ``package.module:callable`` form. Atlas resolves the final module and
every parent package below the selected release's ``modules/`` directory without importing release
code. The child runner loads those exact source files in order, then checks the same callable
contract: one unambiguous top-level synchronous function that accepts ``argv`` and returns an
integer or ``None``. Required arguments beyond ``argv``, duplicate definitions, rebindings, async
functions, and incompatible return annotations are rejected.

The selected release's ``modules/`` directory is first on ``PYTHONPATH``, followed by Atlas's
support packages. The caller's ``PYTHONPATH`` and other active releases are not exposed to the
child. Release code imports its context from ``atlas_core``:

.. code-block:: python

   from atlas_core import get_context

   context = get_context()
   print(context.host.name)
   print(context.artifact.operation_id)

Run commands and jobs
---------------------

.. code-block:: bash

   atlas command list --verbose
   atlas which atlas-ansible
   atlas run atlas-ansible diff site web-01

   export PATH="/opt/atlas/shims:$PATH"
   atlas-ansible diff site web-01

   atlas job list
   atlas job inspect operations inventory-refresh
   atlas job run operations inventory-refresh -- --site default

The shim invokes ``/opt/atlas/bin/artifact-runner``, which delegates to ``atlas run``. Arguments after
``--`` reach a job unchanged. Direct jobs inherit the caller's working directory.

A job instance binds a release job to host settings stored below ``/etc/atlas/jobs.d``:

.. code-block:: yaml

   schema: atlas.job-instance/v1
   release: operations
   job: inventory-refresh
   user: ops
   working_directory: /srv/provisioning
   arguments:
     - --site
     - default
   environment_files:
     - /etc/atlas/env/provisioning.env
   timeout_seconds: 300
   lock: provisioning-inventory-refresh

.. code-block:: bash

   atlas job instance list
   atlas job instance inspect provisioning-inventory-refresh
   atlas job instance run provisioning-inventory-refresh

Working directories and environment-file paths must be absolute. Atlas reads environment values
only into the child process and does not put them in the run record. It does not switch users or
invoke ``sudo``; direct execution fails when the declared user differs from the caller.

The bundled inventory-refresh systemd service uses the ``ops`` account and the
``provisioning-inventory-refresh`` job instance. Create that account and instance before installing
the unit. A maintained release variant may use another account or instance name, but its ``User=``
and job instance must change together.

Install systemd files
---------------------

.. code-block:: bash

   atlas systemd list operations
   atlas systemd diff operations inventory-refresh
   sudo atlas systemd install operations inventory-refresh
   sudo atlas systemd remove operations inventory-refresh

Each managed service has one ``ExecStart`` through ``/opt/atlas/bin/atlas``. It invokes a
manifest command or a matching job instance. A job-backed service must use
``atlas job instance run``, and its ``User=`` value must match the instance user.

Atlas writes ``atlas-<release>-<service>.service`` and an optional ``.timer`` with mode ``0644`` and
owner ``root:root``, then runs ``systemctl daemon-reload``. It does not enable, start, stop, or restart
units. Review the diff before using native systemd commands:

.. code-block:: bash

   sudo systemctl enable --now atlas-operations-inventory-refresh.timer
   systemctl status atlas-operations-inventory-refresh.timer
   journalctl -u atlas-operations-inventory-refresh.service

Read execution state
--------------------

The default layout is:

.. code-block:: text

   /etc/atlas/
     config.yml
     host.yml
     jobs.d/
     env/

   /opt/atlas/
     bin/atlas
     bin/artifact-runner
     lib/python/atlas_release_runner.py
     runtime/
     releases/<release>/<version>-<content-digest>/
     current/<release> -> ../releases/<release>/<version>-<content-digest>
     shims/
     tmp/

   /var/lib/atlas/
     logs/runs.jsonl
     locks/
     cache/

Commands and jobs share one executor. Arguments remain a list and run with ``shell=False``. Atlas
records read-only Git context for the working directory and starts the child in a new process
group. A timeout or termination signal reaches descendant process groups as well: Atlas sends
SIGTERM, allows five seconds for cleanup, then sends SIGKILL to groups that remain. A timeout's
exit status is 124. A held non-blocking job-instance lock returns 75.

Each run receives ``run_id``, ``parent_run_id``, and ``operation_id``. Nested Atlas execution records the
caller as its parent while retaining the operation ID. Release code receives the same values as
``ATLAS_RUN_ID``, ``ATLAS_PARENT_RUN_ID``, and ``ATLAS_OPERATION_ID``, plus:

.. list-table::
   :header-rows: 1

   * - Variable
     - Value
   * - ``ATLAS_RELEASE_NAME``
     - Manifest release name
   * - ``ATLAS_RELEASE_VERSION``
     - Contents of ``VERSION``
   * - ``ATLAS_ARTIFACT_TYPE``
     - ``command`` or ``job``
   * - ``ATLAS_ARTIFACT_NAME``
     - Manifest artifact name
   * - ``ATLAS_RELEASE_ROOT``
     - Installed directory used by the run
   * - ``ATLAS_RELEASE_DIGEST``
     - SHA-256 content identity of the installed snapshot used by the run
   * - ``ATLAS_HOST_FILE``
     - Resolved host profile path

``/var/lib/atlas/logs/runs.jsonl`` stores artifact identity, release content digest, correlation IDs,
redacted arguments, working directory, Git context, exit status, duration, timeout state, and lock
name. Rotate and collect it with the host's logging tools.

Recover from failures
---------------------

Runtime installation failure
   Run ``atlas runtime status``. Check pyenv visibility, the configured Python version, OS build
   dependencies, free space below ``/opt/atlas/tmp``, and
   ``/var/lib/atlas/cache/python-build``.

Unknown command
   Run ``atlas command list --verbose`` and ``atlas which <command>``. Confirm that
   ``/opt/atlas/shims`` is in ``PATH`` and resolve any cross-release name collision.

Job lock conflict
   Confirm the active process before retrying. Deleting a lock file does not replace checking the
   operating-system lock.

Timed-out job
   Atlas returns 124, records ``timed_out``, and terminates the process group and its descendants.

Systemd installation failure
   Check root permission, the unit files, destination symlinks, and
   ``systemctl daemon-reload``.

Back up ``/etc/atlas``, release source references, and required run logs. Installed release
directories and active links can be recreated from their sources.
