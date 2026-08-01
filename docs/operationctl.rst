Operation diagnostics
=====================

``operationctl`` reads operation plans, evidence, and durable Global Registry state. It does not
run a host or image workflow and does not mutate provider resources.

.. code-block:: text

   operationctl validate [ARTIFACT]
   operationctl inspect [ARTIFACT]
   operationctl status OPERATION_ID

``validate`` checks one ``OperationPlan`` or ``OperationEvidence`` and writes a small JSON result.
``inspect`` validates the same artifact and writes operator-readable fields. Omission and ``-`` read
JSON from stdin.

``status`` requires ``ATLAS_REGISTRY_PROFILE``. It reads the operation record by ID and writes its
status, revision, plan, resources, and steps as JSON. Registry credential values are referenced by
the profile and never appear in argv or output.

.. code-block:: console

   $ operationctl validate vm.plan.json
   $ operationctl inspect vm.evidence.json
   $ ATLAS_REGISTRY_PROFILE=/etc/atlas/registry.yml \
       operationctl status op-123 | jq '{id, status, revision}'

Invalid artifacts, a missing profile, or an unknown operation return 2. Registry authentication
returns 4. Revision, lock, availability, or fencing failures return 5. Diagnostics use stderr and
successful results use stdout. Keep the original artifact, correct the reported input or Registry
access problem, and rerun the diagnostic. ``operationctl`` does not repair or resume an operation.

The previous ``operation-artifact-*`` commands have no shims. Replace callers with
``operationctl validate`` or ``operationctl inspect`` as listed in :doc:`command-migration`.
