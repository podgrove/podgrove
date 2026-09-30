import { mkdir, readFile, rm, writeFile, copyFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve, dirname } from 'node:path';
import { pages, repository } from './pages.mjs';

const site = fileURLToPath(new URL('../', import.meta.url));
const root = resolve(site, '..');
const destination = resolve(site, 'src/content/docs/generated');
await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
for (const page of pages) {
  const markdown = await readFile(resolve(root, page.source), 'utf8');
  const match = /^# (.+)\r?\n/.exec(markdown);
  if (!match) throw new Error(`Expected one leading title in ${page.source}`);
  const frontmatter = [
    '---',
    `title: ${JSON.stringify(match[1])}`,
    `slug: ${JSON.stringify(page.slug)}`,
    `editUrl: ${JSON.stringify(`${repository}/edit/main/${page.source}`)}`,
    '---',
    '',
  ].join('\n');
  await writeFile(resolve(destination, `${page.slug}.md`), frontmatter + markdown.slice(match[0].length));
}
for (const name of ['logo.svg', 'logo-light.svg', 'logo-dark.svg']) {
  const output = resolve(site, 'public/brand', name);
  await mkdir(dirname(output), { recursive: true });
  await copyFile(resolve(root, 'docs/assets', name), output);
}
console.log(`Prepared ${pages.length} documentation pages from repository Markdown.`);
