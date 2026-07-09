// Vercel serverless function:把 /api/* 反代到 VPS(104.156.239.83:8000)
// 解 mixed content 問題:瀏覽器從 https://mls-v1-2.vercel.app 打到同源,
// server-to-server fetch VPS http,沒有 mixed content。
const UPSTREAM = "http://104.156.239.83:8000";

export default async function handler(req, res) {
  // Vercel catch-all:子路徑陣列在 req.query.path
  const sub = (req.query && req.query.path) || [];
  const subPath = Array.isArray(sub) ? sub.join("/") : String(sub || "");
  const upstreamUrl = UPSTREAM + "/api/" + subPath + (req.url && req.url.includes("?") ? req.url.slice(req.url.indexOf("?")) : "");

  try {
    const headers = { ...req.headers };
    // 移除會干擾 upstream 的 header
    delete headers.host;
    delete headers["content-length"];

    const init = {
      method: req.method || "GET",
      headers,
    };
    if (req.method && req.method !== "GET" && req.method !== "HEAD" && req.body !== undefined && req.body !== null) {
      const raw = await readBody(req);
      if (raw && raw.length) init.body = raw;
    }

    const r = await fetch(upstreamUrl, init);

    // 把 upstream 的 status + header 透傳回去(去掉 hop-by-hop)
    res.status(r.status);
    r.headers.forEach((v, k) => {
      const lk = k.toLowerCase();
      if (lk === "transfer-encoding" || lk === "connection" || lk === "keep-alive") return;
      try { res.setHeader(k, v); } catch (e) { /* ignore */ }
    });
    // CORS 對 vercel origin 開放
    res.setHeader("access-control-allow-origin", "*");
    const buf = await r.arrayBuffer();
    res.send(Buffer.from(buf));
  } catch (e) {
    res.status(502).json({ error: "proxy failure", detail: String(e), upstream: UPSTREAM });
  }
}

function readBody(req) {
  return new Promise((resolve) => {
    if (typeof req.body === "string") return resolve(req.body);
    if (Buffer.isBuffer(req.body)) return resolve(req.body);
    const chunks = [];
    req.on && req.on("data", (c) => chunks.push(c));
    req.on && req.on("end", () => resolve(Buffer.concat(chunks)));
    // body 已被 express-style middleware 解析的情況
    try {
      if (req.body && typeof req.body === "object") return resolve(JSON.stringify(req.body));
    } catch (e) {}
    resolve("");
  });
}

export const config = {
  api: { bodyParser: false },
};
