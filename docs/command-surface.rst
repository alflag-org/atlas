First-party command surface
===========================

Atlas installs command shims only for these first-party operation controllers:

.. code-block:: text

   configctl
   hostctl
   imagectl
   providerctl
   operationctl

``atlas`` is installed by the host package. It manages the runtime, releases, commands, jobs, and
init artifacts; it does not manage infrastructure resources.

Command responsibilities
------------------------

.. list-table::
   :header-rows: 1

   * - Command
     - Owns
     - Does not own
   * - ``configctl``
     - Configuration project validation, inventory, check, diff, and apply
     - Provider resources or Registry mutation
   * - ``hostctl``
     - Managed host plan, apply, status, resume, verify, and rollback
     - Machine images or arbitrary provider actions
   * - ``imagectl``
     - Machine image plan, apply, verify, and rollback
     - Host configuration or provider administration
   * - ``providerctl``
     - Provider definition validation and read-only status
     - Provider mutation
   * - ``operationctl``
     - Plan and evidence validation, inspection, and durable operation status
     - Host, image, provider, or configuration mutation

Controller syntax
-----------------

.. code-block:: text

   configctl validate PLAYBOOK
   configctl check PLAYBOOK TARGET
   configctl diff PLAYBOOK TARGET
   configctl diff-many PLAYBOOK [TARGET ...]
   configctl apply PLAYBOOK TARGET
   configctl inventory

   hostctl plan HOST_SPEC
   hostctl apply [PLAN] --confirm PLAN_ID
   hostctl status PLAN_OR_OPERATION
   hostctl resume PLAN_OR_OPERATION --confirm PLAN_ID
   hostctl verify RESOURCE_OR_OPERATION
   hostctl rollback PLAN_OR_OPERATION --confirm PLAN_ID

   imagectl plan PROVIDER INPUT
   imagectl apply PROVIDER [PLAN] --confirm PLAN_ID
   imagectl status TARGET
   imagectl resume TARGET --confirm PLAN_ID
   imagectl verify PROVIDER [PLAN_OR_EVIDENCE]
   imagectl rollback PROVIDER [EVIDENCE] --confirm PLAN_ID

   providerctl validate PROVIDER
   providerctl status PROVIDER

   operationctl validate [ARTIFACT]
   operationctl inspect [ARTIFACT]
   operationctl status OPERATION_ID

An omitted optional artifact and ``-`` both read stdin. ``configctl diff-many`` also reads targets
from non-terminal stdin, keeps first-seen order, removes duplicates, and continues after a failed
target.

Streams and exit status
-----------------------

Controllers do not add headings or progress to stdout. Child stdout and stderr retain their
original destination, and private jobs run in the caller's working directory. Exit meanings are:

.. list-table::
   :header-rows: 1

   * - Exit
     - Meaning
   * - 0
     - Success
   * - 1
     - Operation or verification failure
   * - 2
     - Invalid input or artifact
   * - 3
     - Confirmation or safety rejection
   * - 4
     - Provider or configurator failure
   * - 5
     - Registry revision, lock, or fencing conflict
   * - 6
     - Reconciliation required
   * - 127
     - Required child executable not found

Private jobs
------------

Use ``atlas job list configuration-operations`` or
``atlas job list infrastructure-operations`` to inspect internal artifacts. They are implementation
boundaries for controllers and services, not operator-facing replacements for the controllers.
