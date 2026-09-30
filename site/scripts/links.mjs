import path from 'node:path';
import { base, pages, repository } from './pages.mjs';

export function rewriteUrl(url, source) {
  if (!url || url.startsWith('#') || url.startsWith('/') || /^[a-z][a-z\d+.-]*:/i.test(url)) return url;
  const match = /^([^?#]*)(.*)$/.exec(url);
  const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(source), decodeURIComponent(match[1])));
  if (resolved.startsWith('../')) throw new Error(`Documentation link leaves repository: ${source}`);
  const page = pages.find((candidate) => candidate.source === resolved);
  if (page) return `${base}${page.slug}/${match[2]}`;
  if (resolved === 'README.md') return `${base}${match[2]}`;
  if (/^docs\/assets\/logo(?:-light|-dark)?\.svg$/.test(resolved)) return `${base}brand/${path.posix.basename(resolved)}${match[2]}`;
  return `${repository}/blob/main/${resolved.split('/').map(encodeURIComponent).join('/')}${match[2]}`;
}

export function rewriteDocumentationLinks() {
  return (tree, file) => {
    const filename = path.basename(file.path || '', '.md');
    const page = pages.find((candidate) => candidate.slug === filename);
    if (!page) return;
    function visit(node) {
      if (['link', 'image', 'definition'].includes(node.type)) node.url = rewriteUrl(node.url, page.source);
      for (const child of node.children || []) visit(child);
    }
    visit(tree);
  };
}
