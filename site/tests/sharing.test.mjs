import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';
import { prepareSharingAssets, sharingHead } from '../scripts/sharing.mjs';

test('social previews use absolute PNG URLs under the Pages base', () => {
  for (const property of ['og:image', 'twitter:image']) {
    const tags = sharingHead.filter(({ attrs }) => attrs.property === property || attrs.name === property);
    assert.equal(tags.length, 1);
    assert.equal(tags[0].attrs.content, 'https://podgrove.github.io/podgrove/brand/share.png');
  }
  for (const property of ['og:image:alt', 'twitter:image:alt']) {
    assert.match(sharingHead.find(({ attrs }) => attrs.property === property || attrs.name === property).attrs.content, /Podgrove/);
  }
});

test('sharing assets render the approved logo as nonempty PNGs at advertised dimensions', async () => {
  const output = await mkdtemp(resolve(tmpdir(), 'podgrove-sharing-'));
  try {
    const root = fileURLToPath(new URL('../../', import.meta.url));
    await prepareSharingAssets(root, output);
    for (const [name, width, height] of [['share', 1200, 630], ['icon-48', 48, 48], ['icon-180', 180, 180]]) {
      const buffer = await readFile(resolve(output, `brand/${name}.png`));
      const metadata = await sharp(buffer).metadata();
      assert.equal(metadata.format, 'png');
      assert.equal(metadata.width, width);
      assert.equal(metadata.height, height);
      assert.ok((await sharp(buffer).stats()).channels.slice(0, 3).every(({ stdev }) => stdev > 10));
    }
    const expected = await sharp(resolve(root, 'docs/assets/logo.svg')).resize(128, 128).flatten({ background: '#f7f5f2' }).removeAlpha().raw().toBuffer();
    const actual = await sharp(resolve(output, 'brand/share.png')).extract({ left: 72, top: 120, width: 128, height: 128 }).removeAlpha().raw().toBuffer();
    assert.equal(actual.length, expected.length);
    assert.ok(actual.every((value, index) => Math.abs(value - expected[index]) <= 1));
  } finally {
    await rm(output, { recursive: true, force: true });
  }
});
