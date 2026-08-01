Runtime behavior
================

Installed packages
------------------

The host CLI is installed as the ``atlas`` package. Release code uses ``atlas_core`` for host
paths, host profile values, artifact identity, and run correlation identifiers. First-party
operations are packaged as ``configuration-operations`` and ``infrastructure-operations``.

Filesystem
----------

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
     lib/
     tmp/

   /var/lib/atlas/
     logs/runs.jsonl
     locks/
     cache/

Paths below ``/home/ops/repos`` are an operator convention. Atlas uses the caller's working
directory but does not discover infrastructure repositories or modify their Git state.

Artifact execution
------------------

Commands and jobs use the same executor. For each run, Atlas creates ``run_id``,
``parent_run_id``, and ``operation_id`` values. It records read-only Git context for the working
directory, prepends the shim and runtime directories to ``PATH``, and starts the artifact in a
new process group. Arguments are passed as a list with ``shell=False``.

Timeout handling sends SIGTERM to the process group and then SIGKILL after five seconds. A timeout
returns exit code 124. Job-instance locks use non-blocking ``flock`` locks below
``/var/lib/atlas/locks`` and return exit code 75 when already held.

Systemd files
-------------

Atlas validates, diffs, installs, and removes systemd files declared by a release. Installed unit
names are ``atlas-<release>-<service>.service`` and optionally
``atlas-<release>-<service>.timer``. Each service has exactly one ``ExecStart`` through
``/opt/atlas/bin/atlas``.

Installation uses atomic replacement with mode ``0644`` and owner ``root:root``, then runs
``systemctl daemon-reload``. Atlas does not enable, start, stop, or restart units.
