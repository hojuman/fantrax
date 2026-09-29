// Small helpers for the local UI: action buttons, a tiny markdown renderer, and the streaming chat.
const hockey = (() => {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // Enough markdown for Claude's answers: headings, bullets, **bold**, *italic*, `code`, [links](url).
  function md(text) {
    const inline = (s) =>
      esc(s)
        .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
        .replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const out = [];
    let list = false;
    for (const raw of String(text).split("\n")) {
      const line = raw.trimEnd();
      const item = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
      if (item) {
        if (!list) { out.push("<ul>"); list = true; }
        out.push(`<li>${inline(item[1])}</li>`);
        continue;
      }
      if (list) { out.push("</ul>"); list = false; }
      const h = line.match(/^#{1,4}\s+(.*)$/);
      if (h) out.push(`<p><b>${inline(h[1])}</b></p>`);
      else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push("</ul>");
    return out.join("");
  }

  async function post(url, btn, statusId, busyText) {
    const status = statusId && document.getElementById(statusId);
    if (btn) btn.disabled = true;
    if (status) status.textContent = busyText || "Working…";
    try {
      const r = await fetch(url, { method: "POST" });
      const data = await r.json();
      if (data.error) {
        if (status) status.innerHTML = `<span class="bad">${esc(data.error)}</span>`;
        return null;
      }
      return data;
    } catch (e) {
      if (status) status.innerHTML = `<span class="bad">${esc(e)}</span>`;
      return null;
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function sync(btn) {
    const data = await post("/api/sync", btn, "sync-status", "Syncing Fantrax, NHL and MoneyPuck… (this can take a minute)");
    if (data) location.reload();
  }

  async function news(btn) {
    const data = await post("/api/news", btn, "news-status", "Searching the news for your roster and top free agents…");
    if (data) {
      if (data.dropped_domains && data.dropped_domains.length)
        alert("Skipped news sites that block Anthropic's search: " + data.dropped_domains.join(", ") + ". Remove them from ai.news_domains in data/league.yaml.");
      location.reload();
    }
  }

  async function take(btn) {
    const data = await post("/api/take", btn, "take-status", "Claude is reading the report…");
    if (data) {
      document.getElementById("take").innerHTML = md(data.text);
      document.getElementById("take-status").textContent = "";
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-md]").forEach((el) => { el.innerHTML = md(el.textContent); });
  });

  // ---- chat (/ask) ----
  function chat() {
    const form = document.getElementById("ask-form");
    if (!form) return;
    const log = document.getElementById("chat");
    const box = form.querySelector("textarea");
    const web = document.getElementById("web");
    const send = form.querySelector("button[type=submit]");
    let session = sessionStorage.getItem("hockey-chat") || "";

    document.querySelectorAll(".examples button").forEach((b) =>
      b.addEventListener("click", () => { box.value = b.textContent; box.focus(); }));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });
    document.getElementById("new-chat").addEventListener("click", () => {
      session = ""; sessionStorage.removeItem("hockey-chat"); log.innerHTML = "";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const q = box.value.trim();
      if (!q) return;
      box.value = "";
      log.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(q)}</div>`);
      const bot = document.createElement("div");
      bot.className = "msg bot";
      bot.innerHTML = '<div class="tools"></div><div class="md"><span class="muted">Thinking…</span></div><div class="meta"></div>';
      log.appendChild(bot);
      bot.scrollIntoView({ behavior: "smooth", block: "end" });
      const tools = bot.querySelector(".tools"), body = bot.querySelector(".md"), meta = bot.querySelector(".meta");
      let text = "";
      send.disabled = true;
      try {
        const r = await fetch("/api/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: q, session, web: web.checked }),
        });
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            if (!chunk.startsWith("data: ")) continue;
            const ev = JSON.parse(chunk.slice(6));
            if (ev.type === "session") { session = ev.session; sessionStorage.setItem("hockey-chat", session); }
            else if (ev.type === "text") { text += ev.text; body.innerHTML = md(text); }
            else if (ev.type === "tool") {
              tools.insertAdjacentHTML("beforeend", `<span title="${esc(ev.detail)}">${ev.name === "web_search" ? "🔎 " + esc(ev.detail) : "📊 " + esc(ev.name)}</span>`);
            } else if (ev.type === "error") { body.innerHTML = `<span class="bad">${esc(ev.message)}</span>`; }
            else if (ev.type === "done") {
              const bits = [];
              if (ev.truncated) bits.push('<span class="warn">The answer hit the length limit.</span>');
              if (ev.sources && ev.sources.length)
                bits.push("Sources: " + ev.sources.map(([t, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>`).join(" · "));
              if (ev.dropped_domains && ev.dropped_domains.length)
                bits.push(`<span class="warn">Skipped sites that block Anthropic's search: ${esc(ev.dropped_domains.join(", "))}</span>`);
              if (ev.credit) bits.push(esc(ev.credit));
              bits.push(`Recommendations only · ${ev.tokens.in.toLocaleString()} in / ${ev.tokens.out.toLocaleString()} out tokens` + (ev.searches ? ` · ${ev.searches} searches` : ""));
              meta.innerHTML = bits.join("<br>");
            }
            bot.scrollIntoView({ behavior: "smooth", block: "end" });
          }
        }
      } catch (err) {
        body.innerHTML = `<span class="bad">${esc(err)}</span>`;
      } finally {
        send.disabled = false;
        box.focus();
      }
    });
  }
  document.addEventListener("DOMContentLoaded", chat);

  return { md, sync, news, take };
})();
