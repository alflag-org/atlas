Proxmox operations
==================

The ``infrastructure-operations`` release uses reviewed Proxmox jobs behind ``hostctl`` and
``imagectl``. Provider, input, plan, and evidence files remain explicit. JSON artifacts go to
stdout; progress and diagnostics go to stderr.

Files passed to plan commands
-----------------------------

Every plan command requires a provider definition and an operation input. The
provider selects one live Proxmox API and contains secret references, not
secret values:

.. code-block:: yaml

   schema: atlas.provider/v1
   provider: proxmox
   safety:
     require_confirm: true
     max_plan_age_seconds: 1800
     allow_rollback_delete: true
   connection:
     api_url: https://pve.example.org:8006/api2/json
     verify_ssl: true
     token_id_ref: env:PROXMOX_TOKEN_ID
     token_secret_ref: file:/run/secrets/proxmox-token
     task_timeout_seconds: 900
     poll_interval_seconds: 3

``env:NAME`` and ``file:/absolute/path`` are the only secret-reference forms.
Secret files must be regular, non-symlink files. Atlas rejects plaintext values
under secret-looking keys before schema validation.

A VM input supplies the site-specific values:

.. code-block:: yaml

   schema: atlas.operation-input/v1
   kind: ProxmoxVmCreate
   site: example
   target: web01
   create_allowed: true
   rollback_delete_allowed: true
   vm:
     vmid: 121
     name: web01
     node: pve01
     template_vmid: 9000
     template_name: tmpl-ubuntu-cloudinit
     full_clone: true
     pool: zone-dmz
   resources:
     cores: 2
     sockets: 1
     memory_mb: 2048
     disk:
       device: scsi0
       size_gb: 20
       storage: local-lvm
   network:
     bridge: vmbr0
     vlan: 130
     ip: 192.0.2.21
     prefix: 24
     gateway: 192.0.2.1
     dns_servers:
       - 192.0.2.53
   cloud_init:
     user: ops
     ssh_public_keys:
       - ssh-ed25519 AAAA...
     ciupgrade: false
   guest:
     qemu_agent: true
     ssh_port: 22
   tags:
     - zone-dmz
     - role-web

The VM name must be a lowercase DNS label and must equal ``target``. Atlas adds
``managed-atlas`` and ``platform-vm`` tags. VM creation requires the qemu guest agent;
``qemu_agent`` cannot be disabled.

A template input supplies the template creation values:

.. code-block:: yaml

   schema: atlas.operation-input/v1
   kind: ProxmoxVmTemplateCreate
   site: example
   target: tmpl-ubuntu-2404
   create_allowed: true
   rollback_delete_allowed: true
   vmid: 9100
   name: tmpl-ubuntu-2404
   node: pve01
   image:
     url: https://images.example.org/ubuntu-24.04.img
     checksum: sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
     shared_path: /srv/proxmox-images/ubuntu-24.04.img
   resources:
     memory_mb: 1024
     cores: 1
     disk_gb: 10
     storage: local-lvm
     disk_device: scsi0
   network:
     bridge: vmbr0
     vlan: 130
   cloud_init:
     user: ops
   guest:
     qemu_agent: true
     serial_console: true
   tags:
     - os-ubuntu

The image URL must use HTTPS. Use one absolute ``shared_path``, or use
``runner_path`` and ``node_path`` together when the runner and Proxmox node see
the same shared storage under different paths. Atlas downloads a missing image
to a temporary sibling, checks its declared SHA-256 digest, and renames it
atomically. It does not delete an existing file with a different checksum.
Template creation requires both the qemu guest agent and serial console settings.

Controllers and their exact input
---------------------------------

``providerctl status PROVIDER`` reads nodes and VMs without mutation. ``hostctl`` reads provider
and VM input paths from a host specification and owns the durable managed-host lifecycle described
in :doc:`hostctl`.

``imagectl`` takes the provider definition explicitly:

``imagectl plan PROVIDER INPUT``
   Runs local and live Proxmox preflight checks and emits an ``OperationPlan``.

``imagectl apply PROVIDER [PLAN] --confirm PLAN_ID``
   Reads the plan from ``PLAN`` or stdin, repeats preflight, applies its steps, verifies the
   template, and emits ``OperationEvidence``.

``imagectl verify PROVIDER [PLAN_OR_EVIDENCE]``
   Verifies live template state and emits a JSON verification result.

``imagectl rollback PROVIDER [EVIDENCE] --confirm PLAN_ID``
   Verifies the evidence and live resource identity, deletes only the template created by that
   plan, verifies deletion, and emits updated evidence.

``operationctl validate [ARTIFACT]`` checks one plan or evidence artifact without live access.
``operationctl inspect [ARTIFACT]`` prints operator-readable facts. For commands with an optional
artifact, ``-`` and omission both mean stdin.

.. code-block:: bash

   imagectl plan provider.yml image-create.yml > image-plan.json
   plan_id="$(jq -r '.metadata.planId' image-plan.json)"

   imagectl apply \
     provider.yml image-plan.json \
     --confirm "$plan_id" \
     > image-evidence.json

   imagectl verify provider.yml image-evidence.json

   imagectl rollback \
     provider.yml image-evidence.json \
     --confirm "$plan_id" \
     > image-rollback-evidence.json

Files and evidence checked before mutation
------------------------------------------

A plan uses API version ``atlas.operation/v1`` and records absolute paths and
SHA-256 digests for both input files. Apply, verify, and rollback require the
same provider path and reject either source when its bytes have changed.
Fingerprints cover the complete plan except the fingerprint field itself.
Unknown fields, unsupported operation kinds, provider mismatches, stale plans,
future timestamps, failed repeated preflight, and confirmation values other
than the exact plan ID are rejected.

Apply records the completed step IDs and the first created resource even when
a later step fails. Rollback requires that evidence, the embedded plan
snapshot, matching plan and provider identifiers, permission in both the
provider safety policy and operation input, and an exact VMID, node, and name
match. Atlas writes these Proxmox description lines:

.. code-block:: text

   managed-by: atlas
   atlas-plan-id: <plan-id>
   atlas-operation-kind: <operation-kind>
   atlas-target: <target>

A written marker must match before deletion. An unmarked partial clone or
temporary template can be removed only when evidence records that the same
plan created it and the live VMID and name still match.

Exit status and persistence
---------------------------

.. list-table::
   :header-rows: 1

   * - Status
     - Meaning
   * - ``0``
     - The command completed and the reported result passed or succeeded.
   * - ``1``
     - Apply, verify, or rollback ran and reported a failed result.
   * - ``2``
     - An input, schema, artifact, fingerprint, or source binding is invalid.
   * - ``3``
     - A mutation safety check refused apply or rollback.
   * - ``4``
     - The live provider could not perform the requested operation.

Redirect stdout to persist a plan or evidence file. Atlas does not choose an
output path or write an implicit operation-state directory. Artifacts contain
target topology and step results, but no resolved secret values. Protect them
as operational records.
