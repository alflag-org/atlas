Usage
=====

Atlas provides the ``atlas`` command for installing and running script releases.

Common commands
---------------

.. code-block:: bash

   atlas status
   atlas runtime status
   atlas runtime install
   atlas scripts install examples/basic-scripts-release --name sample
   atlas scripts update
   atlas scripts list --verbose
   atlas scripts shims
   atlas which sample
   atlas run sample hello --name=takuya

Runtime installation
--------------------

``atlas runtime install`` uses the configured Python version from ``/etc/atlas/config.yml``.
Atlas expects ``pyenv`` and required OS build dependencies to already be installed on the host.
It keeps Python version management separate from the scripts virtual environment.

Script releases
---------------

``atlas scripts install <source>`` accepts these source forms:

* local release directory
* local ``.tar``, ``.tar.gz``, ``.tgz``, or ``.zip`` archive
* HTTP(S) archive URL
* git repository source as ``git+<repo-url>#<ref>``
* registry alias defined in ``/etc/atlas/config.yml``

Atlas installs releases under ``/opt/atlas/scripts/releases/<release-name>/<version>`` and
activates them through ``/opt/atlas/scripts/current/<release-name>`` symlinks.
Command collisions across active releases fail closed in discovery, shim generation, execution,
and command lookup.

Script context
--------------

Installed scripts should import from ``atlas_core`` instead of importing Atlas internals:

.. code-block:: python

   from atlas_core import get_context

   ctx = get_context()
   print(ctx.host.name)
   print(ctx.script.release_name)
