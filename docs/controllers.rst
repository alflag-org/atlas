First-party controllers
=======================

Install the bundled operations release and build the shared runtime:

.. code-block:: bash

   atlas release install /srv/atlas/source/operations
   atlas runtime install
   atlas command list

The public commands are ``atlas-ansible``, ``hostctl``, and ``imagectl``. Provider validation,
artifact validation, and lifecycle phases run at their consuming boundaries or as private jobs;
they have no separate PATH entry.

.. warning::

   The domains under ``example.org``, addresses in ``192.0.2.0/24``, resource identifiers, paths,
   user names, and SSH key below are examples. Replace every value and verify the resulting plan
   before applying it. The documented endpoints and address range do not identify live services.

Each controller parses a fixed set of subcommands and invokes a private job through
``atlas job run``. ``atlas-ansible diff-many`` invokes the private diff job once per target. Child
processes receive an argument list with ``shell=False`` and inherit the working directory,
environment, and streams. stdout contains results; diagnostics use stderr. Controllers return the
child status unchanged, and a missing child returns 127.

Run Ansible with Atlas
----------------------

``atlas-ansible`` operates on the Ansible project in the current directory:

.. code-block:: text

   atlas-ansible check PLAYBOOK TARGET
   atlas-ansible diff PLAYBOOK TARGET
   atlas-ansible diff-many PLAYBOOK [TARGET ...]
   atlas-ansible apply PLAYBOOK TARGET
   atlas-ansible inventory

``PLAYBOOK`` is a basename resolved as ``playbooks/PLAYBOOK.yml``. ``TARGET`` becomes one Ansible
``--limit`` value. The project root must contain a regular, non-symlink ``ansible.cfg``.

.. list-table::
   :header-rows: 1

   * - Command
     - Native process
   * - ``check site web-01``
     - ``ansible-playbook playbooks/site.yml --limit web-01 --check``
   * - ``diff site web-01``
     - ``ansible-playbook playbooks/site.yml --limit web-01 --check --diff``
   * - ``apply site web-01``
     - ``ansible-playbook playbooks/site.yml --limit web-01``
   * - ``inventory``
     - ``ansible-inventory --graph``

Atlas sets ``ANSIBLE_CONFIG`` to the checked project file. ``diff-many`` reads targets from argv and
non-terminal stdin, removes duplicates in first-seen order, runs every target, and returns the
first non-zero status. It stores no operation state. Source lint and standalone syntax validation
belong to the Ansible repository's checks; ``atlas-ansible`` is the audited execution boundary for
read-only and mutating Ansible runs.

Prepare Proxmox inputs
----------------------

``hostctl`` and ``imagectl`` use a strict ``atlas.provider/v1`` definition. Credentials must use
``env:NAME`` or ``file:/absolute/path`` references; plaintext secrets and symlinked secret files are
rejected.

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

A ``ProxmoxVmCreate`` input describes one managed VM:

.. code-block:: yaml

   schema: atlas.operation-input/v1
   kind: ProxmoxVmCreate
   site: site-a
   target: web-01
   create_allowed: true
   rollback_delete_allowed: true
   vm:
     vmid: 121
     name: web-01
     node: pve-01
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

The VM name must equal ``target`` and use a lowercase DNS label. VM creation requires the qemu guest
agent.

A ``ProxmoxVmTemplateCreate`` input describes one machine image:

.. code-block:: yaml

   schema: atlas.operation-input/v1
   kind: ProxmoxVmTemplateCreate
   site: site-a
   target: tmpl-ubuntu-2404
   create_allowed: true
   rollback_delete_allowed: true
   vmid: 9100
   name: tmpl-ubuntu-2404
   node: pve-01
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

Image URLs must use HTTPS. Use ``shared_path``, or use ``runner_path`` and ``node_path`` together when
the runner and Proxmox node have different paths to shared storage. Atlas verifies the declared
SHA-256 before making the image available.

Run a managed-host lifecycle with hostctl
-----------------------------------------

Global Registry must contain an unbound ``compute`` Resource with lifecycle ``absent``. Its key and
name must match the host specification, and the named provider must exist. The Registry must
accept an ordered Operation ``lifecyclePath``, ``binding.replace`` with a provider resource ID assigned
during the operation, and Binding removal by the same Operation while the Resource is
``allocated``.

The provisioning project must be a readable Git checkout. Its inventory already contains the
target, and both playbooks must pass the private ``ansible-syntax-check`` job. Atlas records the
commit and dirty state without changing the checkout.

Use a Registry profile that contains credential references:

.. code-block:: yaml

   schema: atlas.registry-profile/v1
   base_url: https://registry.example.org
   timeout_seconds: 30
   access:
     client_id_ref: env:GLOBAL_REGISTRY_ACCESS_CLIENT_ID
     client_secret_ref: file:/run/secrets/global-registry-access-client-secret

``access`` accepts one service-token pair, ``jwt_ref``, or ``development_identity``. Plain HTTP is
limited to localhost; ``development_identity`` is limited to Registry development servers.

``atlas.host-spec/v1`` accepts ``HostCreate``:

.. code-block:: yaml

   schema: atlas.host-spec/v1
   kind: HostCreate

   resource:
     id: host-web-01
     name: web-01
     site: site-a
     zone: dmz

   registry:
     profile: registry.yml

   provider:
     adapter: proxmox
     definition: providers/proxmox.yml
     input: hosts/web-01.proxmox.yml

   configuration:
     adapter: ansible
     project_root: /srv/provisioning
     target: web-01
     bootstrap_playbook: bootstrap
     converge_playbook: site

   readiness:
     address: 192.0.2.21
     ssh_port: 22
     ssh_user: ops
     require_cloud_init: true
     require_guest_agent: true

Relative paths resolve from the specification directory; a plan stores absolute paths and source
digests. Resource name, configuration target, inventory host, VM name, site, address, SSH port,
and guest-agent setting must agree across inputs.

.. code-block:: bash

   hostctl plan hosts/web-01.yml > web-01.plan.json
   plan_id="$(jq -r .metadata.planId web-01.plan.json)"
   hostctl apply web-01.plan.json --confirm "$plan_id" > web-01.evidence.json

   hostctl status web-01.plan.json
   hostctl resume web-01.plan.json --confirm "$plan_id" > web-01.resume.json
   ATLAS_REGISTRY_PROFILE=/etc/atlas/registry.yml hostctl verify host-web-01
   hostctl rollback web-01.plan.json --confirm "$plan_id" > web-01.rollback.json

Apply runs ``validate``, ``reserve``, ``allocate``, ``provider-verify``, ``bind``, ``wait-ready``,
``bootstrap``, ``converge``, ``configuration-verify``, and ``activate`` in that order. Repeating apply
with the same plan reuses the Registry Operation identified by ``metadata.idempotencyKey``.

Apply requires the exact plan ID and rejects plans older than 30 minutes, changed source bytes,
changed Git state, or an invalid fingerprint. Resume obtains a new lock and fencing token, checks
live state for completed phases, and observes an uncertain allocation before another mutation.
Provider allocation or rollback timeouts require reconciliation and return 6.

Rollback may cancel an operation before allocation. After allocation and before configuration, it
deletes only a VM whose recorded evidence and live ownership marker match, then removes its
Binding. Once bootstrap begins, rollback retains the VM and records that reconciliation is
required. An active host is also retained.

Run an image lifecycle with imagectl
------------------------------------

``imagectl`` is the provider-independent Atlas image lifecycle boundary. The ``operations``
release implements this boundary with an internal Proxmox adapter and VM-template jobs.

.. code-block:: text

   imagectl plan PROVIDER INPUT
   imagectl apply PROVIDER [PLAN] --confirm PLAN_ID
   imagectl verify PROVIDER [PLAN_OR_EVIDENCE]
   imagectl rollback PROVIDER [EVIDENCE] --confirm PLAN_ID

An omitted optional artifact and ``-`` read stdin. ``imagectl`` writes plans, evidence, and
verification results to stdout. Plan generation validates the provider definition before use.
Apply, verify, and rollback validate the supplied artifact at the boundary. Durable image status
and resume operations are not exposed until Registry-owned image operation state exists.

Keep operation artifacts
------------------------

An ``atlas.operation/v1`` plan records absolute source paths and SHA-256 digests. Apply, verify, and
rollback reject changed sources, provider mismatches, stale or future timestamps, invalid
fingerprints, failed live preflight, and an inexact confirmation value.

Evidence records completed steps and the first created resource even when a later step fails.
Rollback requires evidence from the same plan, permission in both safety policies, and a live
VMID, node, name, and ownership-marker match. Atlas writes these Proxmox description lines:

.. code-block:: text

   managed-by: atlas
   atlas-plan-id: <plan-id>
   atlas-operation-kind: <operation-kind>
   atlas-target: <target>

Artifacts contain topology and step results but no resolved credentials. Store them as operational
records.

Read controller exit status
---------------------------

.. list-table::
   :header-rows: 1

   * - Exit
     - Meaning
   * - 0
     - The command succeeded.
   * - 1
     - An operation or verification failed.
   * - 2
     - Input, plan, evidence, or required local state is invalid.
   * - 3
     - Confirmation or another mutation safety rule rejected the request.
   * - 4
     - A provider, configurator, or Registry authentication request failed.
   * - 5
     - Registry revision, lock, fencing, or availability handling stopped the request.
   * - 6
     - Provider outcome is unknown and requires reconciliation.
   * - 127
     - A required child executable was not found.
