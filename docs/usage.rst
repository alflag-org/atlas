Usage
=====

Install a release
-----------------

.. code-block:: bash

   atlas release install ./configuration-operations
   atlas release install ./infrastructure-operations
   atlas release list --verbose
   atlas command list --verbose
   atlas runtime install

The release manifest supplies its name. There is no command-line name override.

Update configured releases
--------------------------

.. code-block:: bash

   atlas release update
   atlas release update configuration-operations
   atlas release shims
   atlas status

Atlas copies and validates every requested source before activation. Release directories, current
links, the launcher, the artifact runner, and command shims are committed as one operation. If a
step fails, Atlas restores the prior active set, even when replacing an existing version directory.

Run commands
------------

.. code-block:: bash

   atlas which configctl
   atlas run configctl diff site web01

   export PATH="/opt/atlas/shims:$PATH"
   configctl diff site web01

The shim points to ``/opt/atlas/bin/artifact-runner``, which delegates to
``atlas run <command>``.

Run jobs
--------

.. code-block:: bash

   atlas job list
   atlas job inspect configuration-operations inventory-refresh
   atlas job run configuration-operations inventory-refresh -- --site default

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

   atlas init list configuration-operations
   atlas init diff configuration-operations inventory-refresh
   sudo atlas init install configuration-operations inventory-refresh
   sudo atlas init remove configuration-operations inventory-refresh

Every managed service must have exactly one ``ExecStart`` through ``/opt/atlas/bin/atlas``. It
must invoke either the command declared by its manifest or a matching job instance. Job-backed
services must use ``atlas job instance run``. Atlas checks that ``User=`` matches the job-instance
user and that the instance references the service's declared release and job.

Use native commands for lifecycle actions:

.. code-block:: bash

   systemctl enable --now atlas-configuration-operations-inventory-refresh.timer
   systemctl status atlas-configuration-operations-inventory-refresh.timer
   journalctl -u atlas-configuration-operations-inventory-refresh.service
