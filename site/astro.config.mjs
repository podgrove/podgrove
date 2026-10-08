import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { unified } from '@astrojs/markdown-remark';
import { pages, base, repository } from './scripts/pages.mjs';
import { rewriteDocumentationLinks } from './scripts/links.mjs';
import { origin, sharingHead } from './scripts/sharing.mjs';

export default defineConfig({
  site: origin,
  base,
  trailingSlash: 'always',
  markdown: { processor: unified({ remarkPlugins: [rewriteDocumentationLinks] }) },
  integrations: [
    starlight({
      title: 'Podgrove',
      description: 'Your Compose stack, a separate environment for every worktree.',
      logo: { light: './src/assets/logo-light.svg', dark: './src/assets/logo-dark.svg', replacesTitle: false },
      favicon: '/favicon.svg',
      head: sharingHead,
      customCss: ['./src/styles/custom.css'],
      social: [{ icon: 'github', label: 'GitHub', href: repository }],
      editLink: { baseUrl: `${repository}/edit/main/site/` },
      sidebar: [
        { label: 'Overview', link: '/' },
        ...[...new Set(pages.map((page) => page.group))].map((group) => ({
          label: group,
          items: pages.filter((page) => page.group === group).map((page) => ({ label: page.label, slug: page.slug })),
        })),
      ],
    }),
  ],
});
