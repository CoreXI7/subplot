"use strict";
/*
 * Subplot — Settings block (static/settings.js).
 *
 * Class vocabulary used here (styled in subs.css):
 *   settings-body, set-group, set-help, set-field, set-label, set-input,
 *   set-origin, is-changed, set-reset, set-hint, set-error, set-chips,
 *   set-chip, set-chip-x, is-locked, set-add, set-hours, set-hour-row,
 *   set-actions, set-status, set-history.
 *
 * Global helpers from subs.js (reused, never redefined): $, el, setText, ago, api.
 * Everything below is plain ES2020, built with el()/setText/document.createElement
 * only (no HTML strings), and talks only to /api/settings and /api/state.
 */

const SET_SECTIONS = ["plex", "languages", "thresholds", "find", "generate", "pc_idle", "priority"];

const SET_DAY_CHOICES = [
  ["", "Every day"],
  ["mon-fri", "Mon-Fri"],
  ["sat-sun", "Sat-Sun"],
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"]
];

const SET_GROUPS = [
  {
    title: "Languages",
    help: "Which subtitle languages are made, and who gets a second one.",
    fields: [
      { key: "languages.primary", label: "Main language", type: "text" },
      { key: "languages.secondary", label: "Second language", type: "text" },
      { key: "languages.secondary_for_users", label: "Second-language viewers", type: "list" }
    ]
  },
  {
    title: "Downloads",
    help: "How much is searched each day and how sure a match has to be.",
    fields: [
      { key: "find.daily_budget", label: "Downloads a day", type: "number", min: 1, max: 1000 },
      { key: "find.min_score", label: "Lowest match score", type: "number", min: 1, max: 1000, strict: "down" },
      { key: "find.retry_days", label: "Search again after (days)", type: "number", min: 1, max: 1000 }
    ]
  },
  {
    title: "Timing",
    help: "How far out of step a subtitle may be before its timing is fixed.",
    fields: [
      { key: "find.sync_max_offset_ms", label: "Largest timing error (ms)", type: "number", min: 1, max: 100000, strict: "up" },
      { key: "find.auto_shift_max_ms", label: "Largest automatic shift (ms)", type: "number", min: 0, max: 600000, strict: "up" }
    ]
  },
  {
    title: "Quality",
    help: "When a subtitle is kept without asking, and when it needs a look.",
    fields: [
      { key: "thresholds.keep", label: "Keep at", type: "number", min: 0, max: 1, step: 0.01, strict: "down" },
      { key: "thresholds.review", label: "Ask me at", type: "number", min: 0, max: 1, step: 0.01, strict: "down" }
    ]
  },
  {
    title: "Generating",
    help: "The speech model and the limits for one night's work.",
    fields: [
      { key: "generate.model", label: "Whisper model", type: "text" },
      { key: "generate.max_hours_per_night", label: "Hours a night", type: "number", min: 1, max: 24 },
      { key: "generate.burned_in_frames", label: "Frames checked for burned-in text", type: "number", min: 4, max: 48 },
      { key: "generate.burned_in_ratio", label: "Burned-in if this share has text", type: "number", min: 0, max: 1, step: 0.01 },
      { key: "generate.es_review_until_kept", label: "Ask me about Spanish until kept", type: "number", min: 0, max: 1000 }
    ]
  },
  {
    title: "When the PC is free",
    help: "Only work in these hours, and unload your chat model to free the GPU.",
    fields: [
      { key: "pc_idle.hours", label: "Free hours", type: "hours" },
      { key: "pc_idle.gpu_util_below", label: "GPU busy above (%)", type: "number", min: 0, max: 100 },
      { key: "pc_idle.vram_free_mb", label: "Free video memory needed (MB)", type: "number", min: 0, max: 100000 },
      { key: "pc_idle.unload_after_min", label: "Unload an idle model after (minutes)", type: "number", min: 0, max: 720, needsOllama: true }
    ]
  },
  {
    title: "Programs",
    help: "Programs that pause the work, that get priority, and that are not games.",
    fields: [
      { key: "pc_idle.no_processes", label: "Programs that block", type: "list" },
      { key: "pc_idle.yield_to", label: "Programs it makes room for", type: "list" },
      { key: "pc_idle.not_games", label: "Programs that are not games", type: "list" }
    ]
  },
  {
    title: "Order",
    help: "Which titles and libraries come first.",
    fields: [
      { key: "priority.watched_days", label: "Recently watched means (days)", type: "number", min: 1, max: 365 },
      { key: "priority.libraries", label: "Library order", type: "list", libs: true }
    ]
  },
  {
    title: "Libraries",
    help: "Whole libraries whose videos are left alone.",
    fields: [
      { key: "exclude_libraries", label: "Excluded libraries", type: "list", libs: true, locked: true }
    ]
  },
  {
    title: "Plex",
    help: "The server id used for the Open in Plex links.",
    fields: [
      { key: "plex.machine_id", label: "Plex server id", type: "text" }
    ]
  }
];

const SET_STATE = {
  data: null,
  version: 0,
  file: {},
  baseline: {},
  current: {},
  refs: {},
  ui: null,
  coverage: null,
  loading: false,
  saving: false,
  loaded: false
};

function setKeyId(key) {
  return "set-" + key.replace(/[^A-Za-z0-9]+/g, "-");
}

function setCopy(value) {
  return Array.isArray(value) ? value.slice() : value;
}

function setList(value) {
  return Array.isArray(value) ? value.map(String) : [];
}

function setNormalize(field, value) {
  if (field.type === "number") {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  if (field.type === "text") return value === null || value === undefined ? "" : String(value);
  return Array.isArray(value) ? value.map(String) : [];
}

function setShow(value) {
  if (value === null || value === undefined) return "nothing";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function setCanon(value) {
  return JSON.stringify(value);
}

function setDotSet(target, key, value) {
  const parts = key.split(".");
  let node = target;
  let i;
  for (i = 0; i < parts.length - 1; i++) {
    if (!node[parts[i]] || typeof node[parts[i]] !== "object") node[parts[i]] = {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
}

function setUnusedSentence(data) {
  const dropped = Array.isArray(data.dropped) ? data.dropped : [];
  const ignored = data.ignored || null;
  if (!dropped.length && !ignored) return "";
  const keys = dropped.length ? dropped.join(", ") : "page settings";
  return "Some saved settings were not used because the server file is stricter: " + keys + ".";
}

function setParseHours(text) {
  const raw = String(text);
  const m = /^(?:([a-z]+(?:-[a-z]+)?)\s+)?([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})$/.exec(raw.trim().toLowerCase());
  if (!m) return { parsed: false, raw: raw };
  const days = m[1] || "";
  const known = SET_DAY_CHOICES.some(function (choice) { return choice[0] === days; });
  if (!known) return { parsed: false, raw: raw };
  const h1 = Number(m[2]);
  const n1 = Number(m[3]);
  const h2 = Number(m[4]);
  const n2 = Number(m[5]);
  if (h1 > 23 || h2 > 23 || n1 > 59 || n2 > 59) return { parsed: false, raw: raw };
  return { parsed: true, day: days, start: m[2] + ":" + m[3], end: m[4] + ":" + m[5] };
}

function setValidateHourRow(row) {
  if (!row.parsed) return;
  const bad = !row.start || !row.end || row.end <= row.start;
  row.bad = bad;
  if (row.el && row.el.error) {
    row.el.error.hidden = !bad;
    setText(row.el.error, bad ? "For a range past midnight add two rows" : "");
    if (row.el.end) row.el.end.setAttribute("aria-invalid", bad ? "true" : "false");
  }
}

function setBuildGroup(body, group) {
  const fieldset = el("fieldset", "set-group");
  const legend = document.createElement("legend");
  setText(legend, group.title);
  fieldset.appendChild(legend);
  const help = el("p", "set-help");
  setText(help, group.help);
  fieldset.appendChild(help);
  group.fields.forEach(function (f) {
    setBuildField(fieldset, f);
  });
  body.appendChild(fieldset);
}

function setBuildField(fs, f) {
  const wrap = el("div", "set-field");
  const ref = {
    f: f,
    wrap: wrap,
    id: setKeyId(f.key),
    input: null,
    origin: null,
    reset: null,
    error: null,
    chips: null,
    hours: null,
    rows: null
  };
  SET_STATE.refs[f.key] = ref;

  const label = document.createElement("label");
  label.className = "set-label";
  label.htmlFor = ref.id;
  setText(label, f.label);
  wrap.appendChild(label);

  if (f.type === "hours") {
    setBuildHours(wrap, f, ref);
  } else if (f.type === "list") {
    setBuildList(wrap, f, ref);
  } else {
    setBuildScalar(wrap, f, ref);
  }

  const origin = el("span", "set-origin");
  wrap.appendChild(origin);
  ref.origin = origin;

  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "set-reset";
  setText(reset, "Reset");
  reset.setAttribute("aria-label", "Reset " + f.label + " to the server file value");
  reset.addEventListener("click", function () {
    setResetField(f);
  });
  wrap.appendChild(reset);
  ref.reset = reset;

  if (f.strict) {
    const hint = el("p", "set-hint");
    const fileValue = SET_STATE.file[f.key];
    if (f.strict === "down") {
      setText(hint, "The server file allows down to " + setShow(fileValue) + "; lower it there.");
    } else {
      setText(hint, "The server file allows up to " + setShow(fileValue) + "; raise it there.");
    }
    wrap.appendChild(hint);
  }

  if (f.needsOllama && !SET_STATE.data.ollama_set) {
    const hint = el("p", "set-hint");
    setText(hint, "Set the Ollama address in rules.json on the server to use this");
    wrap.appendChild(hint);
    if (ref.reset) ref.reset.disabled = true;
  }

  const error = el("p", "set-error subs-error");
  error.hidden = true;
  wrap.appendChild(error);
  ref.error = error;

  fs.appendChild(wrap);

  if (f.type === "list") setRenderChips(f);
}

function setBuildScalar(wrap, f, ref) {
  const input = document.createElement("input");
  input.className = "set-input";
  input.id = ref.id;
  input.type = f.type === "number" ? "number" : "text";
  if (f.type === "number") {
    if (f.min !== undefined) input.min = String(f.min);
    if (f.max !== undefined) input.max = String(f.max);
    input.step = String(f.step !== undefined ? f.step : 1);
    input.inputMode = "numeric";
  }
  if (f.needsOllama && !SET_STATE.data.ollama_set) {
    input.disabled = true;
    input.placeholder = "never";
  }
  const value = SET_STATE.current[f.key];
  input.value = value === null || value === undefined ? "" : String(value);
  input.addEventListener("input", function () {
    setReadScalar(f, input);
    setClearError(f.key);
    setRefreshField(f.key);
  });
  ref.input = input;
  wrap.appendChild(input);
}

function setReadScalar(f, input) {
  const raw = input.value.trim();
  if (f.type === "text") {
    SET_STATE.current[f.key] = raw;
    return;
  }
  if (raw === "") {
    SET_STATE.current[f.key] = null;
    return;
  }
  const n = Number(raw);
  SET_STATE.current[f.key] = Number.isFinite(n) ? n : raw;
}

function setBuildList(wrap, f, ref) {
  const chips = el("div", "set-chips");
  ref.chips = chips;
  wrap.appendChild(chips);

  const input = document.createElement("input");
  input.className = "set-input";
  input.id = ref.id;
  input.type = "text";
  input.autocomplete = "off";
  if (f.libs) input.setAttribute("list", "set-libs");
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      ev.preventDefault();
      setAddList(f, input);
    }
  });
  ref.input = input;
  const row = el("div", "set-add");
  row.appendChild(input);

  const add = document.createElement("button");
  add.type = "button";
  add.className = "subs-btn";
  setText(add, "Add");
  add.setAttribute("aria-label", "Add to " + f.label);
  add.addEventListener("click", function () {
    setAddList(f, input);
  });
  row.appendChild(add);
  wrap.appendChild(row);
}

function setAddList(f, input) {
  const value = input.value.trim();
  if (!value) return;
  const list = SET_STATE.current[f.key] || [];
  if (list.indexOf(value) < 0) {
    list.push(value);
    SET_STATE.current[f.key] = list;
  }
  input.value = "";
  setRenderChips(f);
  setClearError(f.key);
  setRefreshField(f.key);
}

function setRenderChips(f) {
  const ref = SET_STATE.refs[f.key];
  if (!ref || !ref.chips) return;
  const chips = ref.chips;
  chips.replaceChildren();

  if (f.locked && SET_STATE.data) {
    setList(SET_STATE.data.locked_libraries).forEach(function (name) {
      const chip = el("span", "set-chip is-locked");
      setText(chip, name + " (always excluded)");
      chips.appendChild(chip);
    });
  }

  (SET_STATE.current[f.key] || []).forEach(function (name) {
    const chip = el("span", "set-chip");
    setText(chip, name);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "set-chip-x";
    setText(remove, "×");
    remove.setAttribute("aria-label", "Remove " + name);
    remove.addEventListener("click", function () {
      const list = SET_STATE.current[f.key] || [];
      SET_STATE.current[f.key] = list.filter(function (entry) { return entry !== name; });
      setRenderChips(f);
      setClearError(f.key);
      setRefreshField(f.key);
    });
    chip.appendChild(remove);
    chips.appendChild(chip);
  });
}

function setBuildHours(wrap, f, ref) {
  const box = el("div", "set-hours");
  ref.hours = box;
  ref.rows = [];
  wrap.appendChild(box);

  (SET_STATE.current[f.key] || []).forEach(function (text) {
    const row = setParseHours(text);
    ref.rows.push(row);
    setAppendHourRow(f, ref, row);
  });

  const add = document.createElement("button");
  add.type = "button";
  add.className = "subs-btn";
  setText(add, "+ Add hours");
  add.addEventListener("click", function () {
    const row = { parsed: true, day: "", start: "00:00", end: "06:00" };
    ref.rows.push(row);
    setAppendHourRow(f, ref, row);
    setSyncHours(f, ref);
    setRefreshField(f.key);
  });
  add.id = ref.id;
  wrap.appendChild(add);
  ref.input = add;
}

function setRemoveHour(f, ref, row) {
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "set-chip-x";
  setText(remove, "×");
  remove.setAttribute("aria-label", "Remove hours range");
  remove.addEventListener("click", function () {
    const index = ref.rows.indexOf(row);
    if (index >= 0) ref.rows.splice(index, 1);
    const line = remove.parentNode;
    if (line && line.parentNode) line.parentNode.removeChild(line);
    setSyncHours(f, ref);
    setRefreshField(f.key);
  });
  return remove;
}

function setAppendHourRow(f, ref, row) {
  const line = el("div", "set-hour-row");

  if (!row.parsed) {
    const raw = document.createElement("input");
    raw.type = "text";
    raw.className = "set-input";
    raw.readOnly = true;
    raw.value = row.raw;
    line.appendChild(raw);
    line.appendChild(setRemoveHour(f, ref, row));
    ref.hours.appendChild(line);
    row.el = { input: raw };
    return;
  }

  const select = document.createElement("select");
  select.className = "set-input";
  select.setAttribute("aria-label", "Days");
  SET_DAY_CHOICES.forEach(function (choice) {
    const opt = document.createElement("option");
    opt.value = choice[0];
    setText(opt, choice[1]);
    if (choice[0] === row.day) opt.selected = true;
    select.appendChild(opt);
  });

  const start = document.createElement("input");
  start.type = "time";
  start.className = "set-input";
  start.value = row.start;
  start.setAttribute("aria-label", "Start");

  const end = document.createElement("input");
  end.type = "time";
  end.className = "set-input";
  end.value = row.end;
  end.setAttribute("aria-label", "End");

  const error = el("p", "set-error subs-error");
  error.hidden = true;

  select.addEventListener("change", function () {
    row.day = select.value;
    setSyncHours(f, ref);
    setRefreshField(f.key);
  });
  start.addEventListener("input", function () {
    row.start = start.value;
    setValidateHourRow(row);
    setSyncHours(f, ref);
    setRefreshField(f.key);
  });
  end.addEventListener("input", function () {
    row.end = end.value;
    setValidateHourRow(row);
    setSyncHours(f, ref);
    setRefreshField(f.key);
  });

  line.appendChild(select);
  line.appendChild(start);
  line.appendChild(end);
  line.appendChild(setRemoveHour(f, ref, row));
  line.appendChild(error);
  ref.hours.appendChild(line);

  row.el = { select: select, start: start, end: end, error: error };
  setValidateHourRow(row);
}

function setSyncHours(f, ref) {
  SET_STATE.current[f.key] = ref.rows.map(function (row) {
    if (!row.parsed) return row.raw;
    return (row.day ? row.day + " " : "") + row.start + "-" + row.end;
  });
}

function setRenderHours(f, ref) {
  if (!ref.hours) return;
  ref.hours.replaceChildren();
  ref.rows = [];
  (SET_STATE.current[f.key] || []).forEach(function (text) {
    const row = setParseHours(text);
    ref.rows.push(row);
    setAppendHourRow(f, ref, row);
  });
}

function setRefreshField(key) {
  const ref = SET_STATE.refs[key];
  if (!ref) return;
  const f = ref.f;
  const current = SET_STATE.current[key];
  const fileValue = key === "exclude_libraries" ? [] : SET_STATE.file[key];
  const changed = setCanon(current) !== setCanon(setNormalize(f, fileValue));
  if (ref.origin) {
    setText(ref.origin, changed ? "changed here" : "from the server file");
    ref.origin.classList.toggle("is-changed", changed);
  }
  setUpdateSave();
}

function setRefreshAll() {
  Object.keys(SET_STATE.refs).forEach(function (key) {
    setRefreshField(key);
  });
  setUpdateSave();
}

function setUpdateSave() {
  const ui = SET_STATE.ui;
  if (!ui) return;
  const dirty = setCanon(SET_STATE.current) !== setCanon(SET_STATE.baseline);
  if (ui.save) ui.save.disabled = !dirty || SET_STATE.saving;
  if (ui.resetAll) ui.resetAll.disabled = SET_STATE.saving || !!ui.resetAll.dataset.asking;
}

function setResetField(f) {
  if (f.key === "exclude_libraries") {
    SET_STATE.current[f.key] = [];
  } else {
    SET_STATE.current[f.key] = setNormalize(f, SET_STATE.file[f.key]);
  }
  const ref = SET_STATE.refs[f.key];
  if (ref) {
    if (f.type === "hours") {
      setRenderHours(f, ref);
    } else if (f.type === "list") {
      setRenderChips(f);
    } else if (ref.input) {
      const value = SET_STATE.current[f.key];
      ref.input.value = value === null || value === undefined ? "" : String(value);
    }
  }
  setClearError(f.key);
  setRefreshField(f.key);
  setUpdateSave();
}

function setClearError(key) {
  const ref = SET_STATE.refs[key];
  if (ref && ref.error) {
    ref.error.hidden = true;
    setText(ref.error, "");
  }
  if (SET_STATE.ui && SET_STATE.ui.saveError) {
    SET_STATE.ui.saveError.hidden = true;
    setText(SET_STATE.ui.saveError, "");
  }
}

function setClearSaveError() {
  if (SET_STATE.ui && SET_STATE.ui.saveError) {
    SET_STATE.ui.saveError.hidden = true;
    setText(SET_STATE.ui.saveError, "");
  }
}

function setInlineError(key, message) {
  const ref = SET_STATE.refs[key];
  if (!ref || !ref.error) return;
  ref.error.hidden = false;
  setText(ref.error, message);
}

function setValidateForm() {
  let firstBad = null;
  SET_GROUPS.forEach(function (group) {
    group.fields.forEach(function (f) {
      const ref = SET_STATE.refs[f.key];
      if (!ref) return;
      if (f.type === "number") {
        if (f.needsOllama && !SET_STATE.data.ollama_set) return;
        const raw = ref.input ? ref.input.value.trim() : "";
        const optional = f.key === "pc_idle.unload_after_min";
        if (raw === "") {
          if (!optional) {
            setInlineError(f.key, "Type a number.");
            if (!firstBad) firstBad = ref.input;
          }
        } else if (!Number.isFinite(Number(raw))) {
          setInlineError(f.key, "Type a number.");
          if (!firstBad) firstBad = ref.input;
        }
      } else if (f.type === "hours") {
        (ref.rows || []).forEach(function (row) {
          if (row.parsed && (!row.start || !row.end || row.end <= row.start)) {
            setValidateHourRow(row);
            if (!firstBad) firstBad = row.el && row.el.end ? row.el.end : ref.input;
          }
        });
      }
    });
  });
  if (firstBad && firstBad.focus) {
    firstBad.focus();
    return true;
  }
  return false;
}

function setCollectValues() {
  const values = {};
  SET_SECTIONS.forEach(function (name) {
    values[name] = {};
  });
  SET_GROUPS.forEach(function (group) {
    group.fields.forEach(function (f) {
      if (f.key === "exclude_libraries") {
        values.exclude_libraries = (SET_STATE.current[f.key] || []).slice();
        return;
      }
      let value = SET_STATE.current[f.key];
      if (f.type === "text") value = value === null || value === undefined ? "" : String(value).trim();
      else if (Array.isArray(value)) value = value.slice();
      setDotSet(values, f.key, value);
    });
  });
  if (!SET_STATE.data.ollama_set) {
    if (values.pc_idle) delete values.pc_idle.unload_after_min;
  }
  return values;
}

function setStatus(text, cls, withReload) {
  const ui = SET_STATE.ui;
  if (!ui || !ui.status) return;
  ui.status.className = "set-status" + (cls ? " " + cls : "");
  ui.status.replaceChildren();
  setText(ui.status, text);
  if (withReload) {
    const reload = document.createElement("button");
    reload.type = "button";
    reload.className = "subs-btn";
    setText(reload, "Reload");
    reload.addEventListener("click", function () {
      loadSettings();
    });
    ui.status.appendChild(reload);
  }
}

function setFieldError(field, why) {
  const ui = SET_STATE.ui;
  const message = why || "That value was refused.";
  const ref = field ? SET_STATE.refs[field] : null;
  if (ref && ref.error) {
    ref.error.hidden = false;
    setText(ref.error, message);
    const focusTarget = ref.input;
    if (focusTarget && focusTarget.focus) focusTarget.focus();
    return;
  }
  if (ui && ui.saveError) {
    ui.saveError.hidden = false;
    setText(ui.saveError, message);
  }
}

function setBuildActions(body) {
  const wrap = el("div", "set-actions");

  const saveError = el("p", "set-error subs-error");
  saveError.hidden = true;
  wrap.appendChild(saveError);

  const save = document.createElement("button");
  save.type = "button";
  save.className = "subs-btn subs-primary";
  setText(save, "Save");
  save.disabled = true;
  save.addEventListener("click", function () {
    saveSettings();
  });
  wrap.appendChild(save);

  const resetAll = document.createElement("button");
  resetAll.type = "button";
  resetAll.className = "subs-btn";
  setText(resetAll, "Reset all to the server file");
  resetAll.addEventListener("click", function () {
    setAskResetAll(resetAll, wrap);
  });
  wrap.appendChild(resetAll);

  const status = el("p", "set-status");
  status.setAttribute("aria-live", "polite");
  wrap.appendChild(status);

  SET_STATE.ui = { save: save, resetAll: resetAll, status: status, saveError: saveError, actions: wrap };
  body.appendChild(wrap);
  setUpdateSave();
}

function setAskResetAll(button, wrap) {
  if (button.dataset.asking) return;
  button.dataset.asking = "1";
  button.disabled = true;
  const yes = document.createElement("button");
  yes.type = "button";
  yes.className = "subs-btn";
  setText(yes, "Yes, reset all");
  yes.addEventListener("click", function () {
    yes.disabled = true;
    button.disabled = true;
    saveSettings({});
  });
  wrap.insertBefore(yes, SET_STATE.ui.status);
  setUpdateSave();
}

function setBuildHistory(body) {
  const lines = SET_STATE.data && Array.isArray(SET_STATE.data.history) ? SET_STATE.data.history : [];
  if (!lines.length) return;
  const title = el("h3", "set-label");
  setText(title, "Last changes");
  body.appendChild(title);
  const list = el("ul", "set-history");
  lines.slice().reverse().forEach(function (item) {
    const li = document.createElement("li");
    const who = item.by || "page";
    const changed = Array.isArray(item.changed) ? item.changed.join(", ") : "";
    const parts = [ago(item.at, Date.now()), who, changed].filter(function (part) {
      return !!part;
    });
    setText(li, parts.join(" · "));
    list.appendChild(li);
  });
  body.appendChild(list);
}

function setBuildDatalist(body) {
  const list = document.createElement("datalist");
  list.id = "set-libs";
  body.appendChild(list);
  setFillDatalist();
}

function setFillDatalist() {
  const list = document.getElementById("set-libs");
  if (!list) return;
  const names = [];
  function add(name) {
    const text = String(name);
    if (text && names.indexOf(text) < 0) names.push(text);
  }
  if (SET_STATE.data) setList(SET_STATE.data.locked_libraries).forEach(add);
  const fileExcluded = SET_STATE.file.exclude_libraries;
  if (Array.isArray(fileExcluded)) fileExcluded.forEach(add);
  if (SET_STATE.coverage) Object.keys(SET_STATE.coverage).forEach(add);
  names.sort(function (a, b) { return a.localeCompare(b); });
  list.replaceChildren();
  names.forEach(function (name) {
    const option = document.createElement("option");
    option.value = name;
    list.appendChild(option);
  });
}

function setLoadLibraries() {
  if (SET_STATE.coverage) {
    setFillDatalist();
    return;
  }
  api("/state").then(function (res) {
    if (res && res.status === 200 && res.data && res.data.coverage && typeof res.data.coverage === "object") {
      SET_STATE.coverage = res.data.coverage;
      setFillDatalist();
    }
  }).catch(function () {
    /* suggestions are optional */
  });
}

function renderSettings(data) {
  SET_STATE.data = data || {};
  SET_STATE.version = Number.isFinite(Number(SET_STATE.data.version)) ? Number(SET_STATE.data.version) : 0;
  SET_STATE.file = SET_STATE.data.file || {};
  const effective = SET_STATE.data.effective || {};
  const pageValues = SET_STATE.data.values || {};

  SET_STATE.baseline = {};
  SET_STATE.current = {};
  SET_STATE.refs = {};

  SET_GROUPS.forEach(function (group) {
    group.fields.forEach(function (f) {
      let value;
      if (f.key === "exclude_libraries") {
        value = setList(pageValues.exclude_libraries);
      } else {
        value = setNormalize(f, effective[f.key]);
      }
      SET_STATE.baseline[f.key] = setCopy(value);
      SET_STATE.current[f.key] = setCopy(value);
    });
  });

  const body = $("settings-body");
  if (!body) return;
  body.replaceChildren();

  const warnText = setUnusedSentence(SET_STATE.data);
  if (warnText) {
    const warn = el("p", "subs-warn");
    setText(warn, warnText);
    body.appendChild(warn);
  }

  SET_GROUPS.forEach(function (group) {
    setBuildGroup(body, group);
  });

  setBuildDatalist(body);
  setBuildActions(body);
  setBuildHistory(body);
  setLoadLibraries();

  SET_STATE.loaded = true;
  setRefreshAll();
}

async function saveSettings(forceValues) {
  if (SET_STATE.saving) return;

  let values;
  if (forceValues) {
    values = forceValues;
  } else {
    setClearSaveError();
    if (setValidateForm()) return;
    values = setCollectValues();
  }

  SET_STATE.saving = true;
  setUpdateSave();
  setStatus("Saving…", "subs-quiet");

  try {
    const res = await api("/settings", { version: SET_STATE.version, values: values });
    const data = (res && res.data) || {};
    SET_STATE.saving = false;
    if (res.status === 200) {
      renderSettings(data);
      setStatus("Saved. Takes effect at the next run of each step.", "subs-quiet");
    } else if (res.status === 409) {
      setStatus(data.why || "Settings changed in another window. Reload to see them.", "subs-error", true);
    } else if (res.status === 400) {
      setFieldError(data.field, data.why);
    } else {
      setStatus(data.why || "The settings were not saved.", "subs-error");
    }
  } catch (err) {
    SET_STATE.saving = false;
    setStatus("Couldn't reach Subplot. Nothing was saved.", "subs-error");
  }

  setUpdateSave();
}

function setFailBody(text) {
  const body = $("settings-body");
  if (!body) return;
  SET_STATE.ui = null;
  body.replaceChildren();
  const note = el("p", "subs-warn");
  setText(note, text);
  body.appendChild(note);
  const reload = document.createElement("button");
  reload.type = "button";
  reload.className = "subs-btn";
  setText(reload, "Reload");
  reload.addEventListener("click", function () {
    loadSettings();
  });
  body.appendChild(reload);
}

async function loadSettings() {
  if (SET_STATE.loading) return;
  const body = $("settings-body");
  if (!body) return;
  SET_STATE.loading = true;
  SET_STATE.loaded = false;
  body.replaceChildren();
  const loading = el("p", "subs-quiet");
  setText(loading, "Loading…");
  body.appendChild(loading);

  try {
    const res = await api("/settings");
    const data = res && res.data ? res.data : {};
    if (res && res.status === 200) {
      renderSettings(data);
    } else {
      setFailBody(data.why || "The settings could not be loaded.");
    }
  } catch (err) {
    setFailBody("Couldn't reach Subplot.");
  } finally {
    SET_STATE.loading = false;
  }
}

function setInit() {
  const box = $("settings-box");
  if (!box) return;
  box.addEventListener("toggle", function () {
    if (box.open && !SET_STATE.loaded && !SET_STATE.loading) loadSettings();
  });
  if (location.hash === "#settings") {
    box.open = true;
    loadSettings().then(function () {
      box.scrollIntoView({ block: "start" });
    }).catch(function () {
      /* ignore */
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", setInit);
} else {
  setInit();
}
