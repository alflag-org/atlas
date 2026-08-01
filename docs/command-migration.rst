Replace previous operation commands
===================================

The first-party manifests do not create shims for the previous primitive command names. Update
callers before installing these releases:

.. list-table::
   :header-rows: 1

   * - Previous invocation
     - Current invocation
   * - ``config-validate site``
     - ``configctl validate site``
   * - ``config-check site web01``
     - ``configctl check site web01``
   * - ``config-diff site web01``
     - ``configctl diff site web01``
   * - ``config-diff-many site web01 web02``
     - ``configctl diff-many site web01 web02``
   * - ``config-apply site web01``
     - ``configctl apply site web01``
   * - ``inventory-show``
     - ``configctl inventory``
   * - ``proxmox-status provider.yml``
     - ``providerctl status provider.yml``
   * - ``vm-create-plan/apply/verify/rollback ...``
     - matching ``hostctl`` lifecycle subcommand
   * - ``vm-template-create-plan/apply/verify/rollback ...``
     - matching ``imagectl`` lifecycle subcommand
   * - ``operation-artifact-validate artifact.json``
     - ``operationctl validate artifact.json``
   * - ``operation-artifact-inspect artifact.json``
     - ``operationctl inspect artifact.json``

There are no permanent aliases or fallback dispatch. Search provisioning repositories, systemd
units, scheduled jobs, shell scripts, and runbooks for previous names. After updating callers,
install ``configuration-operations`` and ``infrastructure-operations``, rebuild the runtime, run
``atlas release shims``, and verify ``atlas command list`` contains exactly five operation
controllers.

The native Ansible argv, Proxmox plan and evidence schemas, source digest checks, confirmation
rules, ownership marker, rollback checks, stdout, stderr, and exit meanings are unchanged.
