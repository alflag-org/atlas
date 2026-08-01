Jobs and job instances
======================

Jobs are non-interactive, one-shot Python artifacts declared in ``release.yml``. They use the same
execution, logging, environment, timeout, signal, and redaction behavior as commands, but Atlas
does not generate job shims.

Direct jobs
-----------

List, inspect, or invoke a job explicitly:

.. code-block:: bash

   atlas job list
   atlas job list configuration-operations
   atlas job inspect configuration-operations inventory-refresh
   atlas job run configuration-operations inventory-refresh -- --site default

Arguments after ``--`` are passed without shell parsing. A direct job inherits the caller's
current working directory. The manifest may provide ``default_timeout_seconds``.

Job instances
-------------

A job instance binds a release job to the host settings needed by a scheduler or service manager:
user, working directory, arguments, environment files, timeout, and advisory lock. Instance files
live below ``/etc/atlas/jobs.d``:

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

Use the instance commands to validate and invoke the resolved job:

.. code-block:: bash

   atlas job instance list
   atlas job instance inspect provisioning-inventory-refresh
   atlas job instance run provisioning-inventory-refresh

The schema rejects unknown keys. The working directory and environment-file paths must be
absolute. Arguments remain a list of strings. Timeout values must be positive integers. Names use
lowercase words separated by single hyphens.

User and environment behavior
-----------------------------

Atlas does not switch users or invoke ``sudo``. Direct instance execution fails when the declared
user differs from the caller. A systemd service can invoke the same instance through
``/opt/atlas/bin/atlas job instance run <instance>``. Before installation, Atlas requires the
unit's single ``User=`` value to equal the instance's ``user`` and requires the instance to
resolve to the service's declared release and job.

Environment files accept ``NAME=value`` lines, comments, blank lines, and matching single or
double quotes around a value. Atlas reads their values only into the child environment; it does
not include file contents in the run record.

Timeout and lock behavior
-------------------------

A job instance timeout overrides the job's default. On timeout Atlas sends SIGTERM to the child
process group, waits for a grace period, then sends SIGKILL if needed. Timeout returns exit code
124. Keyboard interruption also terminates the process group and returns control without leaving
descendants running.

Each instance uses a non-blocking advisory lock below ``/var/lib/atlas/locks``. The instance name
is the default lock name. If another process holds the lock, Atlas exits with code 75 instead of
waiting.

Nested execution
----------------

Atlas prepends ``/opt/atlas/shims`` and the artifact runtime ``bin`` directory to child ``PATH``.
A controller may therefore compose another public controller or invoke a private job through the
Atlas launcher.

Every execution has a ``run_id``. A root execution sets ``operation_id`` to that value and has no
parent. A nested Atlas process reads ``ATLAS_RUN_ID`` and ``ATLAS_OPERATION_ID``, records the
caller as ``parent_run_id``, and retains the operation ID. Atlas writes this relationship to
``/var/lib/atlas/logs/runs.jsonl`` together with cwd and read-only Git context.

Systemd service artifacts
-------------------------

Use ``atlas init list`` and ``atlas init diff`` to inspect manifest-declared services. Installation
writes ``atlas-<release>-<service>.service`` and optional ``.timer`` names, then runs
``systemctl daemon-reload``. Atlas does not enable, start, stop, or restart a unit. The complete
``configuration-operations/inventory-refresh`` is documented in :doc:`operations`.
