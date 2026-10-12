import { mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import sharp from 'sharp';
import { base } from './pages.mjs';

export const origin = 'https://podgrove.github.io';
export const previewPath = 'brand/share.png';
export const previewAlt = 'Podgrove — your Compose stack, a separate environment for every worktree.';
const assetUrl = (path) => new URL(base + path, origin).href;

export const sharingHead = [
  { tag: 'meta', attrs: { property: 'og:image', content: assetUrl(previewPath) } },
  { tag: 'meta', attrs: { property: 'og:image:type', content: 'image/png' } },
  { tag: 'meta', attrs: { property: 'og:image:width', content: '1200' } },
  { tag: 'meta', attrs: { property: 'og:image:height', content: '630' } },
  { tag: 'meta', attrs: { property: 'og:image:alt', content: previewAlt } },
  { tag: 'meta', attrs: { name: 'twitter:image', content: assetUrl(previewPath) } },
  { tag: 'meta', attrs: { name: 'twitter:image:alt', content: previewAlt } },
  { tag: 'link', attrs: { rel: 'icon', type: 'image/png', sizes: '48x48', href: assetUrl('brand/icon-48.png') } },
  { tag: 'link', attrs: { rel: 'apple-touch-icon', sizes: '180x180', href: assetUrl('brand/icon-180.png') } },
];

export async function prepareSharingAssets(root, output) {
  await mkdir(resolve(output, 'brand'), { recursive: true });
  const logo = await readFile(resolve(root, 'docs/assets/logo.svg'));
  const surface = Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">
    <rect width="1200" height="630" fill="#f7f5f2"/>
    <g font-family="Arial, Helvetica, sans-serif" fill="#292524">
      <text x="228" y="220" font-size="88" font-weight="600">Podgrove</text>
      <text x="80" y="345" font-size="46">Your Compose stack.</text>
      <text x="80" y="410" font-size="46">A separate environment for every worktree.</text>
      <text x="80" y="552" font-size="25" fill="#78716c">podgrove.github.io/podgrove</text>
    </g>
  </svg>`);
  const mark = await sharp(logo).resize(128, 128).png().toBuffer();
  await sharp(surface).composite([{ input: mark, left: 72, top: 120 }]).png().toFile(resolve(output, previewPath));
  for (const size of [48, 180]) {
    await sharp(logo).resize(size, size).flatten({ background: '#f7f5f2' }).png()
      .toFile(resolve(output, `brand/icon-${size}.png`));
  }
}
