Hermes and Ares responsibility classification
==============================================

Source checkouts
----------------

This classification compares the Atlas target contract with these tracked
checkouts:

* Hermes ``master`` at ``54d2fed``
* Ares ``codex/ares-cli-v2-redesign`` at ``f5920c1``

The untracked Hermes ``docs/goal.md`` and Ares ``goal.md`` files remain
user-owned inputs. They were inspected for context but are not migration
targets and are not modified.

Classification language
-----------------------

``generic primitive``
   One reusable operation that accepts explicit input and performs one main
   calculation or provider action.

``generic composition``
   Reusable orchestration that invokes public primitive executables rather
   than importing their internal functions.

``environment desired state``
   Values or policy that describe a particular site, host, network, provider
   placement, or intended service state.

``generic backend adapter``
   Code that converts reviewed operation steps into provider API calls without
   selecting site-specific values. A generic backend adapter is Atlas-owned.

``provider definition``
   The endpoint, node, storage, bridge, VLAN, guest specification, image, or
   other environment-selected value supplied to a backend adapter. A provider
   definition belongs in an external infrastructure repository.

``unused/historical``
   A wrapper, duplicate implementation, speculative abstraction, retired
   integration, or product-specific name that is not part of the Atlas target
   contract.

Every class, method, function, and nested function in a file inherits the
file's decision below unless the file has an explicit symbol exception.

An AST inventory of the source commits above found 52 Hermes production
Python files with 237 definitions and 39 Ares production Python files with
289 definitions. Every production file appears below; the inheritance rule
therefore gives every discovered definition a decision.

Hermes production files
-----------------------

No Hermes production module moves into Atlas. The apparently reusable pieces
either encode the unadopted host-manifest schema, duplicate final Atlas
runtime behavior, depend on retired product projections, provide a
product-wide output-format shell, or implement mutations that violate the
Atlas command and subprocess contracts.

The following files encode environment desired state or projection policy.
Keep real desired-state data in its owning external repository, but delete
these Hermes implementations instead of copying the host-manifest framework
into Atlas:

.. code-block:: text

   modules/hermes/atlas/__init__.py
   modules/hermes/atlas/diff.py
   modules/hermes/atlas/project.py
   modules/hermes/daedalus/__init__.py
   modules/hermes/daedalus/diff.py
   modules/hermes/daedalus/project.py
   modules/hermes/inventory/__init__.py
   modules/hermes/inventory/daedalus.py
   modules/hermes/inventory/network.py
   modules/hermes/inventory/site.py
   modules/hermes/manifest/__init__.py
   modules/hermes/manifest/load.py
   modules/hermes/manifest/report.py
   modules/hermes/manifest/schema.py
   modules/hermes/manifest/validate.py

This includes every definition in those files, including the hard-coded
KANAGAWA01 network constants, host-manifest validators, inventory walkers,
projection renderers, diff calculations, and report builders. Atlas already
uses ``atlas_core`` for its runtime context and native Ansible commands for
inventory inspection; it does not adopt a universal host-manifest schema.

The following files are unused or historical integrations. Delete every
definition in them:

.. code-block:: text

   modules/hermes/cataloga/__init__.py
   modules/hermes/cataloga/client.py
   modules/hermes/cataloga/dataset.py
   modules/hermes/cataloga/diff.py
   modules/hermes/cataloga/models.py
   modules/hermes/cataloga/normalize.py
   modules/hermes/cataloga/plan.py
   modules/hermes/cataloga/project.py
   modules/hermes/dns/__init__.py
   modules/hermes/dns/apply.py
   modules/hermes/dns/records.py
   modules/hermes/dns/render.py
   modules/hermes/dns/zone.py
   modules/hermes/proxmox/__init__.py
   modules/hermes/proxmox/client.py
   modules/hermes/proxmox/collect.py
   modules/hermes/proxmox/diff.py
   modules/hermes/proxmox/normalize.py
   modules/hermes/proxmox/plan.py
   modules/hermes/report/__init__.py
   modules/hermes/report/dns.py
   modules/hermes/report/drift.py
   modules/hermes/report/hosts.py
   modules/hermes/report/inventory.py
   modules/hermes/report/projections.py
   modules/hermes/report/summary.py

The Cataloga code targets a retired product projection. The DNS parser is not
an authoritative zone parser, and its apply path accepts shell command
strings, runs them with ``shell=True``, writes hidden backups, and performs
implicit reloads. The Proxmox code duplicates the reviewed-plan behavior
retained from Ares. The report modules compose these obsolete boundaries and
offer the output-format switching prohibited by the target command design.

The following files are the obsolete Hermes product shell or support code.
Delete every definition rather than renaming the umbrella command:

.. code-block:: text

   modules/hermes/__init__.py
   modules/hermes/__main__.py
   modules/hermes/cli.py
   modules/hermes/config.py
   modules/hermes/confirm.py
   modules/hermes/context.py
   modules/hermes/errors.py
   modules/hermes/io.py
   modules/hermes/models.py
   modules/hermes/output.py
   modules/hermes/plan.py

``hermes`` is not retained as a public command or package name.
``modules/hermes/context.py`` duplicates ``atlas_core`` and adds workspace
discovery that the Atlas contract intentionally rejects. ``cli.py`` combines
unrelated read, report, plan, and mutation behavior behind one product command
instead of exposing domain-verb primitives.

Hermes non-production files
---------------------------

Delete the old release wrapper, package metadata, schemas, examples, tests,
and product documentation when the Hermes repository is retired:

.. code-block:: text

   commands/hermes.py
   schemas/dns-zone-plan.v1.schema.json
   schemas/hermes-config.v1.schema.json
   schemas/proxmox-state.v1.schema.json
   schemas/sync-plan.v1.schema.json
   examples/dns-zone-plan.json
   examples/hermes.yml
   examples/proxmox-state.json
   examples/resources.yaml
   tests/
   README.md
   docs/
   VERSION
   pyproject.toml
   requirements.txt
   requirements.lock
   .github/workflows/ci.yml

The files under ``tests/fixtures`` are test-only examples, not production
desired state. No fixture is moved to the provisioning repository.

Ares production files retained by Atlas
---------------------------------------

The Ares repository contains reusable reviewed-operation behavior. Extract
only the files listed here, rename their public schemas and symbols to
Atlas-owned terms, and remove their implicit file writes and product-wide CLI
dispatch.

Artifact parsing and serialization:

.. code-block:: text

   src/ares/artifacts.py
   src/ares/files.py
   src/ares/io.py

Retain ``read_artifact_arg``, artifact kind detection, strict JSON parsing,
digest calculation, and explicit stdout/stderr helpers. Do not retain
Ares-named diagnostics or generic YAML output switching.

Plan, evidence, fingerprint, and safety behavior:

.. code-block:: text

   src/ares/plans/fingerprint.py
   src/ares/plans/models.py
   src/ares/plans/validate.py
   src/ares/evidence/__init__.py
   src/ares/evidence/models.py
   src/ares/safety.py

Every definition in these files is a generic safety policy or its data model.
Move it under ``atlas_operations``. Change the public API version from
``ares.alflag.org/v1`` to ``atlas.operation/v1``. Rename the common Pydantic
base model and all Ares-specific diagnostics. Keep strict unknown-field
rejection, plan fingerprints, plan age checks, exact confirmation, preflight
gating, evidence matching, and created-resource ownership checks.

The ``SafetyGate`` catalog dependency is an exception: do not move the
speculative catalog framework. Validate only the operation kinds implemented
by the first-party release.

Provider-neutral interfaces and the Proxmox backend adapter:

.. code-block:: text

   src/ares/providers/base.py
   src/ares/providers/proxmox/__init__.py
   src/ares/providers/proxmox/client.py
   src/ares/providers/proxmox/cloudinit.py
   src/ares/providers/proxmox/models.py
   src/ares/providers/proxmox/tasks.py

Every definition in these files is a generic backend adapter or support for
one. Move it under ``atlas_operations``. Retain strict permission checks,
task polling, cloud-init argument construction, ownership markers, live-state
verification, and rollback verification. The adapter receives an already
validated provider definition and secret values; it does not discover either.

Reviewed Proxmox operations:

.. code-block:: text

   src/ares/operations/proxmox_vm_create.py
   src/ares/operations/proxmox_vm_template_create.py

Every definition in these files belongs to explicit Atlas command artifacts,
with these final command boundaries:

.. code-block:: text

   proxmox-status
   vm-create-plan
   vm-create-apply
   vm-create-verify
   vm-create-rollback
   vm-template-create-plan
   vm-template-create-apply
   vm-template-create-verify
   vm-template-create-rollback
   operation-artifact-validate
   operation-artifact-inspect

Plan commands emit a plan on stdout. Apply and rollback commands consume a
reviewed artifact and emit evidence on stdout. Verify commands consume a plan
or evidence and emit a verification report. No command silently writes a plan
or evidence directory.

Ares files that require symbol-level splitting
----------------------------------------------

``src/ares/config.py``
   Move the strict Pydantic provider and operation input models,
   ``is_secret_ref``, ``resolve_secret_ref``, and plaintext-secret rejection
   after renaming them to Atlas-owned terms. Do not move ``AresSettings``,
   ``AresConfig``, ``resolve_config_path``, implicit ``./ares.yml`` discovery,
   ``/etc/atlas/ares.yml`` defaults, Daedalus paths, or ``/var/lib/atlas/ares``
   paths. Final commands require an explicit provider-definition path. Actual
   endpoint, node, storage, bridge, VLAN, template, guest, and secret
   references remain in the external infrastructure repository.

``src/ares/errors.py``
   Move the error roles needed by retained plan, safety, provider, and input
   validation behavior. Rename them without the Ares product boundary. Delete
   the inventory and catalog errors with the discarded abstractions.

``src/ares/evidence/io.py``
   Retain strict evidence parsing. Delete ``evidence_path`` and implicit
   ``save_evidence`` calls. Evidence persistence is an explicit caller
   redirection or output-path decision.

``src/ares/plans/io.py``
   Retain strict plan parsing. Delete implicit save behavior.

Ares production files not retained
----------------------------------

Delete every definition in these files:

.. code-block:: text

   src/ares/__init__.py
   src/ares/__main__.py
   src/ares/catalog.py
   src/ares/cli.py
   src/ares/context.py
   src/ares/inventory/__init__.py
   src/ares/inventory/daedalus.py
   src/ares/inventory/models.py
   src/ares/operations/__init__.py
   src/ares/operations/base.py
   src/ares/operations/future.py
   src/ares/operations/noop.py
   src/ares/output.py
   src/ares/plans/__init__.py
   src/ares/providers/__init__.py
   src/ares/providers/disabled.py
   src/ares/providers/noop.py
   src/ares/providers/proxmox/permissions.py

The umbrella ``ares`` CLI and package name are historical. The operation
catalog contains speculative DNS, Cloudflare, SSH, and network entries that
have no implementation. The no-op and disabled providers are scaffolding, not
production capabilities. The Daedalus inventory model couples reusable
provider logic to an obsolete product and a site-selected schema. Final plan
commands accept an explicit, validated operation input file instead of
discovering inventory or host variables.

Ares non-production files
-------------------------

Move and rewrite only tests that exercise retained plan, safety, provider, VM,
template, artifact, and secret-reference behavior. Do not copy repository
packaging, generated distributions, product documentation, old CLI tests,
future-provider documentation, or product-branded examples.

Provider definitions currently shown in ``examples/ares.yml`` belong in the
external infrastructure repository after conversion to the explicit final
input schema. Plaintext secret values never move; only secret references may
appear in that repository.

Implementation order
--------------------

1. Add Atlas-owned plan, evidence, safety, artifact, and Proxmox adapter
   modules with the final ``atlas.operation/v1`` contract.
2. Add one manifest entrypoint for each final command listed above.
3. Port and rewrite only the tests associated with retained behavior.
4. Verify unit, integration-fixture, coverage, lint, package, and release
   artifact checks in Atlas.
5. Convert real provider definitions in their external repository without
   adding an Atlas wrapper or package.
6. Remove or archive Hermes and Ares only after replacement commands pass
   authorized real-host smoke tests and the observation period.

No repository-wide copy, compatibility import, product-name alias, legacy
schema reader, or umbrella command is permitted.
