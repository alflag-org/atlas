Usage
=====

Install a release
-----------------

.. code-block:: bash

   atlas release install ./operations
   atlas release list --verbose
   atlas command list --verbose
   atlas runtime install

The release manifest supplies its name. There is no command-line name override.

Update configured releases
--------------------------

.. code-block:: bash

   atlas release update
   atlas release update operations
   atlas release shims
   atlas status

Activation-link updates are rolled back when validation or cross-release command indexing fails.
Installed but inactive version directories may remain for inspection.

Run commands
------------

.. code-block:: bash

   atlas which config-diff
   atlas run config-diff site web01

   export PATH="/opt/atlas/shims:$PATH"
   config-diff site web01

The shim points to ``/opt/atlas/bin/artifact-runner``, which delegates to
``atlas run <command>``.

Run jobs
--------

.. code-block:: bash

   atlas job list
   atlas job inspect operations inventory-refresh
   atlas job run operations inventory-refresh -- --site default

Direct jobs inherit the caller's working directory. Arguments after ``--`` are passed unchanged.

Run job instances
-----------------

.. code-block:: bash

   atlas job instance list
   atlas job instance inspect provisioning-inventory-refresh
   atlas job instance run provisioning-inventory-refresh

Instances load working directory, arguments, environment-file paths, timeout, and lock settings
from ``/etc/atlas/jobs.d``.

Manage Atlas systemd artifacts
------------------------------

.. code-block:: bash

   atlas init list operations
   atlas init diff operations inventory-refresh
   sudo atlas init install operations inventory-refresh
   sudo atlas init remove operations inventory-refresh

Every managed service must have exactly one ``ExecStart`` through ``/opt/atlas/bin/atlas`` and
must invoke the command declared by its manifest or a matching job instance. Job-backed services
must use ``atlas job instance run``; Atlas validates that ``User=`` matches the job-instance user
and that the instance references the service's declared release and job.

Use native commands for lifecycle actions:

.. code-block:: bash

   systemctl enable --now atlas-operations-inventory-refresh.timer
   systemctl status atlas-operations-inventory-refresh.timer
   journalctl -u atlas-operations-inventory-refresh.service
