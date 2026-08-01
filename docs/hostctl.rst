Managed host lifecycle
======================

The ``host-operations`` release adds ``hostctl`` for creating one managed host as a reviewed,
resumable operation. It keeps durable operation state in Global Registry, delegates Proxmox and
Ansible work to the existing ``operations`` release, and exposes no provider-specific types in its
public artifacts.

Install both first-party releases
---------------------------------

``host-operations`` exposes one command, ``hostctl``. Its ten phase executors are private jobs.
The Proxmox and Ansible adapters invoke the reviewed ``vm-create-*`` and ``config-*`` command
shims, so install both releases into the same Atlas runtime:

.. code-block:: console

   $ atlas release install ./operations
   $ atlas release install ./host-operations
   $ atlas runtime install
   $ atlas release shims
   $ atlas which hostctl
   /opt/atlas/releases/host-operations/1.0.0/commands/hostctl.py

The production adapters in version 1 are ``proxmox`` and ``ansible``. The fake provider,
configurator, readiness checker, and Registry client exist only for contract tests.

Prepare Global Registry and the provisioning checkout
-----------------------------------------------------

The current Global Registry API requires the Resource identity to exist before it accepts an
Operation. Before planning, create an unbound ``compute`` Resource whose lifecycle is ``absent``.
Its key and name must match ``resource.id`` and ``resource.name`` in the host specification. The
provider named by the future Binding, normally ``proxmox``, must also exist in Global Registry.
The deployed Registry must accept an ordered Operation ``lifecyclePath``, a ``binding.replace``
plan whose ``providerResourceId`` is assigned at runtime, and a same-Operation Binding removal
while the Resource is still ``allocated``. These capabilities are provided by Registry migration
``0005_host_lifecycle_operations.sql`` and its corresponding Worker code.

The provisioning checkout must be a readable Git checkout. The target must already be present
under ``hosts`` in ``inventories/<site>/hosts.yml``, and the bootstrap and converge playbooks must
be accepted by ``config-validate``. ``hostctl`` records the checkout commit and dirty state; it
does not run Git commands that change the checkout.

Use a Registry profile containing references to credentials, not credential values. A Cloudflare
Access service-token profile has this form:

.. code-block:: yaml

   schema: atlas.registry-profile/v1
   base_url: https://registry.example.org
   timeout_seconds: 30
   access:
     client_id_ref: env:GLOBAL_REGISTRY_ACCESS_CLIENT_ID
     client_secret_ref: file:/run/secrets/global-registry-access-client-secret

``access`` accepts exactly one of a service-token pair, ``jwt_ref``, or
``development_identity``. Plain HTTP is accepted only for ``localhost`` or ``127.0.0.1``;
``development_identity`` is intended for a Registry development server.

Create a host specification
---------------------------

Version 1 accepts only ``HostCreate``. Relative file paths are resolved from the host
specification's directory; the generated plan stores absolute paths.

.. code-block:: yaml

   schema: atlas.host-spec/v1
   kind: HostCreate

   resource:
     id: host-web01
     name: web01
     site: topmost01
     zone: dmz

   registry:
     profile: registry.yml

   provider:
     adapter: proxmox
     definition: providers/proxmox.yml
     input: hosts/web01.proxmox.yml

   configuration:
     adapter: ansible
     project_root: /srv/provisioning
     target: web01
     bootstrap_playbook: bootstrap
     converge_playbook: site

   readiness:
     address: 10.10.30.21
     ssh_port: 22
     ssh_user: ops
     require_cloud_init: true
     require_guest_agent: true

``resource.id`` uses the same lowercase-letter, digit, and hyphen format as a Global Registry key
and is limited to 128 characters. Host names are lowercase DNS labels; site, zone, and playbook
names use the safe identifiers already accepted by the ``operations`` release. SSH users cannot
begin with an option prefix. ``configuration.target``, ``resource.name``, the inventory host, and
the VM name in the Proxmox input must agree. The Proxmox input's site, address, SSH port, and
guest-agent setting must also agree with the host plan. Unknown fields, unsafe symlink sources,
and plaintext values under secret-like keys are rejected.

Plan and apply one operation
----------------------------

Planning reads the source files, the provisioning Git state, provider preflight results, and the
current Registry Resource. It does not create an Operation, acquire a lock, or mutate a provider:

.. code-block:: console

   $ hostctl plan hosts/web01.yml > web01.plan.json
   $ jq -r .metadata.planId web01.plan.json
   plan-7c4...
   $ hostctl apply web01.plan.json --confirm plan-7c4... > web01.evidence.json

Apply rejects a plan older than 30 minutes. It also rejects a changed source digest, changed Git
commit or dirty state, invalid fingerprint, or a confirmation value that does not exactly match
``metadata.planId``. Repeating apply with the same plan reuses the Registry Operation identified
by ``metadata.idempotencyKey``.

The generated plan records ``provider.resourceType`` (``proxmox.qemu`` for the Proxmox adapter).
Atlas uses that immutable value when it creates the Registry Binding plan; the concrete
``providerResourceId`` is supplied only after allocation returns provider evidence.

Apply executes these phases in order:

.. list-table::
   :header-rows: 1

   * - Phase
     - Concrete action
   * - ``validate``
     - Record successful plan and source validation.
   * - ``reserve``
     - Acquire the Resource lock and fencing token, check the Resource identity, and start the Operation.
   * - ``allocate``
     - Run ``vm-create-apply`` and transition the Registry Resource to ``allocated``.
   * - ``provider-verify``
     - Run ``vm-create-verify`` against the recorded child artifact.
   * - ``bind``
     - Store the provider Resource identity and ownership evidence in the Registry Binding.
   * - ``wait-ready``
     - Check provider state, guest agent when required, TCP, SSH authentication, and cloud-init when required.
   * - ``bootstrap``
     - Run ``config-apply <bootstrap-playbook> <target>`` and transition to ``bootstrapped``.
   * - ``converge``
     - Run ``config-apply <converge-playbook> <target>`` and transition to ``configured``.
   * - ``configuration-verify``
     - Run ``config-check <converge-playbook> <target>``.
   * - ``activate``
     - Transition to ``ready`` and complete the Registry Operation.

The current Registry lifecycle values ``allocated``, ``bootstrapped``, and ``configured`` are
reported by ``hostctl status`` as ``provisioning``; ``ready`` is reported as ``active``. A
Registry Operation with status ``succeeded`` is reported as ``completed``, and a blocked step is
reported as ``needs-reconcile``.

Inspect, resume, and verify
---------------------------

Use a plan path while the plan is available. An Operation ID or Resource key can also resolve the
plan stored in Global Registry; set ``ATLAS_REGISTRY_PROFILE`` for those forms:

.. code-block:: console

   $ hostctl status web01.plan.json
   $ ATLAS_REGISTRY_PROFILE=/etc/atlas/registry.yml hostctl status op-123
   $ hostctl resume web01.plan.json --confirm plan-7c4... > web01.resume.json
   $ ATLAS_REGISTRY_PROFILE=/etc/atlas/registry.yml hostctl verify host-web01

Resume obtains a new lock and fencing token. Blocking provider, readiness, and configuration
actions renew the five-minute lease every two minutes and use the newest fencing token for the
following Registry write. Resume revalidates live state for every succeeded step instead of
blindly replaying it. For a blocked, failed, or interrupted allocation, it observes the provider
before taking another mutation: recoverable ownership evidence continues the existing allocation,
while a new allocation is allowed only when the provider can confirm absence. A changed source or
provisioning checkout is rejected. Provider allocation or rollback timeouts produce
``needs-reconcile`` evidence and exit 6; an uncertain observation remains blocked for operator
review.

``hostctl verify`` checks the recorded provider evidence against live provider state, repeats the
readiness checks, and runs the configuration check. It is read-only and returns exit 1 when any
check fails.

Rollback before configuration starts
-------------------------------------

.. code-block:: console

   $ hostctl rollback web01.plan.json --confirm plan-7c4... > web01.rollback.json

Before allocation, rollback cancels the Operation without touching a provider. After allocation
and before bootstrap, it delegates deletion to ``vm-create-rollback`` and removes an existing
Binding only after deletion succeeds. Provider deletion is refused when the recorded ownership
marker or the original ``vm-create`` evidence is absent.

Once bootstrap, converge, or configuration verification has started, rollback retains the provider
Resource and records ``needs-reconcile``. An Ansible failure or interrupted Ansible child never
permits VM deletion. An allocation with an unconfirmed result and a Resource with a conflicting
Binding are also retained. An active host is retained; retiring it requires a future
``HostRetire`` operation.

The current Global Registry API uses expiring lock leases and has no explicit release endpoint.
On completion, ``hostctl`` discards its local lock-scope record and the server-side lease expires.

Read stdout, stderr, and exit status separately
-----------------------------------------------

Successful command output on stdout is one JSON plan, evidence, status, or verification object.
Phase progress, child command lines, warnings, and diagnostics go to stderr.

.. list-table::
   :header-rows: 1

   * - Exit
     - Meaning
   * - 0
     - The command or verification succeeded.
   * - 1
     - The Operation, verification, or provider rollback failed.
   * - 2
     - Input, plan, or evidence is invalid.
   * - 3
     - Confirmation or another safety rule rejected the mutation.
   * - 4
     - The provider or configurator could not perform the requested action, or Registry authentication failed.
   * - 5
     - Registry revision, lock, fencing-token, or availability handling stopped the phase.
   * - 6
     - Provider outcome is unknown and reconciliation is required.

Artifacts never contain Registry credentials. Child processes receive argument lists with
``shell=False``. Each private phase job receives the same ``ATLAS_OPERATION_ID`` correlation value,
and every completed phase writes evidence to the Registry Operation step.
