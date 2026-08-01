Provider diagnostics
====================

``providerctl`` validates provider definitions and reads provider status. It never creates,
deletes, resizes, migrates, or rolls back a resource.

.. code-block:: text

   providerctl validate PROVIDER
   providerctl status PROVIDER

``validate`` parses the strict ``atlas.provider/v1`` schema and emits JSON containing the schema,
provider kind, and ``valid`` result. It does not contact the provider. ``status`` validates the same
file, resolves its credential references, reads Proxmox nodes and VMs, and emits provider JSON.

.. code-block:: console

   $ providerctl validate providers/proxmox.yml
   {"provider":"proxmox","schema":"atlas.provider/v1","valid":true}
   $ providerctl status providers/proxmox.yml | jq .provider
   "proxmox"

Provider definitions may contain ``env:NAME`` or ``file:/absolute/path`` references. Plaintext
secret values and symlinked files are rejected. Resolved credentials are not written to stdout,
stderr, artifacts, or Atlas run arguments.

Invalid input returns 2. A provider read failure returns 4. The output from a successful diagnostic
uses stdout; diagnostics use stderr. Correct the definition or credential reference and rerun the
diagnostic; ``providerctl`` stores no state and never attempts provider recovery.

The previous ``proxmox-status`` command has no shim. Replace callers with ``providerctl status``
as listed in :doc:`command-migration`.
