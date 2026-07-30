Migration from Atlas 0.3
========================

Compatibility decision
----------------------

Atlas 1.0 does not read the former ``scripts`` configuration, ``ATLAS_SCRIPT_*`` variables, or
``/opt/atlas/scripts`` layout. It does not provide ``atlas scripts`` or ``script-runner`` aliases.
This is an intentional breaking migration. Atlas performs no in-place conversion and must not be
started against a partially converted host.

Prepare the cutover
-------------------

1. Record every installed release source, active version, shim caller, job scheduler, and service.
2. Choose a maintenance window and an observation period.
3. Stop schedulers and services that invoke former shims.
4. Back up the complete ``/etc/atlas`` and ``/opt/atlas`` trees plus required run logs.
5. Record ownership, modes, package versions, service enablement, and checksums needed to restore
   that snapshot.
6. Keep the backup outside ``/opt/atlas`` so a clean installation cannot overwrite it.

Do not delete ``/opt/atlas/scripts`` during preparation. It remains the rollback source until the
new installation has completed its observation period.

Install the final layout
------------------------

With all callers stopped:

1. Install the Atlas 1.0 package.
2. Replace ``config.yml`` with the strict ``runtime`` and ``releases`` schema.
3. Add and validate ``release.yml`` for every release; do not rely on file discovery.
4. Install each release into ``/opt/atlas/releases``.
5. Rebuild the shared runtime.
6. Regenerate final command shims.
7. Reinstall declared systemd artifacts after reviewing their diffs.

.. code-block:: bash

   atlas release install /srv/releases/operations
   atlas runtime install
   atlas release shims
   atlas command list --verbose
   atlas job list
   atlas init list
   atlas status

``atlas status`` must report ``/opt/atlas/releases``, ``/opt/atlas/current``,
``/opt/atlas/bin/artifact-runner``, and the runtime Python path. Update shell ``PATH`` settings,
job-instance files, and native service references before starting callers. No final configuration
or unit may refer to ``atlas scripts``, ``script-runner``, ``ATLAS_SCRIPT_*``, or
``/opt/atlas/scripts``.

Verify and commit the cutover
-----------------------------

Run read-only commands and diffs against representative infrastructure targets. Compare
replacement operation output with the retired wrapper, run a command through its final shim, run
each scheduled job instance manually, validate systemd units, and inspect correlated run records.
Then start callers gradually and observe them for the agreed period.

The cutover is committed only after:

- release and command lists match the intended active set;
- every current link resolves below ``/opt/atlas/releases``;
- runtime dependency checks succeed;
- command, job, timeout, lock, and run-log behavior is confirmed;
- systemd units use the stable Atlas launcher and expected user;
- no process reads the former tree.

After these checks, remove ``/opt/atlas/scripts`` and obsolete caller configuration. Removal is a
separate, explicit step; Atlas never deletes the former tree automatically.

Rollback
--------

If any pre-commit check fails, stop new callers and restore the complete snapshot: the Atlas 0.3
package, ``/etc/atlas``, ``/opt/atlas``, ownership and modes, executable ``PATH``, and systemd
state. Run ``systemctl daemon-reload`` after restoring native unit files.

Do not retain a mixture of final configuration with former paths, or final binaries with former
environment variables. Rollback restores one coherent host version; it does not activate a
dual-read compatibility mode.

External repository migration
-----------------------------

Move Ansible inventory, playbooks, roles, and collections to an independent provisioning
repository whose root is the Ansible project root. Do not copy an old product wrapper, release
packaging, or Atlas shim into that repository.

Before retiring an old wrapper, compare its check and diff results against ``config-check`` and
``config-diff`` on representative targets. Archive an old repository only after real-host smoke
tests and the observation period succeed.
