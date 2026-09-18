const $ = (sel) => document.querySelector(sel);

const THEME_STORAGE_KEY = "ticket-ai-theme";

/** Updates the toggle button's icon/title to reflect the current theme. */
function syncThemeToggleUI() {
  const theme = document.documentElement.getAttribute("data-theme") || "dark";
  const icon = $("#theme-toggle-icon");
  const btn = $("#theme-toggle");
  if (icon) icon.textContent = theme === "light" ? "🌙" : "☀️";
  if (btn) btn.title = theme === "light" ? "Switch to dark mode" : "Switch to light mode";
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme") || "dark";
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch (err) {
    // private browsing etc - theme still works for this session, just won't stick
  }
  syncThemeToggleUI();
}

async function checkHealth() {
  const badge = $("#health-badge");
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (res.ok) {
      badge.textContent = `● online — ${data.rows_loaded} tickets loaded`;
      badge.className = "badge ok";
    } else {
      throw new Error(data.detail || "unhealthy");
    }
  } catch (err) {
    badge.textContent = `● offline — ${err.message}`;
    badge.className = "badge err";
  }
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function renderTable(rows, columns) {
  if (!rows || rows.length === 0) {
    return `<p class="empty-note">No tickets matched this rule 🎉</p>`;
  }
  const head = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = rows
    .map(
      (r) =>
        `<tr>${columns.map((c) => `<td>${escapeHtml(r[c] ?? "")}</td>`).join("")}</tr>`
    )
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/** Turns the model's lightly-markdown answer into safe HTML - bold, bullets, paragraphs. */
function formatAnswer(raw) {
  const escaped = escapeHtml(raw).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const lines = escaped.split("\n");
  const out = [];
  let inList = false;
  for (const line of lines) {
    const bulletMatch = line.match(/^\s*[-•]\s+(.*)/);
    if (bulletMatch) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${bulletMatch[1]}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (line.trim() !== "") out.push(`<p>${line}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("") || `<p>${escaped}</p>`;
}

/** Types formatted HTML into `el` word by word with a blinking cursor, like a chat model streaming. */
function typeIntoElement(el, html, { speed = 16, onStep } = {}) {
  return new Promise((resolve) => {
    const tokens = html.match(/<[^>]+>|[^<]+/g) || [];
    const chunks = [];
    tokens.forEach((tok) => {
      if (tok.startsWith("<")) {
        chunks.push(tok);
      } else {
        (tok.match(/\s*\S+\s*|\s+/g) || []).forEach((w) => chunks.push(w));
      }
    });

    const cursor = document.createElement("span");
    cursor.className = "type-cursor";

    let buffer = "";
    let i = 0;

    function step() {
      if (i >= chunks.length) {
        el.innerHTML = buffer;
        onStep && onStep();
        resolve();
        return;
      }
      buffer += chunks[i++];
      el.innerHTML = buffer;
      el.appendChild(cursor);
      onStep && onStep();
      setTimeout(step, speed);
    }
    step();
  });
}

function scrollChatToBottom() {
  const box = $("#chat-messages");
  box.scrollTop = box.scrollHeight;
}

function addUserMessage(question) {
  const box = $("#chat-messages");
  const msg = document.createElement("div");
  msg.className = "msg msg-user";
  msg.innerHTML = `
    <div class="avatar user-avatar">🧑</div>
    <div class="bubble">${escapeHtml(question)}</div>
  `;
  box.appendChild(msg);
  scrollChatToBottom();
}

/** Adds a bot bubble with a typing indicator, returns it so we can fill it in once the answer arrives. */
function addBotTypingMessage() {
  const box = $("#chat-messages");
  const msg = document.createElement("div");
  msg.className = "msg msg-bot";
  msg.innerHTML = `
    <div class="avatar bot-avatar">🤖</div>
    <div class="bubble">
      <div class="typing-dots"><span></span><span></span><span></span></div>
    </div>
  `;
  box.appendChild(msg);
  scrollChatToBottom();
  return msg.querySelector(".bubble");
}

function addErrorMessage(text) {
  const box = $("#chat-messages");
  const msg = document.createElement("div");
  msg.className = "msg msg-bot msg-error";
  msg.innerHTML = `
    <div class="avatar bot-avatar">🤖</div>
    <div class="bubble">${escapeHtml(text)}</div>
  `;
  box.appendChild(msg);
  scrollChatToBottom();
}

async function askQuestion(question) {
  const input = $("#question-input");
  const sendBtn = $(".send-btn");
  input.value = "";
  input.disabled = true;
  sendBtn.disabled = true;

  addUserMessage(question);
  const bubble = addBotTypingMessage();

  try {
    const res = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Request failed");

    bubble.innerHTML = "";
    const answerEl = document.createElement("div");
    bubble.appendChild(answerEl);

    await typeIntoElement(answerEl, formatAnswer(data.answer || "(no answer)"), {
      onStep: scrollChatToBottom,
    });
  } catch (err) {
    bubble.closest(".msg").remove();
    addErrorMessage(`Error: ${err.message}`);
  } finally {
    input.disabled = false;
    sendBtn.disabled = false;
    input.focus();
    scrollChatToBottom();
  }
}

let anomaliesLoaded = false;
let anomalyData = { longRes: [], stale: [] };

const ANOMALY_LONG_RES_COLUMNS = [
  "ticket_id", "category", "priority", "status", "resolution_time_hrs", "agent_id", "issue_summary",
];
const ANOMALY_STALE_COLUMNS = [
  "ticket_id", "category", "priority", "status", "age_hrs", "agent_id", "issue_summary",
];

/** Filters anomaly rows we already have client-side, no need to re-fetch on filter change. */
function filterAnomalyRows(rows, filter) {
  if (filter === "critical") return rows.filter((r) => r.priority === "Critical");
  if (filter === "open") return rows.filter((r) => r.status === "Open");
  return rows;
}

function renderAnomalyTables(filter) {
  $("#anomaly-long-res").innerHTML = renderTable(
    filterAnomalyRows(anomalyData.longRes, filter),
    ANOMALY_LONG_RES_COLUMNS
  );
  $("#anomaly-stale").innerHTML = renderTable(
    filterAnomalyRows(anomalyData.stale, filter),
    ANOMALY_STALE_COLUMNS
  );
}

async function loadAnomalies() {
  $("#anomaly-summary").textContent = "Loading…";
  const res = await fetch("/api/anomalies");
  const data = await res.json();

  anomalyData.longRes = data.long_resolution_time || [];
  anomalyData.stale = data.stale_unresolved || [];

  $("#anomaly-summary").textContent =
    `As of ${data.reference_now} · ${data.long_resolution_time_count} long-resolution outliers · ` +
    `${data.stale_unresolved_count} stale unresolved high-priority tickets`;

  const activeBtn = document.querySelector(".filter-btn.active");
  renderAnomalyTables(activeBtn ? activeBtn.dataset.filter : "all");
}

/** Opens/closes the full-screen anomaly report panel, opened from the sidebar. */
function setAnomalyOverlayOpen(open) {
  const header = $("#anomaly-toggle");
  const overlay = $("#anomaly-overlay");

  header.setAttribute("aria-expanded", String(open));
  overlay.classList.toggle("open", open);
  overlay.setAttribute("aria-hidden", String(!open));
  document.body.classList.toggle("no-scroll", open);

  if (open && !anomaliesLoaded) {
    anomaliesLoaded = true;
    loadAnomalies();
  }
}

/** Opens/closes the sliding sidebar drawer. */
function setSidebarOpen(open) {
  const btn = $("#sidebar-toggle");
  $("#sidebar").classList.toggle("open", open);
  $("#sidebar").setAttribute("aria-hidden", String(!open));
  $("#sidebar-backdrop").classList.toggle("open", open);
  btn.setAttribute("aria-expanded", String(open));
  btn.title = open ? "Close sidebar" : "Open sidebar";
}

function toggleSidebar() {
  setSidebarOpen(!$("#sidebar").classList.contains("open"));
}

document.addEventListener("DOMContentLoaded", () => {
  checkHealth();
  syncThemeToggleUI();
  $("#theme-toggle").addEventListener("click", toggleTheme);

  $("#sidebar-toggle").addEventListener("click", toggleSidebar);
  $("#sidebar-close").addEventListener("click", () => setSidebarOpen(false));
  $("#sidebar-backdrop").addEventListener("click", () => setSidebarOpen(false));

  $("#anomaly-toggle").addEventListener("click", () => {
    setSidebarOpen(false);
    setAnomalyOverlayOpen(true);
  });
  $("#anomaly-close").addEventListener("click", () => setAnomalyOverlayOpen(false));
  $("#anomaly-overlay").addEventListener("click", (e) => {
    if (e.target.id === "anomaly-overlay") setAnomalyOverlayOpen(false);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if ($("#anomaly-overlay").classList.contains("open")) setAnomalyOverlayOpen(false);
    else if ($("#sidebar").classList.contains("open")) setSidebarOpen(false);
  });

  $("#query-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#question-input").value.trim();
    if (q) askQuestion(q);
  });

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      askQuestion(chip.dataset.q);
    });
  });

  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      renderAnomalyTables(btn.dataset.filter);
    });
  });
});
