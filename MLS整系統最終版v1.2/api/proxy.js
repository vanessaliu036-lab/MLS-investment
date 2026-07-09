// Vercel serverless function:把 /api/* 反代到 VPS(104.156.239.83:8000)
// 解 mixed content 問題:瀏覽器從 https://mls-v1-2.vercel.app 打到同源,
// server-to-server fetch VPS http,沒有 mixed content。
//
// Rewrite 規則(vercel.json):
//   { "source": "/api/(.*)", "destination": "/api/proxy" }
// Vercel 把路徑 params 放到 req.query,然後 function 內組出 upstream URL。

const UPSTREAM = "http://104.156.239.83:8000";

export default async function handler(req, res) {
  try {
    // Vercel catch-all:把 /api/<rest> 的 <rest> 從 req.url 或 query.path 拿
    // Rewrite "/api/(.*)" → "/api/proxy" 後,原始子路徑在 req.query[0] 或 req.url 後綴
    const sub =
      (req.query && Array.isArray(req.query.path) ? req.query.path.join("/") : null) ||
      (() => {
        const u = req.url || "";
        const i = u.indexOf("/api/");
        return i >= 0 ? u.slice(i + 5) : "";
      })();

    const qs = (req.url && req.url.includes("?"))
      ? req.url.slice(req.url.indexOf("?"))
      : "";
    const upstreamUrl = `${UPSTREAM}/api/${sub}${qs}`;

    const headers = { ...req.headers };
    delete headers.host;
    delete headers["content-length"];

    const init = {
      method: req.method || "GET",
      headers,
    };
    if (req.method && req.method !== "GET" && req.method !== "HEAD") {
      if (req.body !== undefined && req.body !== null) {
        if (typeof req.body === "string") init.body = req.body;
        else if (Buffer.isBuffer(req.body)) init.body = req.body;
        else init.body = JSON.stringify(req.body);
      }
    }

    const r = await fetch(upstreamUrl, init);

    res.status(r.status);
    r.headers.forEach((v, k) => {
      const lk = k.toLowerCase();
      if (lk === "transfer-encoding" || lk === "connection" || lk === "keep-alive") return;
      try { res.setHeader(k, v); } catch (e) { /* ignore */ }
    });
    res.setHeader("access-control-allow-origin", "*");

    const buf = await r.arrayBuffer();
    res.send(Buffer.from(buf));
  } catch (e) {
    res.status(502).json({ error: "proxy failure", detail: String(e), upstream: UPSTREAM });
  }
}