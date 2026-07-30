Architecture and ownership
==========================

Runtime boundary
----------------

Atlas core is installed as the ``atlas`` Python package. Release code receives the smaller
``atlas_core`` API plus environment variables describing the current artifact and execution.
Infrastructure backends such as Ansible are release dependencies, not public ``atlas_core`` APIs.

The first-party ``operations`` directory is developed in this repository but remains a separate
Atlas release. The GitHub release workflow packages it as its own archive.

Filesystem boundary
-------------------

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

``/home/ops/repos`` is an operator convention for independent infrastructure repositories. Atlas
does not hard-code or discover it.

Execution boundary
------------------

Commands and jobs share one execution path. Atlas creates ``run_id``, ``parent_run_id``, and
``operation_id`` values, captures read-only Git context for the working directory, prepends the
shim and runtime directories to ``PATH``, and starts the artifact in a new process group.

Timeout handling sends SIGTERM to the group and then SIGKILL after a grace period. Job-instance
locks use ``flock`` and never wait by default.

Systemd boundary
----------------

Atlas validates, diffs, atomically installs, and removes only init artifacts declared by an Atlas
release. Installed unit names are ``atlas-<release>-<service>.service`` and optionally ``.timer``.
Each service must have exactly one ``ExecStart`` through ``/opt/atlas/bin/atlas``. The referenced
command must match the service declaration; a job-backed service must invoke a job instance whose
release, job, and user match. Atlas runs ``systemctl daemon-reload`` after a change, but does not
enable, start, stop, or restart a unit.

Global Registry boundary
------------------------

Release acquisition currently accepts explicit local, archive, HTTP(S), and Git sources. A remote
Global Registry release-resolution contract is not defined by the currently deployed registry API,
so Atlas does not invent one or treat the old host-local alias map as a global registry.
