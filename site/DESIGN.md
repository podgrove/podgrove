# Documentation design

Podgrove’s established brand is the source of truth. `src/styles/custom.css` maps the dashboard’s OKLCH palette and font stacks to Starlight variables. The page uses no external fonts, tracking or decorative animation.

The splash page has a concise introduction beside a real worktree/engine diagram, followed by an explanation of the model, an ordered setup sequence and documentation links. The diagram represents architecture, not a running cluster. Each worktree has its own Docker engine and persistent storage; sharing a namespace does not share that engine.

The Pods logo is copied from `docs/assets/logo.svg`. Light and dark diagram assets use the corresponding brand tokens. Only the splash heading uses display serif type; reference headings and body text use the existing sans-serif stack. Code uses the established monospace stack.

Interactive controls use restrained rounded corners, at least 44-pixel primary targets, and one inset focus outline. Starlight continues to own navigation, keyboard behavior, search, code copying, mobile menus and theme persistence. Custom CSS should not replace those components or duplicate their JavaScript.

Verify landing and reference pages at 320, 375, 414, 768 and desktop widths in both themes. Check actual root overflow, keyboard focus, search results, theme persistence and base-prefixed links. SVG text has an equivalent descriptive image alt. Avoid putting private cluster names, screenshots of credentials, release claims or unverified installation commands on this public surface.
