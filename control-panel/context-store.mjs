import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import mammoth from 'mammoth';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';

export const MAX_FILE_BYTES = 8 * 1024 * 1024;
export const MAX_TOTAL_TEXT = 1_000_000;
const TEXT_EXTENSIONS = new Set(['.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.yaml', '.yml', '.log']);

function normalizeText(value) {
  return String(value || '')
    .replace(/\0/g, '')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{4,}/g, '\n\n\n')
    .trim();
}

function safeName(value) {
  const name = path.basename(String(value || '')).replace(/[\u0000-\u001f]/g, '').trim();
  if (!name || name.length > 180) throw new Error('Each document needs a valid filename under 180 characters.');
  return name;
}

async function pdfText(buffer) {
  const loadingTask = getDocument({ data: new Uint8Array(buffer), useSystemFonts: true, isEvalSupported: false });
  const document = await loadingTask.promise;
  const pages = [];
  try {
    for (let pageNumber = 1; pageNumber <= document.numPages; pageNumber += 1) {
      const page = await document.getPage(pageNumber);
      const content = await page.getTextContent();
      pages.push(content.items.map(item => item.str || '').join(' '));
      page.cleanup();
    }
  } finally {
    await loadingTask.destroy();
  }
  return pages.join('\n\n');
}

export async function extractDocument(file) {
  const name = safeName(file?.name);
  if (typeof file?.data !== 'string') throw new Error(`${name} has no readable content.`);
  if (file.data.length > Math.ceil(MAX_FILE_BYTES * 4 / 3) + 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(file.data)) {
    throw new Error(`${name} has invalid or oversized encoded content.`);
  }
  const buffer = Buffer.from(file.data, 'base64');
  if (!buffer.length || buffer.length > MAX_FILE_BYTES) throw new Error(`${name} must be between 1 byte and 8 MB.`);
  const extension = path.extname(name).toLowerCase();
  let text;
  let kind;
  if (TEXT_EXTENSIONS.has(extension)) {
    text = buffer.toString('utf8');
    kind = 'text';
  } else if (extension === '.pdf') {
    text = await pdfText(buffer);
    kind = 'pdf';
  } else if (extension === '.docx') {
    text = (await mammoth.extractRawText({ buffer })).value;
    kind = 'docx';
  } else {
    throw new Error(`${name} is not supported. Use TXT, Markdown, CSV, TSV, JSON, YAML, PDF, or DOCX.`);
  }
  text = normalizeText(text);
  if (!text) throw new Error(`${name} did not contain extractable text.`);
  return { name, kind, text };
}

export function readContext(indexPath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
    if (parsed?.version !== 1 || !Array.isArray(parsed.sources)) return { version: 1, sources: [] };
    return { version: 1, sources: parsed.sources.filter(source => source && typeof source.text === 'string') };
  } catch {
    return { version: 1, sources: [] };
  }
}

function writeContext(indexPath, context) {
  fs.mkdirSync(path.dirname(indexPath), { recursive: true, mode: 0o700 });
  const temporary = `${indexPath}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(context, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, indexPath);
  fs.chmodSync(indexPath, 0o600);
}

export function publicContext(context) {
  return {
    sources: context.sources.map(({ id, name, kind, addedAt, text }) => ({
      id, name, kind, addedAt, characters: text.length,
    })),
    totalCharacters: context.sources.reduce((total, source) => total + source.text.length, 0),
  };
}

export async function addContext(indexPath, input = {}) {
  const existing = readContext(indexPath);
  const additions = [];
  const pasted = normalizeText(input.text);
  if (pasted) additions.push({ name: 'Pasted meeting context', kind: 'text', text: pasted });
  const files = Array.isArray(input.files) ? input.files : [];
  if (files.length > 10) throw new Error('Add no more than 10 documents at a time.');
  for (const file of files) additions.push(await extractDocument(file));
  if (!additions.length) throw new Error('Paste text or choose at least one document.');

  const now = new Date().toISOString();
  const sources = [...existing.sources, ...additions.map(source => ({
    id: crypto.randomBytes(10).toString('hex'),
    ...source,
    addedAt: now,
  }))];
  if (sources.reduce((total, source) => total + source.text.length, 0) > MAX_TOTAL_TEXT) {
    throw new Error('Meeting context is limited to 1,000,000 extracted characters. Clear older sources first.');
  }
  const context = { version: 1, sources };
  writeContext(indexPath, context);
  return publicContext(context);
}

export function clearContext(indexPath) {
  try { fs.unlinkSync(indexPath); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  return publicContext({ version: 1, sources: [] });
}
