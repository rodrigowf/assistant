/**
 * The shared internal-link corpus (spec 12 §9.4; `apps/protocol-fixtures/links/internal-links.json`,
 * also run by Android's `InternalLinksTest`).
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { findBarePaths, internalTargetUrl, linkableCode, resolveInternalLink, type InternalTarget } from './internalLinks';

interface Corpus {
  origin: string;
  cases: { href: string; context?: { visuals?: string[] }; expect: InternalTarget | null; note: string }[];
  inline_code: { code: string; linked: boolean }[];
  bare_text: { text: string; paths: string[] }[];
}

const corpus = JSON.parse(
  fs.readFileSync(path.resolve(process.cwd(), '..', 'protocol-fixtures', 'links', 'internal-links.json'), 'utf8'),
) as Corpus;

describe('resolveInternalLink (shared corpus)', () => {
  it.each(corpus.cases.map((c) => [c.note, c] as const))('%s', (_note, c) => {
    const visuals = c.context?.visuals ?? [];
    const got = resolveInternalLink(c.href, { origin: corpus.origin, isVisual: (p) => visuals.indexOf(p) >= 0 });
    expect(got).toEqual(c.expect);
  });
});

describe('linkableCode (shared corpus)', () => {
  it.each(corpus.inline_code.map((c) => [c.code, c.linked] as const))('%s → %s', (code, linked) => {
    expect(linkableCode(code, { origin: corpus.origin }) !== null).toBe(linked);
  });
});

describe('findBarePaths (shared corpus)', () => {
  it.each(corpus.bare_text.map((c) => [c.text, c.paths] as const))('%s', (text, paths) => {
    const found = findBarePaths(text);
    expect(found.map((f) => f.path)).toEqual(paths);
    for (const f of found) expect(text.slice(f.start, f.end)).toBe(f.path);
  });
});

describe('internalTargetUrl', () => {
  it('encodes segments and keeps memory fragments', () => {
    expect(internalTargetUrl({ kind: 'visual', path: 'visualizations/my chart.html' })).toBe('/visualizations/my%20chart.html');
    expect(internalTargetUrl({ kind: 'memory', path: 'archie/specs/12.md', fragment: 'x' })).toBe('/memory/archie/specs/12.md#x');
  });
});
