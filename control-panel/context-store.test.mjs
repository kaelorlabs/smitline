import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import JSZip from 'jszip';

import { addContext, clearContext, extractDocument, readContext } from './context-store.mjs';

function simplePdf(text) {
  const stream = `BT /F1 18 Tf 40 120 Td (${text}) Tj ET`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let value = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(value));
    value += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(value);
  value += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  value += offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  value += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(value, 'binary');
}

test('stores pasted text and uploaded text without persisting raw files', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'colleague-context-'));
  const index = path.join(directory, 'index.json');
  const result = await addContext(index, {
    text: 'Launch target: October 4.',
    files: [{ name: 'metrics.csv', data: Buffer.from('metric,value\nretention,94').toString('base64') }],
  });
  assert.equal(result.sources.length, 2);
  assert.equal(readContext(index).sources[1].text, 'metric,value\nretention,94');
  assert.equal(fs.statSync(index).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(directory), ['index.json']);
  assert.equal(clearContext(index).sources.length, 0);
});

test('rejects unsupported and oversized uploads', async () => {
  await assert.rejects(() => extractDocument({ name: 'archive.zip', data: Buffer.from('x').toString('base64') }), /not supported/);
  await assert.rejects(() => extractDocument({ name: 'large.txt', data: Buffer.alloc(8 * 1024 * 1024 + 1).toString('base64') }), /8 MB/);
});

test('extracts text from a DOCX document', async () => {
  const zip = new JSZip();
  zip.file('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>');
  zip.folder('_rels').file('.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>');
  zip.folder('word').file('document.xml', '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Renewal target is 94 percent.</w:t></w:r></w:p></w:body></w:document>');
  const buffer = await zip.generateAsync({ type: 'nodebuffer' });
  const result = await extractDocument({ name: 'renewal.docx', data: buffer.toString('base64') });
  assert.equal(result.kind, 'docx');
  assert.match(result.text, /Renewal target is 94 percent/);
});

test('extracts text from a PDF document', async () => {
  const result = await extractDocument({
    name: 'targets.pdf',
    data: simplePdf('Quarterly target is 120.').toString('base64'),
  });
  assert.equal(result.kind, 'pdf');
  assert.match(result.text, /Quarterly target is 120/);
});
