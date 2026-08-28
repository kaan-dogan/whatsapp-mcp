#!/usr/bin/env node
// whatsapp-outbox — staging area for files that are about to be sent on WhatsApp.
//
// Why this is a separate service from whatsapp-readapi: that one guarantees
// "GET only, databases opened read-only", and that guarantee is load-bearing --
// it is how this setup is reasoned about. Bolting an upload route onto it would
// quietly retire the guarantee. This service accepts writes and nothing else;
// it cannot read a chat.
//
// It also does NOT send anything. It takes bytes, parks them on disk, and
// returns a handle. Deciding who may receive a file stays where it already
// lives: the allowlist in mentee_send.py. Split this way, a leaked outbox token
// buys an attacker disk space, not a message to a student.
//
// Env:
//   WHATSAPP_OUTBOX_TOKEN  (required)  bearer token clients must send
//   OUTBOX_ROOT            staging dir (default /root/whatsapp/outbox)
//   OUTBOX_MAX_BYTES       per-file cap (default 20 MiB)
//   OUTBOX_TTL_HOURS       delete staged files older than this (default 24)
//   PORT                   listen port (default 45411, bound to 127.0.0.1 only)
import http from 'node:http';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

const TOKEN = process.env.WHATSAPP_OUTBOX_TOKEN || '';
const ROOT = process.env.OUTBOX_ROOT || '/root/whatsapp/outbox';
const MAX_BYTES = Number(process.env.OUTBOX_MAX_BYTES || 20 * 1024 * 1024);
const TTL_HOURS = Number(process.env.OUTBOX_TTL_HOURS || 24);
const PORT = Number(process.env.PORT || 45411);
const HOST = '127.0.0.1';

if (!TOKEN) {
  console.error('FATAL: WHATSAPP_OUTBOX_TOKEN is not set — refusing to start.');
  process.exit(1);
}
fs.mkdirSync(ROOT, { recursive: true, mode: 0o700 });

function json(res, code, obj) {
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
}

function tokenOk(header) {
  const provided = String(header || '').replace(/^Bearer\s+/i, '');
  const a = Buffer.from(provided);
  const b = Buffer.from(TOKEN);
  // timingSafeEqual throws unless the lengths match, so gate on length first.
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// The student sees this string as the document name in WhatsApp, so it has to
// stay human-readable -- but it also becomes a path segment, so anything that
// could escape the staging directory has to go.
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/g;

function safeFilename(raw) {
  let name = String(raw || '').normalize('NFC');
  name = name.split(/[/\\]/).pop();   // drop any directory part
  name = name.replace(CONTROL_CHARS, '');
  name = name.replace(/^\.+/, '');    // no leading dots ("..", ".hidden")
  name = name.trim();
  if (!name) name = 'belge.pdf';
  if (!/\.pdf$/i.test(name)) name += '.pdf';
  if (Buffer.byteLength(name) > 180) {
    // Trim the stem, never the extension, and cut on a character boundary.
    const stem = name.slice(0, -4);
    name = Array.from(stem).slice(0, 120).join('') + '.pdf';
  }
  return name;
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on('data', (chunk) => {
      total += chunk.length;
      if (total > limit) {
        // Stop reading rather than buffer an unbounded upload.
        reject(Object.assign(new Error('too large'), { code: 'TOO_LARGE' }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

async function sweep() {
  const cutoff = Date.now() - TTL_HOURS * 3600 * 1000;
  let removed = 0;
  const entries = await fsp.readdir(ROOT, { withFileTypes: true }).catch(() => []);
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(ROOT, entry.name);
    const st = await fsp.stat(dir).catch(() => null);
    if (st && st.mtimeMs < cutoff) {
      await fsp.rm(dir, { recursive: true, force: true }).catch(() => {});
      removed++;
    }
  }
  if (removed) console.log(`outbox sweep: removed ${removed} expired staging dir(s)`);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');

  if (url.pathname === '/health') {
    return json(res, 200, {
      ok: true, service: 'whatsapp-outbox', max_bytes: MAX_BYTES, ttl_hours: TTL_HOURS,
    });
  }
  if (!tokenOk(req.headers['authorization'])) return json(res, 401, { error: 'unauthorized' });

  if (req.method === 'POST' && url.pathname === '/') {
    // Reject on the declared length first. The streaming guard below is the
    // backstop for chunked uploads, but it has to destroy the socket to stop
    // reading, which leaves the client with a reset instead of an answer --
    // so give well-behaved clients a real 413 before a byte is read.
    const declared = Number(req.headers['content-length'] || 0);
    if (declared > MAX_BYTES) {
      return json(res, 413, { error: 'file too large', max_bytes: MAX_BYTES, bytes: declared });
    }

    let body;
    try {
      body = await readBody(req, MAX_BYTES);
    } catch (e) {
      if (e.code === 'TOO_LARGE') return json(res, 413, { error: 'file too large', max_bytes: MAX_BYTES });
      return json(res, 400, { error: 'read failed', detail: e.message });
    }

    if (body.length === 0) return json(res, 400, { error: 'empty body' });
    // Trust the bytes, not the name: a .pdf extension proves nothing about what
    // is about to be handed to a student.
    if (body.subarray(0, 5).toString('latin1') !== '%PDF-') {
      return json(res, 415, { error: 'not a PDF (missing %PDF- signature)' });
    }

    const filename = safeFilename(url.searchParams.get('filename') || req.headers['x-filename']);
    const handle = crypto.randomUUID();
    const dir = path.join(ROOT, handle);
    await fsp.mkdir(dir, { recursive: true, mode: 0o700 });
    await fsp.writeFile(path.join(dir, filename), body, { mode: 0o600 });

    const sha256 = crypto.createHash('sha256').update(body).digest('hex');
    console.log(`staged ${handle} ${filename} ${body.length}B sha256=${sha256.slice(0, 16)}`);
    return json(res, 201, {
      handle, filename, bytes: body.length, sha256, expires_in_hours: TTL_HOURS,
    });
  }

  if (req.method === 'GET' && url.pathname === '/') {
    const staged = [];
    const entries = await fsp.readdir(ROOT, { withFileTypes: true }).catch(() => []);
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const files = await fsp.readdir(path.join(ROOT, entry.name)).catch(() => []);
      if (!files.length) continue;
      const st = await fsp.stat(path.join(ROOT, entry.name, files[0])).catch(() => null);
      staged.push({
        handle: entry.name,
        filename: files[0],
        bytes: st ? st.size : null,
        staged_at: st ? st.mtime : null,
      });
    }
    return json(res, 200, { staged });
  }

  if (req.method === 'DELETE') {
    const handle = url.pathname.replace(/^\//, '');
    if (!/^[0-9a-f-]{36}$/.test(handle)) return json(res, 400, { error: 'bad handle' });
    await fsp.rm(path.join(ROOT, handle), { recursive: true, force: true }).catch(() => {});
    return json(res, 200, { deleted: handle });
  }

  return json(res, 404, {
    error: 'not found',
    routes: ['POST /', 'GET /', 'DELETE /<handle>', 'GET /health'],
  });
});

await sweep();
const timer = setInterval(sweep, 3600 * 1000);
if (timer.unref) timer.unref();

server.listen(PORT, HOST, () => {
  console.log(`whatsapp-outbox listening on http://${HOST}:${PORT} (root=${ROOT}, max=${MAX_BYTES}B, ttl=${TTL_HOURS}h)`);
});
