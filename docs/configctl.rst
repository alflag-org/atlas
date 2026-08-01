Configuration controller
========================

``configctl`` operates on the Ansible project in the current working directory. It does not clone
or update the project, install dependencies, create infrastructure resources, or mutate Global
Registry.

Syntax
------

.. code-block:: text

   configctl validate PLAYBOOK
   configctl check PLAYBOOK TARGET
   configctl diff PLAYBOOK TARGET
   configctl diff-many PLAYBOOK [TARGET ...]
   configctl apply PLAYBOOK TARGET
   configctl inventory

``PLAYBOOK`` is a safe basename resolved as ``playbooks/PLAYBOOK.yml``. ``TARGET`` is passed as one
Ansible ``--limit`` argument after an empty-value check. The project must contain a regular,
non-symlink ``ansible.cfg`` at its root.

Native argv
-----------

.. list-table::
   :header-rows: 1

   * - Subcommand
     - Native process
   * - ``validate site``
     - ``ansible-playbook playbooks/site.yml --syntax-check``
   * - ``check site web01``
     - ``ansible-playbook playbooks/site.yml --limit web01 --check``
   * - ``diff site web01``
     - ``ansible-playbook playbooks/site.yml --limit web01 --check --diff``
   * - ``apply site web01``
     - ``ansible-playbook playbooks/site.yml --limit web01``
   * - ``inventory``
     - ``ansible-inventory --graph``

Atlas sets ``ANSIBLE_CONFIG`` to the validated project file. Other environment values, streams,
and the working directory are inherited.

Use more than one target
------------------------

``diff-many`` accepts targets in argv and from non-terminal stdin. It removes empty values and
duplicates while keeping first-seen order, then starts ``configctl diff`` once per target. It runs
every target and returns the first non-zero status.

.. code-block:: console

   $ configctl diff-many site web01 web02
   $ printf '%s\n' web01 web02 | configctl diff-many site

Each target heading goes to stderr. Diff output remains on stdout.

Failures and security
---------------------

Invalid project paths or arguments return 2. A missing Atlas or native executable returns 127.
Ansible's status is otherwise returned unchanged. The controller does not accept native option
passthrough, follow symlinked project files, or print environment values. Correct the project,
inventory, playbook, or target and rerun the same command; ``configctl`` stores no recovery state.

Previous configuration command names have no shims. Replace them as listed in
:doc:`command-migration`.
