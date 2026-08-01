Public operation commands are domain controllers
================================================

Status
------

Accepted.

Context
-------

The first-party releases exposed each Ansible and Proxmox implementation step as a separate
command. Operators had to select the right primitive and preserve its execution order. The same
PATH namespace mixed configuration, provider diagnostics, operation artifacts, hosts, and machine
images.

Decision
--------

Atlas exposes five first-party operation commands: ``configctl``, ``hostctl``, ``imagectl``,
``providerctl``, and ``operationctl``. Each command owns one domain and has an explicit,
single-level subcommand parser. ``atlas`` remains the host runtime CLI and does not gain
infrastructure lifecycle subcommands.

The implementations live in two releases:

* ``configuration-operations`` contains ``configctl`` and configuration jobs. It owns the Ansible
  dependency.
* ``infrastructure-operations`` contains the other four controllers and provider, resource, and
  operation jobs. It owns the Pydantic and Proxmox dependencies.

Jobs have no shims. Controllers compose them through ``atlas job run`` and compose another public
operation only when that boundary matters. In particular, ``configctl diff-many`` invokes
``configctl diff`` once per target. Controllers pass argument lists with ``shell=False`` and keep
the caller's working directory, environment, streams, and child exit status.

Command classification
----------------------

.. list-table::
   :header-rows: 1

   * - Previous command
     - Public interface
     - Private implementation
   * - ``config-validate``
     - ``configctl validate``
     - configuration job
   * - ``config-check``
     - ``configctl check``
     - configuration job
   * - ``config-diff``
     - ``configctl diff``
     - configuration job
   * - ``config-diff-many``
     - ``configctl diff-many``
     - public command composition
   * - ``config-apply``
     - ``configctl apply``
     - configuration job
   * - ``inventory-show``
     - ``configctl inventory``
     - configuration job
   * - ``proxmox-status``
     - ``providerctl status``
     - provider job
   * - ``vm-create-*``
     - ``hostctl`` lifecycle subcommands
     - provider jobs
   * - ``vm-template-create-*``
     - ``imagectl`` lifecycle subcommands
     - provider jobs
   * - ``operation-artifact-validate``
     - ``operationctl validate``
     - operation artifact job
   * - ``operation-artifact-inspect``
     - ``operationctl inspect``
     - operation artifact job

Controller names use ``<domain>ctl`` only when the domain, resource or state model, and supported
operations are specific. Names such as ``infractl`` or ``opsctl`` are rejected because they erase
those boundaries. Subcommands use concrete verbs; arbitrary command dispatch is not supported.

Output contract
---------------

stdout contains results that can be redirected or piped: JSON artifacts, verification results,
inventory, or diffs. stderr contains diagnostics, progress, child commands, and warnings. A
controller returns its child status unchanged. Controller-owned validation uses exit 2, a missing
child executable uses 127, and signal exits retain their conventional status.

Removal criteria
----------------

The previous public commands may be removed only after repository and production callers use the
controllers, systemd and runbook references are updated, the release manifest snapshot contains
only the five controllers, and the agreed observation period has no use of a deprecated shim.
Removal deletes the old manifest entries; it does not add permanent aliases or fallback dispatch.

Consequences
------------

Operators choose a domain first and then a verb. Atlas still records each private job as an
artifact run, including parent and operation correlation. The implementation modules and safety
checks remain reusable, but their old executable names are no longer part of PATH.
