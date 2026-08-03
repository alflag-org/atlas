Atlas reference
===============

Configure an Atlas host
-----------------------

Atlas reads ``/etc/atlas/config.yml``. The schema is strict and rejects unknown keys.

.. code-block:: yaml

   runtime:
     python:
       version: "3.14.6"

   releases:
     configuration-operations:
       source: "/srv/releases/configuration-operations"
       enabled: true

     maintenance:
       source: "https://example.test/maintenance-1.2.0.tar.gz"
       enabled: false

``atlas release update`` updates every enabled entry. Naming one entry updates it even when
``enabled`` is false. A source may be a local directory, ``file:`` URL, local archive, HTTP(S)
archive, or ``git+https://github.com/example/project.git#ref``.

``/etc/atlas/host.yml`` supplies metadata to release code. ``name`` is required. ``site``, ``zone``,
``role``, ``environment``, and ``runtime_kind`` are optional strings; ``tags`` is a list of strings.

.. code-block:: yaml

   name: control-01
   site: kng01
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

Install and update releases
---------------------------

The manifest supplies a release name; ``atlas release install`` has no name override.

.. code-block:: bash

   atlas release install ./configuration-operations
   atlas release list --verbose
   atlas runtime install
   atlas status

   atlas release update
   atlas release update configuration-operations

Atlas copies and validates every requested source before activation. Release directories, active
links, launchers, and command shims change as one operation. A failed install or update leaves the
active set unchanged. Runtime installation restores the active environment when dependency
installation or validation fails.

``atlas runtime install`` reads ``requirements.lock`` when a release provides it, otherwise
``requirements.txt``. Only manifest commands receive shims below ``/opt/atlas/shims``. Jobs remain
available through ``atlas job``.

Write a release manifest
------------------------

``release.yml`` is the only source for executable discovery. Undeclared files are not executable
through Atlas.

.. code-block:: text

   configuration-operations/
   ├── VERSION
   ├── release.yml
   ├── requirements.txt
   ├── commands/
   ├── jobs/
   ├── init/systemd/
   └── modules/

.. code-block:: yaml

   schema: atlas.release/v1
   name: configuration-operations

   commands:
     atlas-ansible:
       runtime: python
       entrypoint: commands/atlas-ansible.py

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

Identifiers use lowercase letters, digits, and single hyphens. Command and job names may not
overlap within a release. ``atlas`` and ``artifact-runner`` are reserved command names.

Atlas rejects unknown manifest keys, unsupported runtimes, missing files, absolute or traversing
entrypoints, release symlinks, malformed service references, invalid unit suffixes, and duplicate
public command names across active releases.

The selected release's ``modules/`` directory is first on ``PYTHONPATH``. Module directories from
other active releases follow in release-name order, then the Atlas runtime package path and the
incoming ``PYTHONPATH``. Release code imports its context from ``atlas_core``:

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
   atlas run atlas-ansible diff site web01

   export PATH="/opt/atlas/shims:$PATH"
   atlas-ansible diff site web01

   atlas job list
   atlas job inspect configuration-operations inventory-refresh
   atlas job run configuration-operations inventory-refresh -- --site default

The shim invokes ``/opt/atlas/bin/artifact-runner``, which delegates to ``atlas run``. Arguments after
``--`` reach a job unchanged. Direct jobs inherit the caller's working directory.

A job instance binds a release job to host settings stored below ``/etc/atlas/jobs.d``:

.. code-block:: yaml

   schema: atlas.job-instance/v1
   release: configuration-operations
   job: inventory-refresh
   user: ops
   working_directory: /home/ops/repos/provisioning
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

Install systemd files
---------------------

.. code-block:: bash

   atlas systemd list configuration-operations
   atlas systemd diff configuration-operations inventory-refresh
   sudo atlas systemd install configuration-operations inventory-refresh
   sudo atlas systemd remove configuration-operations inventory-refresh

Each managed service has one ``ExecStart`` through ``/opt/atlas/bin/atlas``. It invokes a
manifest command or a matching job instance. A job-backed service must use
``atlas job instance run``, and its ``User=`` value must match the instance user.

Atlas writes ``atlas-<release>-<service>.service`` and an optional ``.timer`` with mode ``0644`` and
owner ``root:root``, then runs ``systemctl daemon-reload``. It does not enable, start, stop, or restart
units. Review the diff before using native systemd commands:

.. code-block:: bash

   sudo systemctl enable --now atlas-configuration-operations-inventory-refresh.timer
   systemctl status atlas-configuration-operations-inventory-refresh.timer
   journalctl -u atlas-configuration-operations-inventory-refresh.service

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
     runtime/
     releases/<release>/<version>/
     current/<release> -> ../releases/<release>/<version>
     shims/
     tmp/

   /var/lib/atlas/
     logs/runs.jsonl
     locks/
     cache/

Commands and jobs share one executor. Arguments remain a list and run with ``shell=False``. Atlas
records read-only Git context for the working directory and starts the child in a new process
group. A timeout sends SIGTERM, waits five seconds, then sends SIGKILL; its exit status is 124.
A held non-blocking job-instance lock returns 75.

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
   * - ``ATLAS_HOST_FILE``
     - Resolved host profile path

``/var/lib/atlas/logs/runs.jsonl`` stores artifact identity, correlation IDs, redacted arguments,
working directory, Git context, exit status, duration, timeout state, and lock name. Rotate and
collect it with the host's logging tools.

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
   Atlas returns 124, records ``timed_out``, and terminates the process group.

Systemd installation failure
   Check root permission, the unit files, destination symlinks, and
   ``systemctl daemon-reload``.

Back up ``/etc/atlas``, release source references, and required run logs. Installed release
directories and active links can be recreated from their sources.
