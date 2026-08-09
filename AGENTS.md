# Atlas Repository Guidance

## Scope

This file applies to the entire Atlas repository. It supplements the global agent guidance with
Atlas-specific architecture, release, execution, controller, job, service, and verification rules.

## Atlas core boundary

The Atlas core boundary is the ownership line between Atlas runtime mechanics and external
infrastructure state.

- Atlas installs and executes versioned release artifacts on a host. It owns release discovery,
  validation, runtime construction, activation, execution, logging, locks, and host artifacts.
- Infrastructure repositories own desired state, inventory, playbooks, provider configuration,
  release source history, and secrets. Atlas may read their Git state and execute their artifacts,
  but it must not pull, reset, rewrite, or otherwise manage those repositories.
- Keep operator-managed release sources separate from installed snapshots. Never use
  `$ATLAS_HOME/releases` or `$ATLAS_HOME/current` as a release source.
- `release.yml` is the only executable discovery mechanism. Undeclared Python files are libraries,
  not implicitly executable commands or jobs.
- Release code uses `atlas_core` as its supported runtime API. Do not couple release artifacts to
  private modules under `atlas`.
- Release code and the Atlas operating-system account are trusted first-party components. Path,
  digest, and import checks protect release-selection correctness; they are not a hostile same-UID
  sandbox. Do not describe them as one.

## Release transactions and generation lifetime

A release transaction is the all-or-restored activation of runtime, release, and host-artifact
selections. A generation selection is the concrete immutable runtime and host-artifact pair pinned
for one execution tree.

- Validate a source, copy it into a symlink-safe staged snapshot, revalidate the staged bytes, and
  activate that exact snapshot. Never validate one tree and later copy or activate mutable source
  bytes.
- Installed release snapshots, Python runtime generations, and host-artifact generations are
  immutable after publication. Mutable links select them; do not update a published generation in
  place.
- Build a clean candidate runtime for the complete intended active release set. Exclude ambient
  virtual environments, user site packages, caller `PYTHONPATH`, pip configuration, and unrelated
  releases. Install dependencies, run `pip check`, and validate staged targets with the candidate
  interpreter before changing active selections.
- Treat runtime publication, requested release-link changes, host-artifact publication, and stable
  launcher updates as one transaction. A failure must restore the exact pre-transaction mutable
  selections and launcher bytes.
- Rollback may clean only candidates created by its own transaction and only when lease state says
  they are unused. Never delete or rewrite a pre-existing generation, installed snapshot, or lease
  as part of rollback.
- Garbage collection is lease-aware and best-effort. When ownership or lease state is uncertain,
  retain the generation for a later pass instead of risking a live execution.
- Preserve lock ordering: acquire the global host-artifact lock before per-release locks, then
  acquire multiple release locks in sorted manifest-name order. Analyze every lock-scope change for
  both deadlock and selection races.
- Keep empty update paths side-effect free. When no release is selected, do not provision a runtime,
  begin a release transaction, acquire publication locks, or refresh host artifacts.
- Resolve the release snapshot and generation selection before spawning. Nested private jobs inherit
  the parent's snapshot and generations; they must not resolve current links again.
- Keep parent leases continuously held across `exec` until the child has acquired and acknowledged
  its own runtime and artifact leases. Preserve this invariant across timeout, spawn failure,
  acknowledgement failure, and intentional or external parent termination.

## Release targets and process execution

The release target contract is the source, callable, and signature Atlas accepts and later invokes.

- Manifest targets use `package.module:callable` and point directly to a stable module-level
  callable. The callable is synchronous, accepts `argv` as its first positional argument, has no
  other required arguments, and returns `int` or `None`.
- Do not use dynamic module-namespace mutation, import ambiguity, callable rebinding, or wrapper
  aliases to evade the target contract. If a callable cannot satisfy the contract directly, keep it
  behind a compliant entry point or as a library.
- Candidate validation and runtime execution must use the same selected-release source resolver and
  the same callable validator. Runtime invokes the actual object that was validated; a separate AST
  approximation is not sufficient.
- Verify snapshot provenance and root-pin the target inside the selected release before importing
  it. An ambient module with the same name must never execute before wrong-origin rejection.
- The Atlas parent process must not import release code. Validate in a separate child using the
  candidate interpreter and the same sanitized import boundary used at runtime.
- Installation imports manifest targets without invoking them. Keep release module top-level code
  free of infrastructure mutation, network access, credential reads, and other operational side
  effects.
- Commands and jobs share the executor. Preserve exact argument vectors with `shell=False`, caller
  working directory, child-only environment, stdout and stderr separation, child exit status,
  timeout behavior, signal forwarding, descendant termination, run logs, and nested correlation.
- Preserve established exit meanings: timeout is `124`, non-blocking job-lock conflict is `75`, a
  missing child is `127`, and a child terminated by a signal returns `128 + signal number`. A test
  that intentionally kills the Atlas parent itself must assert the parent's negative signal return
  code instead of applying the child-status conversion.
- Every run keeps `run_id`, `parent_run_id`, and `operation_id`; nested jobs change the parent while
  retaining the operation. Do not add an execution path that loses this lineage.
- Test manifest or runner changes through install, activation, and actual execution. Unit validation
  alone cannot prove source provenance, import isolation, candidate dependency availability, or
  callable equivalence.

## Public commands and private jobs

In Atlas, a public command is a release-manifest `commands` entry that produces a shim and becomes
a supported interface for people or external automation. A private job is a `jobs` entry invoked
through Atlas by a controller, job instance, scheduler, or service without a public shim.

Before adding or retaining a public command:

- Enumerate the concrete use cases first. For each use case, identify its initiator, direct caller,
  resource or outcome, inputs, completion result, mutations, state owner, and error contract.
- Publish a command only when people or external automation must invoke that use case directly and
  independently. Reuse by another command is not sufficient.
- Put an operation under an existing resource controller when it shares that controller's resource,
  lifecycle, state, safety rules, evidence, or recovery model. Do not create another top-level
  command for the same lifecycle.
- Keep implementation steps in a library or private job when their callers are controllers,
  schedulers, job instances, or services. Process isolation, retries, locking, logging, timeouts,
  and run correlation do not by themselves justify a public command.
- Model scheduled observations and background maintenance as private jobs, job instances, and
  services unless direct ad hoc use has its own supported contract.
- Do not expose a command named after Ansible, a provider, an adapter, a transport, or an internal
  phase unless that object is itself the intended user-facing resource boundary.
- Do not expose an alternate mutating path that bypasses the authoritative controller's planning,
  confirmation, authorization, state transitions, locks, evidence, recovery, or rollback rules.
- Default to a private library or job when no independent direct caller is established.

Apply these Atlas-specific ownership defaults unless the requested product boundary explicitly
changes:

- `hostctl` owns host provisioning and host configuration mutations that belong to the managed-host
  lifecycle.
- `imagectl` owns the provider-independent machine-image lifecycle.
- Periodic host-configuration drift detection belongs to a private job invoked by a job instance,
  scheduler, or service. Scheduling does not make it a public command.
- Ansible and provider-specific operations remain internal adapters or jobs unless a separate,
  independently used public contract is demonstrated before implementation.

## Controller mutation safety

The controller safety contract is the set of checks and records required before, during, and after
an infrastructure mutation.

- Plan generation is mutation-free. Mutation and recovery subcommands consume validated plan or
  evidence artifacts. Live status or verification may query authoritative state by resource
  identity, but must not reconstruct mutation intent from unchecked ad hoc arguments.
- Bind a mutation to an exact confirmation value, plan fingerprint, source digest, provider
  identity, Git state when applicable, freshness window, and live preflight. Reject changed or
  ambiguous inputs before the first mutation.
- Keep credentials as `env:` or absolute `file:` references. Reject plaintext credentials and
  symlinked secret files. Plans, evidence, stdout, run records, and logs must not contain resolved
  secret values.
- Use Registry idempotency, revisions, locks, and fencing as authoritative durable coordination for
  managed-host operations. Do not invent local status or resume state when the owning durable state
  does not exist.
- A timeout or interrupted provider call can have an unknown outcome. Stop further mutation, record
  evidence, return the reconciliation status, and observe live state before retrying. Never convert
  an unknown outcome into an assumed failure and repeat the mutation blindly.
- Evidence records completed steps and the first created resource even when a later phase fails.
  Preserve partial evidence; it is required for recovery and safe rollback.
- Rollback is phase-bounded and ownership-checked. Delete provider resources only when the plan,
  evidence, live identity, and Atlas ownership marker all match and both safety policies permit it.
  Once host bootstrap has begun, retain the host and require reconciliation rather than deleting it
  automatically.
- Keep provider and configurator implementations behind their adapter boundaries. Controllers own
  lifecycle ordering and safety; adapters perform provider-specific or configuration-specific work
  without becoming alternate lifecycle authorities.
- Preserve controller stdout as machine-readable result data, stderr as diagnostics, and the defined
  controller exit-status meanings. Public automation depends on all three, not only successful
  output.

## Jobs, services, and privilege boundaries

- Use a job instance to bind a private job to its user, absolute working directory, arguments,
  environment files, timeout, and lock. Scheduled work should call the job instance rather than
  duplicate those settings in a public command or unit file.
- Atlas does not switch users, invoke `sudo`, or grant privileges. Direct execution must fail when a
  job instance declares a user different from the caller.
- Environment-file values are child-only inputs and are not execution-record fields. Preserve
  argument redaction and do not add secret material to run metadata.
- Managed systemd units use the stable `/opt/atlas/bin/atlas` launcher and either a manifest command
  or a matching job instance. Never point `ExecStart` at a versioned release snapshot.
- A job-backed unit's `User=` and job-instance user must match. Change them together and test the
  mismatch rejection.
- `atlas systemd install` and `remove` manage unit files and run `daemon-reload`; they do not
  enable, start, stop, or restart services. Keep those lifecycle actions explicit operator
  decisions.

## Compatibility and documentation

- Do not add wrapper command trees, public aliases, manifest fallbacks, or compatibility shims by
  default. A compatibility surface requires a verified current caller, a bounded migration need,
  and an explicit removal condition.
- When an intentional breaking change is approved, identify known callers and update them when they
  are in scope; otherwise report the required migration explicitly. Keep package and release
  versions, release archives, command snapshots, CI, container smoke checks, and operator
  documentation coherent with the new contract. Describe migration impact in the pull request
  instead of preserving permanent historical documentation.
- Keep documentation implementation-backed and current-facing. `README.md` is the concise entry
  point, `docs/reference.rst` owns Atlas runtime and operator behavior, and
  `docs/controllers.rst` owns first-party controller contracts. Extend these sources instead of
  adding overlapping design, ADR, migration-history, or temporary process documents.
- Use generic examples and reserved domains or address ranges. Never copy production inventory,
  internal endpoints, credentials, personal paths, or machine-local runtime state into tests or
  documentation.

## Review and verification

- Before implementation, show the use-case-to-interface mapping and explain why an existing
  controller, subcommand, private job, or library cannot own each proposed public command.
- Before removing or privatizing an existing command, inspect documented and external consumers and
  migration requirements. Repository search or missing local run records alone do not prove that
  there are no external callers.
- For a public-surface change, verify `release.yml`, generated command shims or command snapshots,
  help and argument parsing, stdout and stderr, exit status, documentation, examples, container
  smoke checks, and CI or release packaging affected by the command set.
- Run the narrowest relevant regressions first, then `mise run check`. The repository completion
  gate includes Ruff, 100% line and branch coverage, and package builds.
- Build the documentation with `make clean-docs html SPHINXOPTS=-W`. Run supported Python 3.11
  through 3.14 checks when core runtime, import, manifest, or process behavior changes.
- When command discovery or release contents change, smoke the installed command set across every
  active release. Do not compare only the first-party release and overlook example or companion
  commands.
- Run container build and runtime smoke checks when installation, packaging, launcher, release, or
  host-layout behavior changes. Validate release-archive contents when packaging changes.
- Add failure-injection regressions for high-risk transaction or execution changes. Cover the
  relevant source replacement, ambient import collision, validation-before-activation, rollback,
  live-child generation, parent termination, lease handoff, lock contention, and no-op paths.
- For high-risk release, runtime, rollback, process, or lease-lifetime changes, independently review
  a frozen clean commit before calling it ready. Verify the exact base, HEAD, ancestry, worktree
  state, and targeted regression results first.
- Validate workflow syntax and run `actionlint` when GitHub workflow files change.
- A blocked dependency fetch, unavailable Docker socket, missing tool version, or offline external
  system is an unverified check, not a pass. Do not change host configuration to conceal the limit;
  report the exact evidence and remaining risk.
- Use the pull request template's `Problem`, `Changes`, and `Impact` sections. State operational,
  compatibility, and deployment consequences, and keep unrelated concerns in separate changes.
