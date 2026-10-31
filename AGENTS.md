# Atlas Repository Guidance

## Scope and ownership

This file supplements the global repository guidance with Atlas's execution and SDK contracts.

- Atlas registers locally present programs, selects Python interpreters, creates per-program
  virtual environments, discovers commands, generates shims, and records completed executions.
- Host-owned `/etc/atlas/config.yml` registers programs. Python commands are files under
  `commands/**/*.py`; native commands are executables under `commands/` or `bin/`. Duplicate
  command names fail closed. Keep discovery and shims consistent across all registered programs.
- Program owners manage source history, dependencies, inventory, desired state, infrastructure
  mutations, and secrets. Atlas must not fetch, reset, publish, or version those programs.
- Programs use `atlas_core` as their supported Python API, not private modules under `atlas`.
  Programs and the executing OS account are trusted; these APIs are not a same-UID sandbox.
- Atlas provides shared mechanisms that applications need; it does not manage application
  workflows, resource ownership, or business logic.
- Atlas does not switch users, invoke `sudo`, or grant privileges. Do not add implicit plugin
  discovery or import application code merely to list commands.

## Execution compatibility

- `atlas run` and generated shims use the same executor. Preserve exact argument vectors without
  shell interpretation, caller working directory, child-only environment, stdout/stderr separation,
  exit status, timeout behavior, signal forwarding, and descendant termination.
- Timeout returns `124`, a missing executable `127`, and signal termination `128 + signal`.
  A test killing the Atlas parent itself must assert its negative subprocess signal return code.
- Preserve `ATLAS_CONTEXT_FILE`, host identity, `run_id`, `parent_run_id`, and `operation_id`.
  Nested execution changes the parent identifier while retaining the operation identifier.
- Keep run records limited to completed-command metadata. Never add arguments, environment values,
  resolved secrets, or captured application output to JSONL records.
- Preserve owner-only tmpfs execution storage, process-group registration before command startup,
  nested subtree shutdown, and cleanup after descendants stop. Programs using this storage must
  stay in the inherited process group. SIGKILL and host failure cannot guarantee cleanup.
- Do not infer immutable source selection: registered program directories are operator-managed
  mutable trees. Runtime registration and venv creation do not install program dependencies.

## Independent daemons

- `atlas_core.daemon` is a foreground async SDK for independently runnable applications. It must
  work without Atlas configuration, Atlas CLI execution, systemd, or Cluster dependencies.
- Keep lifecycle, explicit readiness, signals, shutdown, paths, and logging small and typed.
  Use standard asyncio task supervision; do not build a generic supervisor or scheduler.
- Daemon application state and resources belong to the application. The OS service manager owns
  process monitoring and hard termination. Never double-fork, escalate privileges, or start other
  services implicitly.
- Preserve the single-shot Executor contract independently of the SDK. Daemon lifecycle must not
  generate a never-ending Atlas run record or require an execution context.
- Additional management, IPC, or Cluster interfaces require separate reviewable changes. Do not
  turn `atlasd` into a shared supervisor or couple generic SDK imports to a Cluster implementation.

## Cluster membership

- Cluster provides membership, discovery, and host metadata through `atlas_core.cluster`.
  Applications own target selection, execution order, locks, retries, verification, and results.
- The registry stores host information under an explicitly authorized OS account. Remote access
  uses existing SSH host keys and user authentication. Do not add a separate CA, TLS listener,
  invitation system, execution coordinator, workflow DSL, or distributed reservation.
- Cached membership remains readable during registry outages. Preserve explicit freshness and
  retrieval timestamps; membership status is not application health or liveness.
- Cluster does not require a daemon. Keep the Daemon SDK independent of membership storage and
  transport. Host metadata is descriptive and must not be treated as authentication evidence.

## Public interfaces and documentation

- Before adding a public command, identify the initiator, caller, inputs, result, mutations, state
  owner, and error contract. Prefer a library for implementation reuse. Do not add aliases or
  compatibility wrappers without a verified caller, bounded need, and removal condition.
- Before removing a command, inspect documented and external consumers. Repository search alone
  does not prove there are no external callers. Report required migration for breaking changes.
- Keep `README.md` concise. `docs/reference.rst` owns operator behavior; `docs/api.rst` owns public
  Python API documentation. Extend these sources instead of adding design histories or progress
  documents. Documentation must describe implemented behavior.
- Use generic examples and reserved domains or address ranges. Never commit production inventory,
  private endpoints, credentials, personal paths, or machine-local state.
- Keep credentials as references. Do not include resolved values or provider error bodies in
  diagnostics. Application output is outside the framework's redaction boundary.

## Review and verification

- Run targeted regressions first, then `mise run check`: Ruff, 100% line and branch coverage,
  and package builds. Do not relax checks or add meaningless assertions to meet coverage.
- Build documentation with `make clean-docs html SPHINXOPTS=-W`. Test Python 3.11 through 3.14
  when changing core runtime, imports, SDKs, or process behavior.
- For public changes verify API/CLI contracts, parsing, stdout/stderr, exit codes, examples,
  documentation, and affected packaging. Exercise discovery and shims across all programs.
- Run container build and runtime smoke checks when installation, packaging, launcher, or host
  layout changes. Inspect distribution contents when package artifacts change.
- Test high-risk process changes with real signal, timeout, task failure, parent termination,
  and cleanup scenarios. Independently review a frozen clean commit for high-risk runtime or
  process changes; verify base, HEAD, ancestry, worktree state, and regression evidence first.
- Validate workflow syntax and run `actionlint` when changing GitHub workflows.
- Missing tools, blocked fetches, unavailable Docker/systemd, and offline services mean unverified,
  not passed. Report the evidence; do not change host configuration to hide a limitation.
- Use the pull request template's `Problem`, `Changes`, and `Impact` sections. Separate unrelated
  changes and explain operational, compatibility, and deployment consequences.
