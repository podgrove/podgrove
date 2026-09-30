# Documentation website

The public documentation lives at [podgrove.github.io/podgrove](https://podgrove.github.io/podgrove/), using Astro and Starlight on GitHub Pages.

## Edit and preview

Use Node.js 22.12 or newer. From this folder:

```sh
npm ci --ignore-scripts
npm run dev
```

Follow the printed localhost URL, including its `/podgrove/` prefix. Edit the existing repository Markdown in `docs/`, `deploy/README.md`, `CONTRIBUTING.md`, or `SECURITY.md`. The build copies the explicitly mapped pages into an ignored generated directory, preserving the original commands and configuration. `scripts/pages.mjs` controls the navigation and route names; `scripts/links.mjs` sends documentation links to website routes and code/example links to GitHub. Rerun the development command after changing source Markdown. Edit the landing page in `src/content/docs/index.mdx` and styling in `src/styles/custom.css`.

## Check the result

```sh
npm test
npm run build
cd ..
uv sync --locked --extra web-test
uv run --no-sync python site/tests/check_build.py
uv run --no-sync playwright install chromium
uv run --no-sync python site/tests/browser.py --output artifacts/site
```

The browser check starts its own loopback server and tests the built site. It writes screenshots and results to the chosen evidence folder, then closes its server and browser. These checks do not require Kubernetes credentials or a Docker daemon.

## Publish

The Documentation workflow checks pull requests and deploys successful `main` builds to GitHub Pages. Only `site/dist` is uploaded. The repository's Pages source is **GitHub Actions**, and the deployment uses the `github-pages` environment. Workflow actions and npm dependencies are pinned; Dependabot proposes updates.

This is a public static documentation site. The local `podgrove web` dashboard continues to run on the developer's own machine; it is not deployed to GitHub Pages. Homebrew publication is a separate task described in the [release guide](../docs/releasing.md#homebrew-setup).
