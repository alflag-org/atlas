Host operations
===============

Initial installation
--------------------

Install the Atlas package and pyenv outside the artifact runtime. Create ``/etc/atlas``,
``/opt/atlas``, and ``/var/lib/atlas`` with ownership appropriate for the Atlas operator. Place
``config.yml`` and ``host.yml``, then install releases and the runtime.

.. code-block:: bash

   atlas release install /srv/releases/configuration-operations
   atlas release install /srv/releases/infrastructure-operations
   atlas runtime install
   atlas release shims
   atlas status

Infrastructure repository setup is separate:

.. code-block:: bash

   git clone <provisioning-repository> /home/ops/repos/provisioning
   cd /home/ops/repos/provisioning
   mise install
   mise run setup
   configctl validate site

Atlas commands never perform those Git or dependency-setup steps.

Use ``hostctl`` for managed hosts and ``imagectl`` for machine images. Provider and input schemas,
source binding, evidence, and deletion checks are documented in :doc:`proxmox`.

Scheduled inventory refresh
---------------------------

Install ``configuration-operations`` and create a host job instance as described in :doc:`jobs`.
Validate the generated native artifacts before installation:

.. code-block:: bash

   atlas job instance inspect provisioning-inventory-refresh
   atlas init diff configuration-operations inventory-refresh
   sudo atlas init install configuration-operations inventory-refresh
   systemd-analyze verify \
     /etc/systemd/system/atlas-configuration-operations-inventory-refresh.service \
     /etc/systemd/system/atlas-configuration-operations-inventory-refresh.timer

Atlas writes unit files atomically with mode ``0644`` and owner ``root:root``, then runs
``systemctl daemon-reload``. It does not enable, start, stop, or restart them. Use native systemd
commands after reviewing the diff:

.. code-block:: bash

   sudo systemctl enable --now atlas-configuration-operations-inventory-refresh.timer
   systemctl status atlas-configuration-operations-inventory-refresh.timer
   journalctl -u atlas-configuration-operations-inventory-refresh.service

Before removal, disable and stop the timer through systemd, then run
``sudo atlas init remove configuration-operations inventory-refresh``.

Release update
--------------

.. code-block:: bash

   atlas release update
   atlas runtime install
   atlas release shims
   atlas status

Atlas rebuilds the runtime in a temporary virtual environment and moves it into its final path
before installing release requirements. If installation fails, Atlas restores the existing
runtime.

Atlas validates every configured release source before changing active releases. During install
and multi-release update, it keeps previous version directories and active links until launcher
and shim refresh succeeds. On failure, Atlas restores the previous directories, links, and command
shims, even when replacing an existing version.

Run logs
--------

``/var/lib/atlas/logs/runs.jsonl`` is append-only from Atlas's perspective. Rotate and collect it
with the host's standard logging tools. Never put secret values directly in job-instance YAML.
Environment-file paths may be logged, but their contents are not.

Each record includes correlation identifiers, artifact identity, redacted arguments, working
directory, Git context, exit code, duration, timeout state, and lock name. The execution
diagnostic written to stderr uses the same redacted arguments.

Troubleshooting
---------------

Runtime installation failure
   Run ``atlas runtime status``. Verify pyenv visibility, the configured Python version, OS build
   dependencies, ``/opt/atlas/tmp`` capacity, and ``/var/lib/atlas/cache/python-build``.

Unknown command
   Run ``atlas command list --verbose`` and ``atlas which <command>``. Then verify
   ``/opt/atlas/shims`` is in ``PATH`` and that no cross-release collision stopped shim refresh.

Job lock conflict
   A lock conflict returns exit code 75 without waiting. Confirm the active process before
   retrying; do not delete a lock file as a substitute for checking the OS advisory lock.

Timed-out job
   Atlas returns 124, marks ``timed_out`` in the run log, and terminates the process group.

Systemd installation failure
   Verify root permission, unit validation, destination symlinks, and
   ``systemctl daemon-reload``. Atlas intentionally does not enable or start the unit.

Backup scope
------------

Back up ``/etc/atlas``, release source references, and any required run logs. Installed releases
under ``/opt/atlas/releases`` can be recreated from source. ``/opt/atlas/current`` contains
symlinks and must only point to installed version directories.
