Cloudflare Pages Hosting
========================

Atlas documentation is built as static HTML and can be hosted on Cloudflare Pages.
The repository includes ``wrangler.jsonc`` so the Pages build output directory is kept in source control.

Pages project settings
----------------------

Use these settings when creating or updating the Cloudflare Pages project:

* Project name: ``atlas-docs``
* Production branch: ``master``
* Build command: ``make html``
* Build output directory: ``build/html``
* Environment variable: ``PYTHON_VERSION=3.12``

The Makefile installs Atlas with development dependencies, then builds the Sphinx HTML output.
Cloudflare Pages publishes the generated files from ``build/html``.
Do not put ``build/html`` in the build command field; that value belongs only in the build output directory field.

If the project is configured with a deploy command such as ``npx wrangler versions upload``, Wrangler reads
``assets.directory`` from ``wrangler.jsonc`` and uploads ``build/html`` as static assets. For normal Pages Git
integration, leave the deploy command empty and let Pages publish the build output directory.

Local deployment check
----------------------

Build the site locally before deploying:

.. code-block:: bash

   mise run docs

If Wrangler is installed and authenticated, the generated site can be deployed directly:

.. code-block:: bash

   npx wrangler pages deploy build/html --project-name atlas-docs

Git integration is preferred for normal operation because it gives production deployments from
``master`` and preview deployments for pull requests.
