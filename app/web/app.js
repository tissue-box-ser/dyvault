/* ============================================================
   DyVault · 控制台
   ============================================================ */

const el = (id) => document.getElementById(id);
const TOKEN_KEY = "dy_dl_token";
let token = localStorage.getItem(TOKEN_KEY) || "";

// 支持 ?token=xxx 直接进入（脚本 / 书签场景），避免每次过登录页
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) {
  token = urlToken;
  localStorage.setItem(TOKEN_KEY, urlToken);
}

let lastState = null;
let lastCookies = [];
let settingsLoaded = false;
let logSignature = "";
let currentView = "download";
let currentFilter = "all";
let currentSource = "post";
let filesPath = "";
let filesBase = "collection";
let systemInfo = null;
let dlJobId = null;
let dlItems = [];
const jobNodes = new Map();

const STATUS_TEXT = {
  pending: "排队中",
  running: "运行中",
  paused: "已暂停",
  done: "已完成",
  error: "出错",
  canceled: "已取消",
  interrupted: "已中断",
};

const SOURCE_TEXT = { post: "博主作品", like: "博主点赞", collect: "我的收藏" };

/* ------------------------------------------------------------ 工具 */

function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function humanSize(num) {
  num = Number(num || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (num >= 1024 && i < units.length - 1) { num /= 1024; i += 1; }
  return (i === 0 ? num.toFixed(0) : num.toFixed(1)) + units[i];
}

function timeText(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function icon(name) {
  return `<svg class="ico"><use href="#i-${name}"/></svg>`;
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (token) headers["X-Token"] = token;
  const resp = await fetch(path, Object.assign({}, options, { headers }));
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`;
    let data = {};
    try {
      data = await resp.json();
      message = data.detail || message;
    } catch (err) { /* ignore */ }
    if (resp.status === 401 && data.need_login) {
      location.href = "/login";
      throw new Error("登录已过期");
    }
    throw new Error(message);
  }
  return resp.json();
}

function toast(message, bad = false) {
  const node = el("toast");
  node.textContent = message;
  node.style.background = bad ? "#b3173f" : "#14171e";
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 2800);
}

/* ------------------------------------------------------------ 任务渲染 */

function matchFilter(job) {
  if (currentFilter === "active") return ["pending", "running", "paused"].includes(job.status);
  if (currentFilter === "done") return ["done", "error", "canceled", "interrupted"].includes(job.status);
  return true;
}

function jobHTML(job) {
  const pct = job.total ? Math.min(100, Math.round((job.done / job.total) * 100))
    : (job.status === "done" ? 100 : 0);
  const status = STATUS_TEXT[job.status] || job.status;
  const opts = job.options || {};
  const browser = opts.storage === "browser";
  const perAweme = opts.dir_layout === "per_aweme";

  const tags = [
    browser ? `<span class="tag remote">${icon("laptop")}本机</span>` : "",
    perAweme ? `<span class="tag">单作品目录</span>` : "",
  ].join("");

  const nums = [
    `<span>作品 <b>${job.done || 0}</b>/${job.total || "?"}</span>`,
    `<span>评论 <b>${job.comments || 0}</b></span>`,
    browser ? "" : `<span>新增 <b>${humanSize(job.bytes)}</b></span>`,
    job.failed ? `<span class="fail">失败 <b>${job.failed}</b></span>` : "",
    `<span>${escapeHtml(SOURCE_TEXT[job.source] || "作品")}</span>`,
    job.dir
      ? `<span class="nums-link" data-act="copy" data-dir="${escapeHtml(job.dir)}" title="${escapeHtml(job.dir)}">目录</span>`
      : "",
  ].filter(Boolean).join("");

  let actions = "";
  if (job.status === "running") {
    actions = `<button class="act" data-act="pause">${icon("pause")}暂停</button>
               <button class="act danger" data-act="cancel">${icon("stop")}取消</button>`;
  } else if (job.status === "paused" || job.status === "pending") {
    actions = `<button class="act" data-act="resume">${icon("play")}继续</button>
               <button class="act danger" data-act="cancel">${icon("stop")}取消</button>`;
  } else {
    const canDownload = (job.done || 0) > 0;
    actions = `${canDownload ? `<button class="act" data-act="download">${icon("dl")}下载</button>` : ""}
               <button class="act" data-act="rerun">${icon("redo")}重跑</button>
               <button class="act danger" data-act="delete">${icon("trash")}删除</button>`;
  }
  if (job.status === "running" && browser && (job.done || 0) > 0) {
    actions += `<button class="act" data-act="download">${icon("dl")}下载</button>`;
  }

  const err = job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : "";

  return `
    <div class="job-top">
      <div style="min-width:0">
        <div class="job-title" title="${escapeHtml(job.title)}">${escapeHtml(job.title)}${tags}</div>
        <div class="job-meta">#${job.id}<span class="dot">·</span>${timeText(job.created_at)}${job.nickname ? `<span class="dot">·</span>${escapeHtml(job.nickname)}` : ""}</div>
      </div>
      <span class="badge ${job.status}">${status}</span>
    </div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="job-foot">
      <div class="job-nums">${nums}</div>
      <div class="job-actions">${actions}</div>
    </div>
    ${err}`;
}

function renderJobs(jobs) {
  const container = el("jobs");
  const list = jobs.filter(matchFilter);
  const seen = new Set();

  list.forEach((job, index) => {
    const key = String(job.id);
    seen.add(key);
    let node = jobNodes.get(key);
    if (!node) {
      node = document.createElement("div");
      node.dataset.id = key;
      jobNodes.set(key, node);
    }
    const html = jobHTML(job);
    if (node.dataset.html !== html) {
      node.innerHTML = html;
      node.dataset.html = html;
    }
    node.className = "job " + job.status;
    if (container.children[index] !== node) {
      container.insertBefore(node, container.children[index] || null);
    }
  });

  jobNodes.forEach((node, key) => {
    if (!seen.has(key)) {
      node.remove();
      jobNodes.delete(key);
    }
  });

  const isEmpty = list.length === 0;
  el("jobsEmpty").style.display = isEmpty ? "block" : "none";
  el("jobsEmpty").querySelector("p").textContent = jobs.length ? "当前筛选下没有任务" : "还没有任务";
  el("jobCount").textContent = jobs.length;
}

/* ------------------------------------------------------------ 数据刷新 */

async function refresh() {
  if (document.hidden) return;
  try {
    const state = await api("/api/state");
    lastState = state;
    const enabled = state.cookies ? state.cookies.enabled : 0;
    el("connText").textContent = enabled ? `${enabled} 个账号可用` : "未配置 Cookie";
    el("connState").className = "chip state " + (enabled ? "ok" : "bad");
    el("sideCookie").textContent = `${enabled} 个账号`;

    renderJobs(state.jobs || []);
    el("statJobs").textContent = state.stats.jobs;
    el("statAwemes").textContent = state.stats.awemes;
    el("statBytes").textContent = humanSize(state.stats.bytes);
    const root = state.stats.download_root;
    el("dlRoot").textContent = root;
    el("dlRoot").title = root;
    el("cookieSummary").textContent = enabled
      ? `${enabled} 个账号轮询中（共 ${state.cookies.total} 个）`
      : "还没有添加账号，去「设置」添加";
    if (!settingsLoaded) fillSettings(state.settings);
  } catch (err) {
    el("connText").textContent = "连接失败";
    el("connState").className = "chip state bad";
  }
}

function paintDisk(prefix, info, withPct = false) {
  if (!info || !el(prefix + "Fill")) return;
  const pct = Math.min(100, Number(info.percent || 0));
  el(prefix + "Fill").style.width = pct + "%";
  el(prefix + "Fill").style.background = pct > 88
    ? "linear-gradient(90deg,#e11d48,#fb7185)"
    : "linear-gradient(90deg,#fe2c55,#ff8a9e)";
  el(prefix + "Text").textContent = `已用 ${humanSize(info.used)} / 剩余 ${humanSize(info.free)}`;
  if (withPct) el(prefix + "Pct").textContent = pct.toFixed(0) + "%";
}

async function refreshSystem() {
  try {
    const info = await api("/api/system");
    systemInfo = info;
    const disks = info.disks || { collection: info.disk, per_aweme: info.disk };
    const roots = info.roots || {};
    const disk = disks.collection || {};
    paintDisk("disk", disk, true);
    paintDisk("diskInline", disks.collection);
    paintDisk("diskPer", disks.per_aweme);
    el("diskHost").textContent = `${info.hostname} · ${humanSize(disk.free)} 可用`;
    el("verText").textContent = info.version || "2.1.0";
    el("aboutVersion").textContent = info.version || "2.1.0";
    el("aboutPlatform").textContent = `${info.platform} · Python ${info.python}`;
    const current = disks[filesBase] || disk;
    el("filesFree").textContent = `${humanSize(current.free)} / ${humanSize(current.total)}`;
    el("tabPathCol").textContent = roots.collection || "—";
    el("tabPathPer").textContent = roots.per_aweme || "—";
    el("tabPathCol").title = roots.collection || "";
    el("tabPathPer").title = roots.per_aweme || "";
    el("diskInlinePath").textContent = roots.collection || "—";
    el("diskPerPath").textContent = roots.per_aweme || "—";
    el("setRootResolved").textContent = roots.collection || "—";
    updateTargetHint();
    if (currentView === "files") {
      el("filesRoot").textContent = roots[filesBase] || "—";
    }
  } catch (err) { /* 静默 */ }
}

async function refreshLogs() {
  try {
    const data = await api("/api/logs?limit=100");
    const logs = data.logs || [];
    const signature = logs.length ? `${logs[logs.length - 1].id}` : "";
    if (signature === logSignature) return;
    logSignature = signature;
    const box = el("logs");
    if (!logs.length) {
      box.innerHTML = `<div class="empty-log">暂无日志</div>`;
      return;
    }
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    box.innerHTML = logs.map((item) => {
      const time = new Date(item.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
      return `<div class="${item.level}"><span class="time">${time}</span>${escapeHtml(item.message)}</div>`;
    }).join("");
    if (stick) box.scrollTop = box.scrollHeight;
  } catch (err) { /* ignore */ }
}

/* ------------------------------------------------------------ 文件库 */

function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadFiles(path = "") {
  filesPath = path;
  const box = el("files");
  box.innerHTML = `<div class="files-empty">加载中…</div>`;
  try {
    const data = await api(`/api/browse?base=${filesBase}&path=${encodeURIComponent(path)}`);
    el("filesRoot").textContent = data.root;
    document.querySelectorAll(".root-tab").forEach((node) => {
      node.classList.toggle("active", node.dataset.base === filesBase);
    });
    el("filesCur").textContent = "/" + (data.path || "");
    if (currentView === "files" && !systemInfo) refreshSystem();
    renderCrumbs(data.path || "");
    const entries = data.entries || [];
    if (!entries.length) {
      box.innerHTML = `<div class="files-empty">这个目录还是空的</div>`;
      return;
    }
    box.innerHTML = entries.map((item) => {
      const full = (data.path ? data.path + "/" : "") + item.name;
      if (item.is_dir) {
        return `<div class="file-row is-dir" data-dir="${escapeHtml(full)}">
          <span class="file-ico dir">${icon("folder")}</span>
          <span class="file-name">${escapeHtml(item.name)}</span>
          <span class="file-meta">${fmtDate(item.mtime)}</span>
          <span class="file-act">打开</span>
        </div>`;
      }
      return `<div class="file-row">
        <span class="file-ico">${icon("file")}</span>
        <span class="file-name">${escapeHtml(item.name)}</span>
        <span class="file-meta">${humanSize(item.size)} · ${fmtDate(item.mtime)}</span>
        <a class="file-act" href="/api/file?base=${filesBase}&path=${encodeURIComponent(full)}">下载</a>
      </div>`;
    }).join("");
  } catch (err) {
    box.innerHTML = `<div class="files-empty">${escapeHtml(err.message)}</div>`;
  }
}

function renderCrumbs(path) {
  const parts = path ? path.split("/").filter(Boolean) : [];
  const crumbs = [`<button class="crumb ${parts.length ? "" : "current"}" data-crumb="">根目录</button>`];
  parts.forEach((part, index) => {
    const sub = parts.slice(0, index + 1).join("/");
    crumbs.push(`<span class="crumb-sep">/</span>`);
    crumbs.push(`<button class="crumb ${index === parts.length - 1 ? "current" : ""}" data-crumb="${escapeHtml(sub)}">${escapeHtml(part)}</button>`);
  });
  el("crumbs").innerHTML = crumbs.join("");
}

/* ------------------------------------------------------------ Cookie 池 */

function renderCookies(items) {
  lastCookies = items || [];
  const box = el("cookieList");
  const ok = lastCookies.filter((c) => c.status === "ok").length;
  const bad = lastCookies.filter((c) => c.status === "bad" || c.status === "risk").length;
  el("cookieCount").textContent = `${lastCookies.length} 个账号`;
  el("statCookieTotal").textContent = lastCookies.length;
  el("statCookieOk").textContent = ok;
  el("statCookieBad").textContent = bad;

  if (!lastCookies.length) {
    box.innerHTML = `<div class="cookie-empty">还没有账号，添加一个已登录的抖音 Cookie 即可开始</div>`;
    return;
  }

  box.innerHTML = lastCookies.map((c) => `
    <div class="cookie-item ${c.enabled ? "" : "disabled"}" data-cid="${c.id}">
      <span class="cookie-dot ${escapeHtml(c.status || "unknown")}"></span>
      <div class="cookie-main">
        <div class="cookie-name">${escapeHtml(c.label)}
          ${c.has_login ? "" : `<span class="tag">无登录态</span>`}
          ${c.status === "bad" ? `<span class="tag">已失效</span>` : ""}
        </div>
        <div class="cookie-meta">${escapeHtml(c.mask)} · 使用 ${c.use_count || 0} 次 · ${c.last_used ? timeText(c.last_used) : "未使用"}</div>
      </div>
      <div class="cookie-actions">
        <button class="switch ${c.enabled ? "on" : ""}" data-cookie-toggle="${c.id}" title="启用 / 停用"></button>
        <button class="act" data-cookie-test="${c.id}">测试</button>
        <button class="act danger" data-cookie-del="${c.id}">${icon("trash")}</button>
      </div>
    </div>
  `).join("");
}

async function loadCookies() {
  try {
    const data = await api("/api/cookies");
    renderCookies(data.items || []);
  } catch (err) {
    el("cookieList").innerHTML = `<div class="cookie-empty">${escapeHtml(err.message)}</div>`;
  }
}

async function addCookie() {
  const label = el("newCookieLabel").value.trim();
  const value = el("newCookieValue").value.trim();
  const hint = el("addCookieHint");
  if (!value) { hint.textContent = "请粘贴 Cookie 内容"; hint.className = "test-result bad"; return; }
  hint.textContent = "添加中…";
  hint.className = "test-result";
  try {
    const data = await api("/api/cookies", { method: "POST", body: JSON.stringify({ label, value }) });
    renderCookies(data.items || []);
    el("newCookieLabel").value = "";
    el("newCookieValue").value = "";
    hint.textContent = "已添加，可点「测试」验证登录态";
    hint.className = "test-result ok";
    refresh();
  } catch (err) {
    hint.textContent = err.message;
    hint.className = "test-result bad";
  }
}

/* ------------------------------------------------------------ 设置 */

function fillSettings(values) {
  settingsLoaded = true;
  el("setRoot").value = values.download_root || "";
  el("setRootPer").value = values.download_root_per_aweme || "";
  el("setProxy").value = values.proxy || "";
  el("setInterval").value = values.min_interval || "1.2";
  el("setJitter").value = values.jitter || "0.8";
  el("setWorkers").value = values.workers || "4";
  el("setMaxJobs").value = values.max_concurrent_jobs || "1";
  el("setToken").value = values.auth_token || "";
  const resolved = values._download_root_resolved || "";
  const resolvedPer = values._download_root_per_aweme_resolved || "";
  if (resolved) {
    el("setRoot").placeholder = resolved;
    el("setRootResolved").textContent = resolved;
  }
  if (resolvedPer) el("setRootPer").placeholder = resolvedPer;
}

async function saveSettings() {
  const values = {
    download_root: el("setRoot").value.trim(),
    download_root_per_aweme: el("setRootPer").value.trim(),
    proxy: el("setProxy").value.trim(),
    min_interval: el("setInterval").value,
    jitter: el("setJitter").value,
    workers: el("setWorkers").value,
    max_concurrent_jobs: el("setMaxJobs").value,
    auth_token: el("setToken").value.trim(),
  };
  const hint = el("saveHint");
  hint.textContent = "保存中…";
  hint.className = "test-result";
  try {
    const res = await api("/api/settings", { method: "POST", body: JSON.stringify({ values }) });
    fillSettings(res.settings);
    hint.textContent = "已保存";
    hint.className = "test-result ok";
    toast("配置已保存");
    refresh();
    refreshSystem();
  } catch (err) {
    hint.textContent = err.message;
    hint.className = "test-result bad";
  }
}

async function changePassword() {
  const oldPass = el("oldPass").value;
  const newPass = el("newPass").value;
  const again = el("newPass2").value;
  const hint = el("passHint");
  if (newPass.length < 6) { hint.textContent = "新密码至少 6 位"; hint.className = "test-result bad"; return; }
  if (newPass !== again) { hint.textContent = "两次输入不一致"; hint.className = "test-result bad"; return; }
  hint.textContent = "提交中…";
  hint.className = "test-result";
  try {
    await api("/api/auth/password", { method: "POST", body: JSON.stringify({ old: oldPass, new: newPass }) });
    el("oldPass").value = "";
    el("newPass").value = "";
    el("newPass2").value = "";
    hint.textContent = "密码已更新，其他设备上的登录会失效";
    hint.className = "test-result ok";
  } catch (err) {
    hint.textContent = err.message;
    hint.className = "test-result bad";
  }
}

/* ------------------------------------------------------------ 视图切换 */

const VIEW_META = {
  download: { title: "下载任务", sub: "粘贴博主主页，抓取作品 / 点赞 / 收藏与评论" },
  files: { title: "文件库", sub: "浏览服务器本地已下载的文件" },
  settings: { title: "设置", sub: "账号池、抓取节奏、存储位置与访问安全" },
};

function switchView(view) {
  currentView = view;
  document.querySelectorAll(".nav-item").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === view);
  });
  el("viewDownload").classList.toggle("hidden", view !== "download");
  el("viewFiles").classList.toggle("hidden", view !== "files");
  el("viewSettings").classList.toggle("hidden", view !== "settings");
  el("pageTitle").textContent = VIEW_META[view].title;
  el("pageSub").textContent = VIEW_META[view].sub;
  if (view === "files") loadFiles(filesPath);
  if (view === "settings") { loadCookies(); refreshSystem(); }
}

/* ------------------------------------------------------------ 下载到本机 */

async function openDownload(jobId) {
  dlJobId = jobId;
  const job = (lastState && lastState.jobs || []).find((j) => String(j.id) === String(jobId));
  el("dlMask").classList.add("show");
  el("dlSub").textContent = "正在读取文件清单…";
  el("dlSummary").innerHTML = "";
  el("dlProgress").classList.add("hidden");
  el("dlBar").style.width = "0";
  el("dlState").textContent = "准备中…";
  el("dlZip").disabled = true;
  el("dlOne").disabled = true;
  try {
    const data = await api(`/api/jobs/${jobId}/manifest`);
    dlItems = data.items || [];
    const browser = job && job.options && job.options.storage === "browser";
    el("dlSub").textContent = job ? job.title : `任务 #${jobId}`;
    el("dlSummary").innerHTML = `
      <div class="dl-cell"><b>${dlItems.length}</b><span>文件</span></div>
      <div class="dl-cell"><b>${job ? (job.done || 0) : "—"}</b><span>作品</span></div>
      <div class="dl-cell"><b>${browser ? "本机" : "服务器"}</b><span>来源</span></div>`;
    el("dlTip").textContent = dlItems.length
      ? (browser
        ? "这些文件保存在抖音 CDN 上，下载时会经服务器中转后落到你的电脑；打包下载只需保存一个 ZIP。"
        : "文件已在服务器上，打包下载会实时压缩成一个 ZIP；“逐个下载”会依次保存到浏览器下载目录。")
      : "这个任务还没有产出文件。";
    el("dlZip").disabled = !dlItems.length;
    el("dlOne").disabled = !dlItems.length;
  } catch (err) {
    el("dlSub").textContent = err.message;
  }
}

function closeDownload() {
  el("dlMask").classList.remove("show");
}

function downloadZip() {
  if (!dlJobId || !dlItems.length) return;
  el("dlProgress").classList.remove("hidden");
  el("dlState").textContent = `正在打包 ${dlItems.length} 个文件，浏览器会开始下载…`;
  el("dlBar").style.width = "100%";
  location.href = `/api/jobs/${dlJobId}/archive`;
}

async function downloadOne() {
  if (!dlItems.length) return;
  el("dlProgress").classList.remove("hidden");
  el("dlZip").disabled = true;
  el("dlOne").disabled = true;
  for (let i = 0; i < dlItems.length; i += 1) {
    const item = dlItems[i];
    const name = (item.name || `file-${i + 1}`).split("/").pop();
    const anchor = document.createElement("a");
    anchor.href = item.url + (item.url.includes("?") ? "&" : "?") + "name=" + encodeURIComponent(name);
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    const pct = Math.round(((i + 1) / dlItems.length) * 100);
    el("dlBar").style.width = pct + "%";
    el("dlState").textContent = `正在下载 ${i + 1}/${dlItems.length}：${name}`;
    await new Promise((r) => setTimeout(r, 700));
  }
  el("dlState").textContent = `已发起 ${dlItems.length} 个下载，请在浏览器下载列表中查看。`;
  el("dlZip").disabled = false;
  el("dlOne").disabled = false;
  toast("已全部发起下载");
}

/* ------------------------------------------------------------ 创建任务 */

async function createJob() {
  const target = el("target").value.trim();
  if (!target) { toast("先粘贴博主主页链接", true); return; }
  const button = el("btnCreate");
  const label = button.innerHTML;
  button.disabled = true;
  button.textContent = "创建中…";
  try {
    const options = {
      limit: Number(el("limit").value || 0),
      max_comments: Number(el("maxComments").value || 0),
      download_video: el("optVideo").checked,
      download_images: el("optImages").checked,
      download_cover: el("optCover").checked,
      download_music: el("optMusic").checked,
      save_comments: el("optComments").checked,
      include_replies: el("optReplies").checked,
      save_raw: el("optRaw").checked,
      storage: el("storage").value,
      dir_layout: el("dirLayout").value,
    };
    const res = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ target, source: currentSource, options }),
    });
    el("target").value = "";
    el("preview").textContent = "";
    toast(`任务 #${res.job_id} 已开始`);
    refresh();
    refreshLogs();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = label;
  }
}

let parseTimer = null;
async function previewTarget() {
  const target = el("target").value.trim();
  if (!target) { el("preview").textContent = ""; return; }
  clearTimeout(parseTimer);
  parseTimer = setTimeout(async () => {
    el("preview").textContent = "识别中…";
    try {
      const res = await api("/api/parse", { method: "POST", body: JSON.stringify({ target }) });
      if (!res.ok) {
        el("preview").innerHTML = `<span class="warn">${escapeHtml(res.message)}</span>`;
        return;
      }
      const parts = [];
      if (res.nickname) parts.push(`<b>${escapeHtml(res.nickname)}</b>`);
      if (res.follower_count != null) parts.push(`粉丝 ${res.follower_count}`);
      if (res.aweme_count != null) parts.push(`作品 ${res.aweme_count}`);
      el("preview").innerHTML = parts.length ? `已识别 · ${parts.join(" · ")}` : "已识别 sec_uid";
      if (res.warning) el("preview").innerHTML += ` <span class="warn">（${escapeHtml(res.warning)}）</span>`;
    } catch (err) {
      el("preview").innerHTML = `<span class="warn">${escapeHtml(err.message)}</span>`;
    }
  }, 350);
}

/* ------------------------------------------------------------ 读取浏览器 Cookie */

function buildBookmarklet() {
  const origin = location.origin;
  return "javascript:(function(){var c=document.cookie||'';"
    + "location.href='" + origin + "/cookie-import#'+encodeURIComponent(c);})()";
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) { /* 落到 execCommand */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

function openBookmarklet() {
  const code = buildBookmarklet();
  el("bmCode").value = code;
  el("bmLink").setAttribute("href", code);
  el("bmMask").classList.add("show");
}

function closeBookmarklet() {
  el("bmMask").classList.remove("show");
}

/* ------------------------------------------------------------ 事件绑定 */

document.querySelectorAll(".nav-item").forEach((node) => {
  node.addEventListener("click", () => switchView(node.dataset.view));
});

el("jobFilter").addEventListener("click", (event) => {
  const button = event.target.closest(".filter");
  if (!button) return;
  currentFilter = button.dataset.filter;
  el("jobFilter").querySelectorAll(".filter").forEach((node) => {
    node.classList.toggle("active", node === button);
  });
  if (lastState) renderJobs(lastState.jobs || []);
});

el("sourceSeg").addEventListener("click", (event) => {
  const button = event.target.closest(".seg-item");
  if (!button) return;
  currentSource = button.dataset.source;
  el("sourceSeg").querySelectorAll(".seg-item").forEach((node) => {
    node.classList.toggle("active", node === button);
  });
});

function updateTargetHint() {
  const layout = el("dirLayout").value;
  const storage = el("storage").value;
  const roots = (systemInfo && systemInfo.roots) || {};
  if (storage === "browser") {
    el("targetHint").textContent = "保存位置：浏览器所在电脑的下载目录（服务器只保留清单与评论）";
    el("heroBadge").textContent = "保存到我的电脑";
    return;
  }
  el("heroBadge").textContent = "服务器本地保存";
  const path = roots[layout];
  el("targetHint").textContent = path ? `保存位置：${path}` : "";
}

el("storage").addEventListener("change", updateTargetHint);
el("dirLayout").addEventListener("change", updateTargetHint);

el("rootTabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".root-tab");
  if (!tab) return;
  filesBase = tab.dataset.base;
  document.querySelectorAll(".root-tab").forEach((node) => {
    node.classList.toggle("active", node === tab);
  });
  loadFiles("");
  refreshSystem();
});

document.addEventListener("click", async (event) => {
  const crumb = event.target.closest("[data-crumb]");
  if (crumb && !crumb.classList.contains("current")) {
    loadFiles(crumb.dataset.crumb);
    return;
  }

  const dirRow = event.target.closest(".file-row.is-dir");
  if (dirRow) {
    loadFiles(dirRow.dataset.dir);
    return;
  }

  const toggle = event.target.closest("[data-cookie-toggle]");
  if (toggle) {
    const cid = toggle.dataset.cookieToggle;
    const row = lastCookies.find((c) => String(c.id) === cid);
    try {
      const data = await api(`/api/cookies/${cid}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !(row && row.enabled) }),
      });
      renderCookies(data.items || []);
      refresh();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  const testBtn = event.target.closest("[data-cookie-test]");
  if (testBtn) {
    const cid = testBtn.dataset.cookieTest;
    testBtn.disabled = true;
    testBtn.textContent = "测试中…";
    try {
      const res = await api(`/api/cookies/${cid}/test`, { method: "POST" });
      renderCookies(res.items || []);
      toast(res.ok ? "账号正常，登录态有效" : (res.report && res.report.message) || "账号不可用", !res.ok);
      refresh();
    } catch (err) {
      toast(err.message, true);
      testBtn.disabled = false;
      testBtn.textContent = "测试";
    }
    return;
  }

  const delBtn = event.target.closest("[data-cookie-del]");
  if (delBtn) {
    const cid = delBtn.dataset.cookieDel;
    if (!confirm("删除这个账号的 Cookie？")) return;
    try {
      const data = await api(`/api/cookies/${cid}`, { method: "DELETE" });
      renderCookies(data.items || []);
      refresh();
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  const button = event.target.closest("[data-act]");
  if (!button) return;
  const act = button.dataset.act;

  if (act === "copy") {
    const dir = button.dataset.dir || "";
    try {
      await navigator.clipboard.writeText(dir);
      toast("路径已复制：" + dir);
    } catch (err) {
      toast(dir);
    }
    return;
  }

  const card = button.closest(".job");
  const jobId = card && card.dataset.id;
  if (!jobId) return;
  if (act === "download") { openDownload(jobId); return; }

  const endpoints = { pause: "pause", resume: "resume", cancel: "cancel", rerun: "rerun" };
  try {
    if (act === "delete") {
      if (!confirm(`删除任务 #${jobId} 的记录？已下载的文件不会被删除。`)) return;
      await api(`/api/jobs/${jobId}`, { method: "DELETE" });
      toast("记录已删除");
    } else if (endpoints[act]) {
      await api(`/api/jobs/${jobId}/${endpoints[act]}`, { method: "POST" });
    }
    refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

el("btnCreate").addEventListener("click", createJob);
el("target").addEventListener("input", previewTarget);
el("target").addEventListener("keydown", (event) => { if (event.key === "Enter") createJob(); });
el("btnSaveSettings").addEventListener("click", saveSettings);
el("btnAddCookie").addEventListener("click", addCookie);
el("btnChangePass").addEventListener("click", changePassword);
el("btnLogout").addEventListener("click", async () => {
  if (!confirm("退出登录？下次进入需要重新输入密码。")) return;
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.href = "/login";
});
el("btnClearLog").addEventListener("click", () => {
  el("logs").innerHTML = `<div class="empty-log">暂无日志</div>`;
  logSignature = "cleared";
});
el("btnRefresh").addEventListener("click", async () => {
  const button = el("btnRefresh");
  button.classList.add("spin");
  await Promise.all([refresh(), refreshSystem(), refreshLogs()]);
  if (currentView === "files") await loadFiles(filesPath);
  if (currentView === "settings") await loadCookies();
  setTimeout(() => button.classList.remove("spin"), 400);
});
el("btnUp").addEventListener("click", () => {
  const parts = filesPath.split("/").filter(Boolean);
  parts.pop();
  loadFiles(parts.join("/"));
});
el("btnReloadFiles").addEventListener("click", () => loadFiles(filesPath));
el("btnBrowserCookie").addEventListener("click", openBookmarklet);
el("bmClose").addEventListener("click", closeBookmarklet);
el("bmMask").addEventListener("click", (event) => { if (event.target === el("bmMask")) closeBookmarklet(); });
el("bmLink").addEventListener("click", (event) => {
  event.preventDefault();
  toast("把按钮拖到书签栏，或点「复制代码」手动新建书签");
});
el("bmCopy").addEventListener("click", async () => {
  const ok = await copyText(el("bmCode").value);
  toast(ok ? "书签代码已复制" : "复制失败，请手动全选文本框内容", !ok);
});

el("dlClose").addEventListener("click", closeDownload);
el("dlMask").addEventListener("click", (event) => { if (event.target === el("dlMask")) closeDownload(); });
el("dlZip").addEventListener("click", downloadZip);
el("dlOne").addEventListener("click", downloadOne);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { closeDownload(); closeBookmarklet(); }
});

/* ------------------------------------------------------------ 启动 */

const initialHash = location.hash.replace("#", "");
if (initialHash === "files") switchView("files");
if (initialHash === "settings") switchView("settings");
if (initialHash === "bookmarklet") { switchView("settings"); openBookmarklet(); }

refresh();
refreshSystem();
refreshLogs();
setInterval(refresh, 2000);
setInterval(refreshLogs, 3000);
setInterval(refreshSystem, 15000);
