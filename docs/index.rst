Atlas
=====

Atlas installs and executes commands, jobs, and systemd files declared by versioned releases. It
does not own infrastructure repositories or secrets. Each operation records its execution context
and the Git state of its working directory when available.

Start with :doc:`reference` to install and configure a host. Read :doc:`controllers` before using
the bundled Ansible, Proxmox, or Global Registry integrations. Release authors can use :doc:`api`
to read host and execution context from Python code.

The documentation uses reserved domains, documentation IP address ranges, and fictional resource
names. Replace every example value before running an operation against real infrastructure.

.. toctree::
   :maxdepth: 2

   reference
   controllers
   api
