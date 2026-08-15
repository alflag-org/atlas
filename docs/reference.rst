Operator reference
==================

Install Atlas as the host-side runtime and keep the automation programs it executes outside the
Atlas directory. Atlas owns runtime links, Python venvs, generated shims, context files, and run
records.

Filesystem layout
-----------------

The default layout is:

.. code-block:: text

   /etc/atlas/
   ├── config.yml
   └── host.yml

   /opt/atlas/
   ├── runtimes/python/
   ├── venvs/
   ├── shims/
   └── launchers/

   /var/lib/atlas/
   ├── logs/runs.jsonl
   └── runtime-state/

The paths can be redirected for tests with ``ATLAS_HOME``, ``ATLAS_ETC_DIR``,
``ATLAS_VAR_DIR``, ``ATLAS_RUNTIMES_DIR``, ``ATLAS_VENVS_DIR``, ``ATLAS_SHIMS_DIR``, and
``ATLAS_HOST_FILE``.

Register programs
-----------------

``config.yml`` contains only runtime selections and local program registrations:

.. code-block:: yaml

   runtime:
     python:
       version: "3.13"

   programs:
     provisioning:
       root: /srv/provisioning
       runtime:
         type: python
         python: "3.13"
         venv: provisioning
     image-tool:
       root: /opt/image-tool
       runtime:
         type: native

``root`` must be an absolute path. A Python program uses its own named venv. A native program is
started without a Python intermediary. The global Python selection is used when a Python program
does not specify its own version. A configured Python executable may be supplied as
``runtime.python.executable`` when the host's managed interpreter is not exposed through pyenv.

Atlas never clones, fetches, downloads, extracts, installs, updates, rolls back, or garbage
collects program trees. Place and update those trees using the program's normal packaging or
deployment workflow.

Command discovery and shims
---------------------------

Python files below ``program-root/commands`` are commands. Executable files below
``program-root/commands`` and, for native programs, ``program-root/bin`` are native commands.
Path segments use lowercase letters, digits, and hyphens. Nested paths are joined with hyphens.
Atlas rejects symlinks and duplicate command names.

Generate shims after registering or changing a program:

.. code-block:: console

   $ atlas command list --verbose
   $ atlas shim generate
   $ export PATH="/opt/atlas/shims:$PATH"
   $ host-diff web01

Generated shims call ``python -m atlas.cli run <command>``. They do not contain a second
execution implementation.

Runtime and venvs
-----------------

``atlas runtime install`` resolves each configured Python version and exposes it below
``/opt/atlas/runtimes/python``. If ``pyenv`` is available, Atlas installs a missing selected
version through that existing runtime manager; otherwise use ``runtime.python.executable`` or an
explicitly provisioned interpreter. Atlas does not use the host's system Python implicitly and
does not install program dependencies. ``atlas venv create <program>`` creates the dedicated venv.
Install dependencies using the program's existing ``pyproject.toml``, requirements file, lock
file, or other standard tooling.

.. code-block:: console

   $ atlas runtime status
   $ atlas runtime install
   $ atlas venv create provisioning
   $ atlas venv list

Execution context
-----------------

``/etc/atlas/host.yml`` identifies only the current host:

.. code-block:: yaml

   version: 1
   host:
     id: control01
     role: control
     site: kanagawa01

During execution Atlas writes a short-lived JSON document and sets ``ATLAS_CONTEXT_FILE``. The
document contains the host, standard paths, program, command, working directory, and
``run_id``, ``parent_run_id``, and ``operation_id``. The same identifiers are available as
environment variables. Python code can call ``atlas_core.get_context()``; other languages can
parse the JSON file directly.

The diagnostic command does not require a registered program:

.. code-block:: console

   $ atlas context
   $ atlas status
   $ atlas check

Run records
-----------

Each spawned command appends one JSON object to ``/var/lib/atlas/logs/runs.jsonl``. It records the
UTC timestamp, host id, user, program, command, run identifiers, working directory, duration, and
exit status. Stdout and stderr are not copied into this log.

The executor preserves the child exit status, returns ``124`` for a timeout, and maps a signal
termination to ``128 + signal``. The child is launched with the caller's working directory and
with an exact argument vector; shell interpretation is not used.
