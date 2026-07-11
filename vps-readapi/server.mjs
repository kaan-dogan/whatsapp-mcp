// WhatsApp read-only query API
// -----------------------------
// Exposes SELECT-only access to the whatsmeow bridge's messages.db so cloud
// skills / routines can query WhatsApp over HTTPS (via Caddy) with a bearer
// token. There is NO write/send path here — the bridge's /api/send stays on
// localhost:8080 and is never proxied out.
//
// Zero npm dependencies: Node built-in http + node:sqlite (experimental, but
// available unflagged on the VPS Node 22.22).
//
// Env:
//   WHATSAPP_READ_TOKEN  (required)  bearer token clients must send
//   WHATSAPP_DB          path to messages.db (default /root/whatsapp/bridge/store/messages.db)
//   PORT                 listen port (default 45410, bound to 127.0.0.1 only)

import http from 'node:http';
import { DatabaseSync } from 'node:sqlite';

const TOKEN = process.env.WHATSAPP_READ_TOKEN || '';
const PORT = parseInt(process.env.PORT || '45410', 10);
const HOST = '127.0.0.1'; // localhost only; Caddy terminates TLS and proxies in

// Multi-account: WHATSAPP_DBS is a JSON map of account -> messages.db path.
// Falls back to a single "main" from WHATSAPP_DB for backwards compatibility.
let DBS;
try {
  DBS = process.env.WHATSAPP_DBS
    ? JSON.parse(process.env.WHATSAPP_DBS)
    : { main: process.env.WHATSAPP_DB || '/root/whatsapp/bridge/store/messages.db' };
} catch (e) {
  console.error('FATAL: WHATSAPP_DBS is not valid JSON:', e.message);
  process.exit(1);
}
const ACCOUNTS = Object.keys(DBS);

if (!TOKEN) {
  console.error('FATAL: WHATSAPP_READ_TOKEN is not set — refusing to start.');
  process.exit(1);
}

// One shared read-only handle per account. SQLite allows concurrent readers
// alongside the bridge's writes; readOnly guarantees we can never mutate.
const handles = new Map();
function getDb(account) {
  if (!handles.has(account)) {
    handles.set(account, new DatabaseSync(DBS[account], { readOnly: true }));
  }
  return handles.get(account);
}

function intParam(v, def, max) {
  const n = parseInt(v ?? '', 10);
  if (!Number.isFinite(n) || n < 0) return def;
  return max != null ? Math.min(n, max) : n;
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'x-robots-tag': 'noindex, nofollow', // never let this surface be indexed
  });
  res.end(body);
}

// Constant-time-ish token compare (lengths first, then value).
function tokenOk(header) {
  const provided = header && header.startsWith('Bearer ') ? header.slice(7) : '';
  return provided.length === TOKEN.length && provided === TOKEN;
}

const MSG_COLS = 'id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename';

const routes = {
  // GET /chats?limit=&offset=&search=
  '/chats'(d, q) {
    const limit = intParam(q.get('limit'), 20, 200);
    const offset = intParam(q.get('offset'), 0);
    const search = q.get('search');
    if (search) {
      const like = `%${search}%`;
      return {
        chats: d.prepare(
          `SELECT jid, name, last_message_time FROM chats
           WHERE name LIKE ? OR jid LIKE ?
           ORDER BY last_message_time DESC LIMIT ? OFFSET ?`
        ).all(like, like, limit, offset),
      };
    }
    return {
      chats: d.prepare(
        `SELECT jid, name, last_message_time FROM chats
         ORDER BY last_message_time DESC LIMIT ? OFFSET ?`
      ).all(limit, offset),
    };
  },

  // GET /messages?chat_jid=&limit=&offset=&search=&after=&before=&order=asc|desc
  '/messages'(d, q) {
    const limit = intParam(q.get('limit'), 50, 500);
    const offset = intParam(q.get('offset'), 0);
    const order = (q.get('order') || 'desc').toLowerCase() === 'asc' ? 'ASC' : 'DESC';
    const where = [];
    const args = [];
    const chatJid = q.get('chat_jid');
    if (chatJid) { where.push('chat_jid = ?'); args.push(chatJid); }
    const search = q.get('search');
    if (search) { where.push('content LIKE ?'); args.push(`%${search}%`); }
    const after = q.get('after');
    if (after) { where.push('timestamp >= ?'); args.push(after); }
    const before = q.get('before');
    if (before) { where.push('timestamp <= ?'); args.push(before); }
    const whereSql = where.length ? `WHERE ${where.join(' AND ')}` : '';
    args.push(limit, offset);
    return {
      messages: d.prepare(
        `SELECT ${MSG_COLS} FROM messages ${whereSql}
         ORDER BY timestamp ${order} LIMIT ? OFFSET ?`
      ).all(...args),
    };
  },

  // GET /contacts?query=   (search chats by name or jid)
  '/contacts'(d, q) {
    const query = q.get('query');
    if (!query) return { error: 'query param required', _status: 400 };
    const like = `%${query}%`;
    const limit = intParam(q.get('limit'), 50, 200);
    return {
      contacts: d.prepare(
        `SELECT jid, name, last_message_time FROM chats
         WHERE name LIKE ? OR jid LIKE ?
         ORDER BY last_message_time DESC LIMIT ?`
      ).all(like, like, limit),
    };
  },

  // GET /context?chat_jid=&message_id=&before=&after=
  // Messages surrounding a target message (by timestamp within the chat).
  '/context'(d, q) {
    const chatJid = q.get('chat_jid');
    const messageId = q.get('message_id');
    if (!chatJid || !messageId) return { error: 'chat_jid and message_id required', _status: 400 };
    const nBefore = intParam(q.get('before'), 5, 50);
    const nAfter = intParam(q.get('after'), 5, 50);
    const target = d.prepare(
      `SELECT ${MSG_COLS} FROM messages WHERE chat_jid = ? AND id = ?`
    ).get(chatJid, messageId);
    if (!target) return { error: 'message not found', _status: 404 };
    const before = d.prepare(
      `SELECT ${MSG_COLS} FROM messages WHERE chat_jid = ? AND timestamp < ?
       ORDER BY timestamp DESC LIMIT ?`
    ).all(chatJid, target.timestamp, nBefore).reverse();
    const after = d.prepare(
      `SELECT ${MSG_COLS} FROM messages WHERE chat_jid = ? AND timestamp > ?
       ORDER BY timestamp ASC LIMIT ?`
    ).all(chatJid, target.timestamp, nAfter);
    return { before, target, after };
  },
};

const server = http.createServer((req, res) => {
  const ua = req.headers['user-agent'] || '-';
  res.on('finish', () => {
    console.log(`${new Date().toISOString()} ${res.statusCode} ${req.method} ${req.url} ua="${ua}"`);
  });

  let url;
  try { url = new URL(req.url, 'http://localhost'); }
  catch { return json(res, 400, { error: 'bad request' }); }
  const path = url.pathname.replace(/\/+$/, '') || '/';

  if (path === '/health') return json(res, 200, { ok: true, service: 'whatsapp-readapi', accounts: ACCOUNTS });

  if (!tokenOk(req.headers['authorization'])) return json(res, 401, { error: 'unauthorized' });
  if (req.method !== 'GET') return json(res, 405, { error: 'read-only: GET only' });

  const handler = routes[path];
  if (!handler) return json(res, 404, { error: 'not found', routes: Object.keys(routes).concat('/health') });

  const account = url.searchParams.get('account') || 'main';
  if (!DBS[account]) return json(res, 400, { error: `unknown account '${account}'`, accounts: ACCOUNTS });

  try {
    const out = handler(getDb(account), url.searchParams);
    const status = out._status || 200;
    if (out._status) delete out._status;
    return json(res, status, out);
  } catch (err) {
    console.error('query error:', err.message);
    return json(res, 500, { error: 'query failed', detail: err.message });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`whatsapp-readapi listening on http://${HOST}:${PORT} (accounts: ${ACCOUNTS.join(', ')})`);
});
