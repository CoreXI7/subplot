/* Subs page. Contract: ARCHITECTURE.md "The page" and "server.py". All DOM is built with el() and text nodes; never innerHTML.
   Class vocabulary shared with subs.css (style exactly these; the Agents board's /style.css gives the tokens):
     layout:    .subs .block .inline .count .subs-quiet .subs-warn .subs-error .subs-btn .subs-primary .tabs .tab .tab-badge
     coverage:  .coverage .cov-row .cov-name .cov-bars .cov-bar .cov-fill .cov-fill-en .cov-fill-es .cov-nums .cov-num .tonight
     progress:  .progress .prog-head .prog-label .prog-pct .prog-bar .prog-fill .prog-nums
                .now .now-head .now-dot .is-live .now-title .now-pct .now-bar .now-fill .is-indeterminate .now-meta
     switch:    .switch .switch-track .switch-knob .switch-text .switch-label .switch-hint
     review:    .review-list .review-card .review-head .review-title .review-meta .review-why .review-preview .review-cue
                .review-time .review-actions
     missing:   .search .missing-list .missing-row .missing-name .missing-nums .missing-actions .episodes .ep-row .ep-name
                .ep-status .status-have .status-queued .status-review .status-burned_in .status-missing .status-excluded
     recent:    .recent .recent-list .recent-row .recent-time .recent-what .recent-result
     sheet:     .sheet .sheet-inner .sheet-title .sheet-actions */
"use strict";

const API = "api";
const POLL_MS = 30000;
const STATUS_WORDS = { have: "has it", queued: "queued", review: "needs a look", burned_in: "burned-in", missing: "missing", excluded: "excluded" };

/* ------------------------------------------------------------------ helpers (same shapes as the Agents board) */
const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function setText(node, text) { if (node) node.textContent = text == null ? "" : String(text); }
function ago(iso, now) {
  if (!iso) return "–";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "–";
  const s = Math.max(0, Math.round(now - t / 1000));
  if (s < 60) return s + "s"; if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  return Math.floor(s / 86400) + "d";
}
function pct(a, b) { return b > 0 ? Math.min(100, Math.max(0, Math.round(100 * a / b))) : 0; }
function nn(v) { const x = Number(v); return Number.isFinite(x) && x > 0 ? x : 0; }
function fmtTime(ms) { const s = Math.floor(ms / 1000); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
function sxe(item) { return item && item.season != null && item.episode != null ? "S" + String(item.season).padStart(2, "0") + "E" + String(item.episode).padStart(2, "0") : ""; }
async function api(path, body) {
  /* GET when body is undefined, else POST JSON with X-Subs. Returns {status, data}; throws only on a network failure.
     Aborts (and so throws) after 15 s — a timeout is treated exactly like a network failure. */
  const opts = { cache: "no-store", redirect: "manual", headers: { Accept: "application/json" } };
  if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.headers["X-Subs"] = "1"; opts.body = JSON.stringify(body); }
  const ctrl = new AbortController();
  opts.signal = ctrl.signal;
  const timer = setTimeout(function () { ctrl.abort(); }, 15000);
  try {
    const r = await fetch(API + path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (r.status === 401 && data && data.signin === true) location.href = "login";   // AUTH=login: the session ended
    return { status: r.status, data };
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ state */
const state = { data: null, down: false, query: "", missing: null, openShow: null, showData: {}, pending: new Set(), timer: null, inFlight: false, progAnimated: false, nowMax: null };

/* ------------------------------------------------------------------ nav */
function safeNavHref(href) {
  /* A path starting with exactly one "/" and free of backslash, space or control characters, or an https:// URL. */
  if (typeof href !== "string" || !href) return false;
  if (href.slice(0, 8) === "https://") return true;
  if (href.charAt(0) !== "/" || href.charAt(1) === "/") return false;
  return !/[\\ \u0000-\u001f\u007f]/.test(href);
}
async function renderNav() {
  /* Build .tab links in #tabs from GET api/config (data.nav). The board already validates; re-check here and drop bad
     entries. Unhide #tabs only when at least one tab was added; a failed fetch leaves it hidden. */
  const host = $("tabs");
  if (!host) return;
  let data = null;
  try {
    const res = await api("/config");
    if (res && res.status === 200 && res.data && typeof res.data === "object" && !Array.isArray(res.data)) data = res.data;
  } catch (e) {
    data = null;
  }
  if (!data || !Array.isArray(data.nav)) return;
  const here = location.pathname;
  let added = 0;
  data.nav.forEach(function (entry) {
    if (!entry || typeof entry !== "object") return;
    const label = entry.label;
    const href = entry.href;
    if (typeof label !== "string" || !/^[A-Za-z0-9 .\-_&']{1,24}$/.test(label)) return;
    if (!safeNavHref(href)) return;
    const tab = document.createElement("a");
    tab.className = "tab";
    tab.setAttribute("href", href);
    const span = document.createElement("span");
    span.className = "tab-label";
    span.textContent = label;
    tab.appendChild(span);
    let path = null;
    try { path = new URL(href, location.href).pathname; } catch (e2) { path = null; }
    if (path !== null && path === here) tab.setAttribute("aria-current", "page");
    host.appendChild(tab);
    added++;
  });
  if (added > 0) host.hidden = false;
}

/* ------------------------------------------------------------------ rendering */
function renderCoverage() {
  /* One .cov-row per library in state.data.coverage: .cov-name, .cov-bars with an English .cov-bar (.cov-fill-en width =
     en/items %) and a thin Spanish bar (.cov-fill-es), and .cov-nums: "have N · missing N · burned-in N · ours N · needs a look N"
     (missing = items - en; each as a .cov-num). The fills animate once: set width in a requestAnimationFrame after insertion
     and only on the first render (keep a module flag). #tonight = "Tonight: found N · generated N · placed N · burned-in N ·
     PC idle <idle_since ago> (next window <first hour of idle_hours>)" or "PC busy: <why>". #stale shows "Scan is from <scan_at>;
     the nightly backup did not run" when data.stale. #summary = "<missing total> missing English · <review> to look at ·
     <queue> queued". #tab-subs badge = review count (hidden at 0). */
  const d = state.data;
  if (!d) return;
  const host = $("coverage");
  if (!host) return;
  const libs = d.coverage && typeof d.coverage === "object" ? d.coverage : {};
  const first = !state.covAnimated;
  let missingTotal = 0;
  let reviewTotal = 0;
  host.textContent = "";
  Object.keys(libs).forEach(function (name) {
    const c = libs[name] || {};
    const items = nn(c.items);
    const en = nn(c.en);
    const es = nn(c.es);
    const burned = nn(c.burned_in);
    const ours = nn(c.ours);
    const review = nn(c.review);
    const missing = Math.max(0, items - en);
    missingTotal += missing;
    reviewTotal += review;
    const row = el("div", "cov-row");
    row.appendChild(el("span", "cov-name", name));
    const bars = el("div", "cov-bars");
    const enBar = el("div", "cov-bar");
    const enFill = el("div", "cov-fill cov-fill-en");
    enBar.appendChild(enFill);
    bars.appendChild(enBar);
    const esBar = el("div", "cov-bar");
    const esFill = el("div", "cov-fill cov-fill-es");
    esBar.appendChild(esFill);
    bars.appendChild(esBar);
    row.appendChild(bars);
    const nums = el("div", "cov-nums");
    const pairs = [["have ", en], ["missing ", missing], ["burned-in ", burned], ["ours ", ours], ["needs a look ", review]];
    pairs.forEach(function (p, i) {
      if (i) nums.appendChild(document.createTextNode(" · "));
      nums.appendChild(el("span", "cov-num", p[0] + p[1]));
    });
    row.appendChild(nums);
    host.appendChild(row);
    if (first) {
      requestAnimationFrame(function () {
        enFill.style.width = pct(en, items) + "%";
        esFill.style.width = pct(es, items) + "%";
      });
    } else {
      enFill.style.width = pct(en, items) + "%";
      esFill.style.width = pct(es, items) + "%";
    }
  });
  state.covAnimated = true;
  missingTotal = nn(missingTotal);
  reviewTotal = nn(reviewTotal);

  const now = Date.now() / 1000;
  const pc = d.pc || {};
  const t = d.tonight || {};
  if (pc.idle) {
    const firstHour = Array.isArray(d.idle_hours) && d.idle_hours.length ? String(d.idle_hours[0]).split("-")[0] : "?";
    setText($("tonight"), "Tonight: found " + (Number(t.found) || 0) + " · generated " + (Number(t.generated) || 0) +
      " · placed " + (Number(t.placed) || 0) + " · burned-in " + (Number(t.burned_in) || 0) +
      " · PC idle " + ago(pc.idle_since, now) + " (next window " + firstHour + ")");
  } else {
    setText($("tonight"), "PC busy: " + (pc.why || "working"));
  }

  const stale = $("stale");
  if (stale) {
    if (d.stale) {
      setText(stale, "Scan is from " + (d.scan_at || "an unknown time") + "; the nightly backup did not run");
      stale.hidden = false;
    } else {
      setText(stale, "");
      stale.hidden = true;
    }
  }

  const reviewCount = Array.isArray(d.review) ? d.review.length : nn(reviewTotal);
  const queue = Array.isArray(d.queue) ? d.queue.length : nn(d.queue);
  setText($("summary"), missingTotal + " missing English · " + reviewCount + " to look at · " + queue + " queued");

  const badge = $("tab-subs");
  if (badge) {
    if (reviewCount > 0) { setText(badge, reviewCount); badge.hidden = false; }
    else { setText(badge, ""); badge.hidden = true; }
  }
}
function renderProgress() {
  /* Block "Progress" (first, above Coverage): overall bar from the summed coverage (one-decimal percent, numbers, and a
     one-time fill animation like renderCoverage), then the Now row from state.data.current: dot, "<Finding|Generating> ·
     <library> › <show|title> SxxEyy · <title>", a per-step percentage or indeterminate bar, phase words in the meta, and
     the never-backwards fill (state.nowMax) for the same step+key. Idle text when current is absent. */
  const d = state.data;
  if (!d) return;
  const libs = d.coverage && typeof d.coverage === "object" ? d.coverage : {};
  let items = 0;
  let en = 0;
  let burned = 0;
  Object.keys(libs).forEach(function (name) {
    const c = libs[name] || {};
    items += nn(c.items);
    en += nn(c.en);
    burned += nn(c.burned_in);
  });
  if (!Number.isFinite(items) || !Number.isFinite(en)) { items = 0; en = 0; }
  en = Math.min(en, items);
  const overall = items > 0 ? Math.min(100, 100 * en / items) : 0;
  const overallText = items > 0 ? overall.toFixed(1) : "–";
  setText($("prog-pct"), items > 0 ? overallText + "%" : overallText);
  const progBar = $("prog-bar");
  if (progBar) {
    if (items > 0) {
      progBar.setAttribute("aria-valuenow", String(Math.round(overall)));
      progBar.setAttribute("aria-valuetext", overallText + " percent, " + en.toLocaleString("en-US") + " of " + items.toLocaleString("en-US"));
    } else {
      progBar.setAttribute("aria-valuenow", "0");
      progBar.setAttribute("aria-valuetext", "No items yet");
    }
  }
  const progFill = $("prog-fill");
  if (progFill) {
    const width = (items > 0 ? overallText : "0") + "%";
    if (!state.progAnimated) requestAnimationFrame(function () { progFill.style.width = width; });
    else progFill.style.width = width;
  }
  state.progAnimated = true;
  const toGo = items - en;
  setText($("prog-nums"), en.toLocaleString("en-US") + " of " + items.toLocaleString("en-US") + " have English · " +
    toGo.toLocaleString("en-US") + " to go" + (burned > 0 ? " · " + burned.toLocaleString("en-US") + " burned-in" : ""));

  const now = Date.now() / 1000;
  const pc = d.pc || {};
  const budget = d.budget || {};
  const cur = d.current && typeof d.current === "object" ? d.current : null;
  const dot = $("now-dot");
  const title = $("now-title");
  const nowPctEl = $("now-pct");
  const nowBar = $("now-bar");
  const nowFill = $("now-fill");
  const meta = $("now-meta");
  if (dot) dot.classList.toggle("is-live", !!cur);
  if (!cur) {
    setText(title, "Idle");
    if (title) title.title = "Idle";
    setText(nowPctEl, "");
    if (nowBar) {
      nowBar.hidden = true;
      nowBar.classList.remove("is-indeterminate");
      nowBar.removeAttribute("aria-valuenow");
      nowBar.removeAttribute("aria-busy");
    }
    if (nowFill) nowFill.style.width = "";
    state.nowMax = null;
    if (pc.idle) {
      const firstHour = Array.isArray(d.idle_hours) && d.idle_hours.length ? String(d.idle_hours[0]).split("-")[0] : "?";
      setText(meta, "PC idle · next window " + firstHour);
    } else {
      setText(meta, pc.why ? "PC busy: " + pc.why : "Waiting for the next tick");
    }
    return;
  }

  const GEN_PHASE = { extracting: "pulling audio and frames", uploading: "sending to the GPU PC", transcribing: "transcribing on the GPU PC", checking: "checking the result" };
  const step = cur.step === "gen" ? "gen" : "find";
  const stepWord = step === "gen" ? "Generating" : "Finding";
  const name = cur.show || cur.title || cur.key || "–";
  let titleText = stepWord + " · " + (cur.library ? cur.library + " › " : "") + name;
  const ep = sxe(cur);
  if (ep) titleText += " " + ep;
  if (cur.show && cur.title) titleText += " · " + cur.title;
  setText(title, titleText);
  if (title) title.title = titleText;

  let phaseWord;
  let metaText;
  let raw = null;
  let indeterminate = false;
  if (step === "gen") {
    const phase = cur.phase || "";
    const ofMs = nn(cur.of_ms);
    const doneMs = Math.min(nn(cur.done_ms), ofMs);
    phaseWord = GEN_PHASE[phase] || phase || "working";
    if (phase === "transcribing" && ofMs > 0) raw = Math.min(100, Math.max(0, pct(doneMs, ofMs)));
    else indeterminate = true;
    metaText = phaseWord + (phase === "transcribing" && ofMs > 0 ? " · " + fmtTime(doneMs) + " of " + fmtTime(ofMs) : "") +
      " · started " + ago(cur.started_at, now) + " ago";
  } else {
    phaseWord = cur.phase === "downloading" ? "downloading" : "searching";
    const of = nn(cur.of);
    const n = Math.min(nn(cur.n), of);
    if (of > 0) raw = Math.min(100, Math.max(0, pct(n, of)));
    else indeterminate = true;
    metaText = phaseWord + " OpenSubtitles · " + n + " of " + of + " this run · " +
      (Number(budget.downloads) || 0) + " of " + (Number(budget.limit) || 0) + " downloads today";
  }
  const maxId = step + "|" + (cur.key || ((cur.show || cur.title || "") + (sxe(cur) || ""))) + "|" + (cur.started_at || "");
  if (!state.nowMax || state.nowMax.id !== maxId) state.nowMax = { id: maxId, pct: 0 };   // a new step or key starts over
  if (raw !== null) {
    raw = Math.max(raw, state.nowMax.pct);
    state.nowMax.pct = raw;
  }
  if (indeterminate) {
    setText(nowPctEl, "");
    if (nowBar) {
      nowBar.hidden = false;
      nowBar.classList.add("is-indeterminate");
      nowBar.removeAttribute("aria-valuenow");
      nowBar.setAttribute("aria-busy", "true");
    }
    if (nowFill) nowFill.style.width = "";
  } else {
    setText(nowPctEl, raw + "%");
    if (nowBar) {
      nowBar.hidden = false;
      nowBar.classList.remove("is-indeterminate");
      nowBar.setAttribute("aria-valuenow", String(raw));
      nowBar.removeAttribute("aria-busy");
    }
    if (nowFill) nowFill.style.width = raw + "%";
  }
  setText(meta, metaText);
}
function renderSwitch() {
  /* The Generate now switch mirrors state.data.pc.generate_now_until: checked while that is in the future, the hint says
     until when (local HH:MM). While a POST is in flight the input is disabled. */
  const box = $("go-now");
  const hint = $("go-now-hint");
  if (!box || state.switchPending) return;
  const pc = (state.data && state.data.pc) || {};
  const until = pc.generate_now_until ? Date.parse(pc.generate_now_until) : NaN;
  const on = Number.isFinite(until) && until > Date.now();
  box.checked = on;
  box.setAttribute("aria-checked", on ? "true" : "false");
  if (on) {
    const dt = new Date(until);
    setText(hint, "On until " + String(dt.getHours()).padStart(2, "0") + ":" + String(dt.getMinutes()).padStart(2, "0") + ". A game, a busy GPU or your model still wins.");
  } else {
    setText(hint, "Ignores the idle hours for 6 h. A game, a busy GPU or your model still wins.");
  }
}
async function toggleGenerateNow(on) {
  const box = $("go-now");
  const hint = $("go-now-hint");
  state.switchPending = true;
  if (box) box.disabled = true;
  let failMsg = null;
  try {
    const res = await api("/pc", { generate_now: !!on, hours: 6 });
    if (!res || res.status !== 202) {
      failMsg = (res && res.data && res.data.why) ? res.data.why : "Could not change it. Try again.";
    } else {
      const data = res.data || {};
      const until = data.until != null ? data.until : data.generate_now_until;
      if (until != null) {
        if (!state.data) state.data = {};
        state.data.pc = Object.assign({}, state.data.pc || {}, { generate_now_until: until });
      }
    }
  } catch (e) {
    failMsg = "Could not reach Subs. Try again.";
  } finally {
    state.switchPending = false;
    if (box) box.disabled = false;
    renderSwitch();
    if (failMsg) setText(hint, failMsg);
  }
}
function renderRecent() {
  /* .recent-row per state.data.recent entry: .recent-time (local HH:MM), .recent-what "<step> · <show or title> SxxEyy",
     .recent-result "<result>" plus " · <why>" when present. */
  const list = $("recent-list");
  if (!list) return;
  const d = state.data;
  const recent = d && Array.isArray(d.recent) ? d.recent : [];
  list.textContent = "";
  recent.forEach(function (r) {
    const row = el("li", "recent-row");
    let stamp = "–";
    if (r && r.at) {
      const dt = new Date(r.at);
      if (!isNaN(dt.getTime())) stamp = String(dt.getHours()).padStart(2, "0") + ":" + String(dt.getMinutes()).padStart(2, "0");
    }
    row.appendChild(el("span", "recent-time", stamp));
    const name = (r && (r.show || r.title || r.key)) || "–";
    const what = (r && r.step ? r.step : "step") + " · " + name + (sxe(r) ? " " + sxe(r) : "");
    row.appendChild(el("span", "recent-what", what));
    let result = r && r.result != null ? String(r.result) : "";
    if (r && r.why) result += " · " + r.why;
    row.appendChild(el("span", "recent-result", result));
    list.appendChild(row);
  });
}
function openSheet(onSend) {
  /* Show #sheet with an empty textarea; Queue it calls onSend(text) (a promise; an error message goes to #sheet-error,
     success closes); Cancel closes; ignore Escape while a send is pending. */
  const dlg = $("sheet");
  const ta = $("sheet-text");
  const err = $("sheet-error");
  const sendBtn = $("sheet-send");
  const cancelBtn = $("sheet-cancel");
  state.sheetOnSend = typeof onSend === "function" ? onSend : null;
  state.sheetPending = false;
  if (err) { setText(err, ""); err.hidden = true; }
  if (ta) ta.value = "";
  if (sendBtn) sendBtn.disabled = false;
  if (cancelBtn) cancelBtn.disabled = false;
  if (!state.sheetBound) {
    state.sheetBound = true;
    const form = $("sheet-form");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (state.sheetPending) return;
      const fn = state.sheetOnSend;
      if (!fn) { if (dlg && dlg.open) dlg.close(); return; }
      const text = ta ? ta.value.trim() : "";
      state.sheetPending = true;
      if (sendBtn) sendBtn.disabled = true;
      if (cancelBtn) cancelBtn.disabled = true;
      if (err) { setText(err, ""); err.hidden = true; }
      Promise.resolve().then(function () { return fn(text); }).then(function () {
        state.sheetPending = false;
        state.sheetOnSend = null;
        if (sendBtn) sendBtn.disabled = false;
        if (cancelBtn) cancelBtn.disabled = false;
        if (dlg && dlg.open) dlg.close();
      }).catch(function (e2) {
        state.sheetPending = false;
        if (sendBtn) sendBtn.disabled = false;
        if (cancelBtn) cancelBtn.disabled = false;
        if (err) {
          setText(err, e2 && e2.message ? e2.message : String(e2 || "Could not queue it."));
          err.hidden = false;
        }
      });
    });
    if (dlg) {
      dlg.addEventListener("cancel", function (e) { if (state.sheetPending) e.preventDefault(); });
      dlg.addEventListener("close", function () { state.sheetPending = false; state.sheetOnSend = null; });
    }
  }
  if (dlg && !dlg.open) dlg.showModal();
  if (ta) ta.focus();
}

/* ------------------------------------------------------------------ polling */
async function load() {
  /* GET /api/state; on success state.data = data, hide #unavailable, render coverage, progress, recent and the badge, then renderReview() when it exists (subs-views.js);
     on a network failure show #unavailable and keep the last data. Schedule the next poll in 10 s while state.data.current is
     present, else POLL_MS (single timer). */
  if (state.inFlight) return;
  state.inFlight = true;
  try {
    const res = await api("/state");
    const ok = res && res.status === 200 && res.data && typeof res.data === "object" &&
      !Array.isArray(res.data) && Object.prototype.hasOwnProperty.call(res.data, "coverage");
    if (ok) {
      state.data = res.data;
      state.down = false;
      const un = $("unavailable");
      if (un) un.hidden = true;
      renderCoverage();
      renderProgress();
      renderSwitch();
      renderRecent();
      if (typeof renderReview === "function") renderReview();
    } else {
      state.down = true;
      const un = $("unavailable");
      if (un) un.hidden = false;
    }
  } catch (e) {
    state.down = true;
    const un = $("unavailable");
    if (un) un.hidden = false;
  } finally {
    state.inFlight = false;
    clearTimeout(state.timer);
    const delay = state.data && state.data.current ? 10000 : POLL_MS;
    state.timer = setTimeout(load, delay);
  }
}
function boot() {
  $("missing-q").addEventListener("input", () => { state.query = $("missing-q").value; loadMissing(); });
  $("sheet-cancel").addEventListener("click", () => $("sheet").close());
  const go = $("go-now");
  if (go) go.addEventListener("change", () => toggleGenerateNow(go.checked));
  load();
  if (typeof loadMissing === "function") loadMissing();
  renderNav();
}
document.addEventListener("DOMContentLoaded", boot);
