Migration from Atlas 0.3
========================

Compatibility decision
----------------------

Atlas 1.0 does not read the former ``scripts`` configuration, ``ATLAS_SCRIPT_*`` variables, or
``/opt/atlas/scripts`` layout. It does not provide ``atlas scripts`` or ``script-runner`` aliases.
This is an intentional breaking migration.

Clean migration
---------------

1. Record the current release sources and active versions.
2. Stop schedulers and services that invoke old shims.
3. Back up ``/etc/atlas``, ``/opt/atlas/scripts``, and required run logs.
4. Install Atlas 1.0.
5. Replace ``config.yml`` with the strict ``runtime`` and ``releases`` schema.
6. Add ``release.yml`` to every release; do not rely on file discovery.
7. Install each release with ``atlas release install``.
8. Rebuild the runtime with ``atlas runtime install``.
9. Verify commands, jobs, and systemd diffs.
10. Switch callers to the new shims and remove the old tree after the observation period.

.. code-block:: bash

   atlas release install /srv/releases/operations
   atlas runtime install
   atlas command list --verbose
   atlas job list
   atlas init list
   atlas status

External desired state
----------------------

Move Ansible inventory, playbooks, roles, and collections to an independent provisioning
repository whose root is the Ansible project root. Do not copy the old product wrapper, release
packaging, or Atlas shim into that repository.

Before retiring an old wrapper, compare its check/diff results against ``config-check`` and
``config-diff`` on representative targets. Archive an old repository only after real-host smoke
tests succeed.

Rollback
--------

Before deleting the old installation, rollback consists of stopping new callers, restoring the
backed-up Atlas 0.3 configuration and tree, and restoring the previous executable path. Systemd
rollback removes installed ``atlas-<release>-<service>`` units and runs
``systemctl daemon-reload``.

There is no in-process dual-read compatibility mode. Do not point Atlas 1.0 at an Atlas 0.3
filesystem and expect automatic conversion.
