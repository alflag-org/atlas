Machine image controller
========================

``imagectl`` owns machine-image plan, apply, verify, and rollback. The current provider job creates
reviewed Proxmox templates from ``ProxmoxVmTemplateCreate`` input. Host configuration and provider
administration are outside this controller.

Syntax
------

.. code-block:: text

   imagectl plan PROVIDER INPUT
   imagectl apply PROVIDER [PLAN] --confirm PLAN_ID
   imagectl status TARGET
   imagectl resume TARGET --confirm PLAN_ID
   imagectl verify PROVIDER [PLAN_OR_EVIDENCE]
   imagectl rollback PROVIDER [EVIDENCE] --confirm PLAN_ID

An omitted optional artifact and ``-`` both read stdin. Plan and evidence JSON use stdout. Progress,
validation failures, and provider diagnostics use stderr.

Durable image operation state is not present in the current Registry contract. ``status`` and
``resume`` therefore return exit 2 with an explicit diagnostic. They never report success or write
local substitute state. Add those operations only when Registry owns the image lifecycle state.

Safety boundary
---------------

Apply requires the exact plan ID, a fresh plan, unchanged provider and input digests, and passing
preflight. Rollback requires evidence from the same plan, permission in both safety policies, and a
matching live ownership marker. It refuses to delete an unbound or differently identified
template. See :doc:`proxmox` for the provider definition, input schema, and evidence checks.

Exit 0 means the requested phase succeeded. Exit 1 reports a failed operation or verification, 2
an invalid artifact, 3 a confirmation or safety rejection, and 4 a provider failure. Private
provider jobs keep those values unchanged. Retain the plan and evidence after a failure. Correct
the reported input, provider, or safety condition, then run ``verify`` before deciding whether to
retry ``apply`` or use ``rollback``.

The previous ``vm-template-create-*`` commands have no shims. Replace direct callers with
``imagectl`` as listed in :doc:`command-migration`.
