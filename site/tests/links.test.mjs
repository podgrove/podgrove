import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rewriteUrl, rewriteDocumentationLinks } from '../scripts/links.mjs';

test('documentation links keep route fragments and the GitHub Pages base', () => {
  assert.equal(rewriteUrl('configuration.md#resources', 'docs/operations.md'), '/podgrove/configuration/#resources');
  assert.equal(rewriteUrl('../deploy/README.md', 'docs/getting-started.md'), '/podgrove/administration/');
  assert.equal(rewriteUrl('../docs/web.md', 'deploy/README.md'), '/podgrove/web/');
});

test('source examples stay on GitHub while documentation images resolve locally', () => {
  assert.equal(rewriteUrl('../examples/shared/podgrove.yml', 'docs/configuration.md'), 'https://github.com/podgrove/podgrove/blob/main/examples/shared/podgrove.yml');
  assert.equal(rewriteUrl('assets/logo-dark.svg', 'docs/branding.md'), '/podgrove/brand/logo-dark.svg');
  assert.equal(rewriteUrl('assets/architecture.svg', 'docs/how-it-works.md'), '/podgrove/diagrams/architecture.svg');
});

test('external links and in-page fragments remain intact', () => {
  for (const url of ['https://example.org/docs', 'mailto:team@example.org', '#install', '/podgrove/']) {
    assert.equal(rewriteUrl(url, 'docs/getting-started.md'), url);
  }
  assert.throws(() => rewriteUrl('../../private', 'docs/getting-started.md'), /leaves repository/);
});

test('link transformation handles references without changing code examples', () => {
  const tree = { children: [
    { type: 'code', lang: 'sh', value: 'cat configuration.md' },
    { type: 'definition', url: 'configuration.md#resources' },
    { type: 'paragraph', children: [{ type: 'link', url: 'operations.md', children: [] }] },
  ] };
  rewriteDocumentationLinks()(tree, { path: '/generated/getting-started.md' });
  assert.equal(tree.children[0].value, 'cat configuration.md');
  assert.equal(tree.children[1].url, '/podgrove/configuration/#resources');
  assert.equal(tree.children[2].children[0].url, '/podgrove/operations/');
});
