/* Subs page, part 2: Needs a look and Missing (loaded after subs.js, which provides $, el, setText, ago, pct, fmtTime, sxe, api,
   state, STATUS_WORDS, openSheet, load). Same class vocabulary as subs.js; all DOM through el() and text nodes. */
"use strict";
let searchTimer = null;
let searchBound = false;
let searchSeq = 0;
let showSeq = 0;
const inFlight = new Set();
function pendingSet(extra) { const set = new Set(state && state.pending ? [...state.pending] : []); if (extra) extra.forEach((v) => set.add(v)); return set; }
function hasKey(set, key) { return set.has(key) || set.has(String(key)); }
function setNote(text) { const note = $("missing-note"); if (!note) return; setText(note, text || ""); note.hidden = !text; }
function targetToken(target) {
  if (target && typeof target === "object") return String(target.key != null ? target.key : target.show);
  return String(target);
}
function actionButtons(row) { return row ? Array.from(row.querySelectorAll(".missing-actions .subs-btn, .ep-actions .subs-btn")) : []; }
function setButtonsDisabled(buttons, disabled) { buttons.forEach((button) => { button.disabled = disabled; }); }
function findRow(token) {
  const shows = Array.from(document.querySelectorAll("#missing-list .missing-row")).find((node) => String(node.dataset.id) === String(token));
  if (shows) return shows;
  return Array.from(document.querySelectorAll("#missing-list .ep-row")).find((node) => node.dataset.key != null && String(node.dataset.key) === String(token)) || null;
}
function rowError(row, why) {
  if (!row || !row.isConnected) return false;
  const text = why || "Could not queue that.";
  const prior = row.querySelector(".subs-error");
  if (prior) { setText(prior, text); return true; }
  row.append(el("p", "subs-error", text));
  return true;
}
function safePlexUrl(raw) {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && url.hostname === "app.plex.tv" ? url.href : null;
  } catch (error) { return null; }
}

function reviewCard(row, pending) {
  const card = el("li", "review-card");
  card.dataset.key = row.key;
  const saving = hasKey(pending, row.key);
  const tag = row.season != null || row.episode != null ? sxe(row) : "";
  const confidence = Number(row.confidence);
  const head = el("div", "review-head");
  head.append(el("div", "review-title", tag ? (row.show || row.title || "") + " · " + tag : (row.title || row.show || "")));
  const fromWhisper = typeof row.source === "string" && row.source.startsWith("gen:");
  const meta = [row.library, row.source,
    (fromWhisper ? "confidence " : "source score ") + (Number.isFinite(confidence) ? confidence.toFixed(2) : (row.confidence ?? "—")), row.lang].filter(Boolean);
  head.append(el("div", "review-meta", meta.join(" · ")));
  card.append(head);
  if (row.why) card.append(el("p", "review-why", row.why));
  if (Array.isArray(row.preview) && row.preview.length) {
    const preview = el("ul", "review-preview");
    row.preview.forEach((cue) => {
      if (!Array.isArray(cue)) return;
      const line = el("li", "review-cue");
      line.append(el("span", "review-time", fmtTime(cue[0])), document.createTextNode(" " + (cue[2] == null ? "" : String(cue[2]).replace(/<\/?[ib]>/g, ""))));   // the <i>/<b> the gate allows are not shown raw
      preview.append(line);
    });
    card.append(preview);
  }
  const actions = el("div", "review-actions");
  if (saving) { card.classList.add("is-saving"); actions.append(el("span", "review-saving", "saving…")); }
  const off = Number(row.offset_ms);
  const canShift = Number.isFinite(off) && Math.abs(off) >= 500;
  const buttons = canShift
    ? [["Fix timing · " + (Math.abs(off) / 1000).toFixed(1) + " s " + (off < 0 ? "earlier" : "later"), "shift", "subs-btn subs-primary"],
       ["Keep as is", "keep", "subs-btn"], ["Regenerate…", "regenerate", "subs-btn"], ["Delete", "delete", "subs-btn"]]
    : [["Keep", "keep", "subs-btn subs-primary"], ["Regenerate…", "regenerate", "subs-btn"], ["Delete", "delete", "subs-btn"]];
  buttons.forEach(([label, decision, cls]) => {
      const button = el("button", cls, label);
      button.type = "button"; button.disabled = saving;
      button.addEventListener("click", () => decide(row, decision));
      actions.append(button);
    });
  const plexHref = safePlexUrl(row.plex_url);
  if (plexHref) {
    const link = el("a", "subs-btn review-plex", "Open in Plex");
    link.href = plexHref; link.target = "_blank"; link.rel = "noopener noreferrer";
    actions.append(link);
  }
  card.append(actions);
  return card;
}

function renderReview() {
  /* One .review-card <li> per state.data.review row: .review-head with .review-title "<show> · SxxEyy" (or the movie title)
     and .review-meta "<library> · <source> · confidence 0.NN · <lang>"; .review-why when why; .review-preview: a scrollable
     list of .review-cue lines "<.review-time mm:ss> text" from row.preview; .review-actions: Keep (filled .subs-primary),
     Regenerate…, Delete, and Open in Plex (<a target=_blank rel=noopener>) only when row.plex_url parses as an https
     app.plex.tv URL; with a measured row.offset_ms the first (filled) button is "Fix timing · N s earlier|later" ->
     decision "shift" and Keep becomes "Keep as is". Buttons call decide(row, name). Cards for keys in state.pending show
     "saving…" and disabled buttons; a pending key the poll no longer lists is dropped. Empty: #review-empty = "Nothing to
     look at. The PC generates when it is idle, next window <hour>." */
  const list = $("review-list");
  const empty = $("review-empty");
  const items = state && state.data && Array.isArray(state.data.review) ? state.data.review : [];
  const live = new Set(items.map((row) => String(row && row.key)));
  if (state && state.pending && typeof state.pending.delete === "function") {
    Array.from(state.pending).forEach((key) => { if (!live.has(String(key))) state.pending.delete(key); });
  }
  const pending = pendingSet();
  if (list) list.textContent = "";
  if (empty) {
    empty.hidden = items.length !== 0;
    if (!items.length) {
      const raw = state && state.data && (state.data.next_window ?? state.data.nextWindow ?? state.data.window ?? state.data.next ?? state.data.hour);
      const m = raw == null ? null : String(raw).match(/(\d{1,2}):(\d{2})/);
      const hour = raw == null || raw === "" ? "soon" : typeof raw === "number" ? String(raw).padStart(2, "0") + ":00" : m ? m[1].padStart(2, "0") + ":" + m[2] : String(raw);
      setText(empty, "Nothing to look at. The PC generates when it is idle, next window " + hour + ".");
    }
  }
  if (list) items.forEach((row) => list.append(reviewCard(row, pending)));
}

async function sendDecision(key, decision, note) {
  if (state && state.pending && typeof state.pending.add === "function") state.pending.add(key);
  renderReview();
  const body = { key: key, decision: decision };
  if (note) body.note = note;
  const res = await api("/decision", body).catch(() => null);
  if (res && res.status >= 200 && res.status < 300) { renderReview(); return; }
  if (state && state.pending && typeof state.pending.delete === "function") state.pending.delete(key);
  renderReview();
  const card = Array.from(document.querySelectorAll("#review-list .review-card")).find((node) => node.dataset.key === String(key));
  if (card) card.append(el("p", "review-error subs-error", (res && res.data && res.data.why) || "Could not save that decision."));
}

async function decide(row, decision) {
  /* "regenerate" opens the sheet first (openSheet) and posts with the note; others POST /decision {key, decision} at once.
     Mark the key pending until the next poll removes the card; show data.why inline on a non-2xx. */
  if (!row || row.key == null) return;
  if (decision === "regenerate") { openSheet(async (note) => { await sendDecision(row.key, decision, note); }); return; }
  await sendDecision(row.key, decision);
}

function bindSearch() {
  if (searchBound) return;
  const input = $("missing-q");
  if (!input) return;
  searchBound = true;
  input.addEventListener("input", () => {
    state.query = input.value.trim();
    if (searchTimer !== null) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { searchTimer = null; loadMissing(); }, 300);
  });
}

function findEpisodes(id) {
  const row = Array.from(document.querySelectorAll("#missing-list .missing-row")).find((node) => String(node.dataset.id) === String(id));
  return row ? row.querySelector(".episodes") : null;
}
function expandRow(id) { const host = findEpisodes(id); if (!host) return; host.hidden = false; state.openShow = id; loadShow(id, host); }

function missingRow(result, queued) {
  const row = el("li", "missing-row");
  row.dataset.id = result.id;
  const numbers = [];
  if (result.missing_en) numbers.push(result.missing_en + " missing English");
  if (result.missing_es) numbers.push(result.missing_es + " missing Spanish");
  if (result.burned_in) numbers.push(result.burned_in + " burned-in");
  row.append(el("div", "missing-name", result.name || ""), el("p", "missing-nums", numbers.join(" · ")));
  const pending = hasKey(queued, result.id);
  const busy = inFlight.has(targetToken({ show: result.id }));
  const actions = el("div", "missing-actions");
  ["find", "generate"].forEach((kind) => {
    const button = el("button", "subs-btn", pending ? "queued" : (kind === "find" ? "Find" : "Generate"));
    button.type = "button"; button.disabled = pending || busy;
    button.addEventListener("click", (event) => { event.stopPropagation(); request(kind, { show: result.id }, row); });
    actions.append(button);
  });
  row.append(actions);
  const episodes = el("ul", "episodes");
  episodes.hidden = true; episodes.dataset.show = result.id;
  row.append(episodes);
  row.addEventListener("click", (event) => {
    if (event.target.closest(".missing-actions") || event.target.closest(".episodes")) return;
    if (!episodes.hidden) {
      episodes.hidden = true;
      showSeq += 1;
      if (String(state.openShow) === String(result.id)) state.openShow = null;
      return;
    }
    expandRow(result.id);
  });
  return row;
}

async function loadMissing() {
  /* GET /api/missing?q=<state.query>; render .missing-row per result: .missing-name "<name>", .missing-nums "<missing_en>
     missing English · <missing_es> missing Spanish · <burned_in> burned-in" (omit zero parts), .missing-actions with Find and
     Generate (disabled + "queued" when result.id is in data.pending, disabled while its POST is in flight), tapping the row
     toggles its .episodes list loaded by loadShow(id). Show at most 50; #missing-note says "Showing the first 50; narrow the
     search." when results.length == 50 or "Nothing missing matches." when empty. Debounce typing by 300 ms; only the latest
     request may render (searchSeq). */
  bindSearch();
  const seq = ++searchSeq;
  const res = await api("/missing?q=" + encodeURIComponent((state && state.query) || "")).catch(() => null);
  if (seq !== searchSeq) return;
  if (!res || res.status < 200 || res.status >= 300) { setNote("Could not load the missing list."); return; }
  const data = res.data || {};
  const results = Array.isArray(data.results) ? data.results : [];
  const queued = pendingSet(Array.isArray(data.pending) ? data.pending : []);
  const list = $("missing-list");
  if (list) list.textContent = "";
  if (list) results.slice(0, 50).forEach((result) => list.append(missingRow(result, queued)));
  if (results.length === 0) setNote("Nothing missing matches.");
  else if (results.length >= 50) setNote("Showing the first 50; narrow the search.");
  else setNote("");
  if (state.openShow != null && results.some((result) => String(result.id) === String(state.openShow))) expandRow(state.openShow);
  else if (state.openShow != null) state.openShow = null;
}

function episodeRow(ep, pending) {
  const row = el("li", "ep-row");
  if (ep.key != null) row.dataset.key = ep.key;
  const tag = sxe(ep);
  row.append(el("div", "ep-name", tag ? tag + " · " + (ep.title || "") : (ep.title || "")));
  const en = ep.en || "unknown";
  row.append(el("span", "ep-status status-" + en, STATUS_WORDS[ep.en] || en));
  if (ep.es && ep.es !== "have") row.append(el("span", "ep-status ep-status-es small status-" + ep.es, STATUS_WORDS[ep.es] || ep.es));
  const needsWork = (status) => status === "missing" || status === "review";
  if (needsWork(ep.en) || needsWork(ep.es)) {
    const queued = hasKey(pending, ep.key);
    const busy = inFlight.has(targetToken({ key: ep.key }));
    const actions = el("div", "ep-actions");
    ["find", "generate"].forEach((kind) => {
      const button = el("button", "subs-btn", queued ? "queued" : (kind === "find" ? "Find" : "Generate"));
      button.type = "button"; button.disabled = queued || busy;
      button.addEventListener("click", (event) => { event.stopPropagation(); request(kind, { key: ep.key }, row); });
      actions.append(button);
    });
    row.append(actions);
  }
  return row;
}

async function loadShow(id, container) {
  /* GET /api/show/<id>; .ep-row per episode: .ep-name "SxxEyy · <title>", .ep-status with class status-<en> and the word from
     STATUS_WORDS, a second small status for es when it is not 'have', and Find/Generate buttons (POST /request {kind, key})
     only when a language is 'missing' or 'review'. Only the latest expansion renders (showSeq). */
  const host = container || findEpisodes(id);
  if (!host) return;
  const seq = ++showSeq;
  const res = await api("/show/" + encodeURIComponent(id)).catch(() => null);
  if (seq !== showSeq) return;
  if (!res || res.status < 200 || res.status >= 300) return;
  const data = res.data || { id: id, episodes: [] };
  state.showData = data;
  state.openShow = data.id != null ? data.id : id;
  const pending = pendingSet();
  host.textContent = "";
  (Array.isArray(data.episodes) ? data.episodes : []).forEach((ep) => host.append(episodeRow(ep, pending)));
}

async function request(kind, target, row) {
  /* POST /api/request; one POST per target at a time (module inFlight set; its buttons stay disabled until the reply).
     202 adds target to state.pending and refreshes; 409 refreshes without marking it queued; any other reply re-enables the
     buttons and shows data.why next to the row. */
  const body = target && typeof target === "object" ? { kind: kind, ...target }
    : typeof target === "number" ? { kind: kind, show: target } : { kind: kind, key: target };
  const token = targetToken(target);
  if (inFlight.has(token)) return;
  const buttons = actionButtons(row);
  inFlight.add(token);
  setButtonsDisabled(buttons, true);
  const res = await api("/request", body).catch(() => null);
  const data = (res && res.data) || {};
  const status = res ? Number(res.status) : 0;
  inFlight.delete(token);
  if (status === 202) {
    if (state && state.pending && typeof state.pending.add === "function") state.pending.add(token);
    await loadMissing();
    return;
  }
  setButtonsDisabled(buttons, false);
  if (status === 409) {
    if (state && state.pending && typeof state.pending.delete === "function") state.pending.delete(token);
    await loadMissing();
  }
  const why = data.why || (status === 409 ? "That request is already queued." : "Could not queue that.");
  const host = row && row.isConnected ? row : findRow(token);
  if (!rowError(host, why)) setNote(why);
}
