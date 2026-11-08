"use strict";

const $ = id => document.getElementById(id);
const VIEWS = ["loading", "signin", "setup", "code", "not-https", "broken"];
const TITLES = {
  signin: ["Sign in", "Sign in · Subplot"],
  setup: ["Create the account", "Create · Subplot"],
  code: ["Two-step code", "Code · Subplot"],
  "not-https": ["HTTPS required", "Sign in · Subplot"],
  broken: ["Sign-in file damaged", "Sign in · Subplot"],
};
const FIELDS = {
  signin: {user: "si-user", name: "si-user", password: "si-pw"},
  setup: {code: "su-code", user: "su-user", name: "su-user", password: "su-pw"},
  code: {code: "c-code"},
};

let waiting = false;
let waitTimer = 0;

function show(view) {
  for (const id of VIEWS) $(id).hidden = id !== view;
  const title = TITLES[view];
  if (title) {
    $("heading").textContent = title[0];
    document.title = title[1];
  }
  const first = $(view).querySelector("input:not([type=checkbox])");
  if (first) first.focus();
}

function say(text, field) {
  for (const el of document.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
  $("status").textContent = text || "";
  if (field && $(field)) {
    $(field).setAttribute("aria-invalid", "true");
    $(field).focus();
  }
}

function setBusy(busy) {
  for (const b of document.querySelectorAll("form button[type=submit]")) b.disabled = busy;
}

async function api(path, body) {
  const post = body !== undefined;
  const options = {method: post ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers: {}};
  if (post) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-Subs"] = "1";
    options.body = JSON.stringify(body);
  }
  const r = await fetch(path, options);
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  return {status: r.status, data: data, retryAfter: r.headers.get("Retry-After")};
}

const fmt = s => s >= 60 ? Math.floor(s / 60) + " min " + (s % 60) + " s" : s + " s";

function retrySeconds(data, header) {
  let n = Number(data && data.retry);
  if (!Number.isFinite(n)) n = Number(header);
  if (!Number.isFinite(n)) {
    const when = Date.parse(header || "");
    if (Number.isFinite(when)) n = Math.ceil((when - Date.now()) / 1000);
  }
  if (!Number.isFinite(n) || n < 1) n = 60;
  return Math.max(1, Math.min(86400, Math.floor(n)));
}

function wait(seconds, why) {
  clearInterval(waitTimer);
  waiting = true;
  let left = seconds;
  setBusy(true);
  say((why ? why + " " : "Too many tries. ") + "You can try again in " + fmt(left) + ".");
  const box = $("wait");
  box.hidden = false;
  const tick = () => { box.textContent = fmt(left) + " left"; };
  tick();
  waitTimer = setInterval(() => {
    left -= 1;
    if (left > 0) return tick();
    clearInterval(waitTimer);
    waiting = false;
    box.hidden = true;
    box.textContent = "";
    setBusy(false);
    say("You can try again now.");
  }, 1000);
}

async function submit(formId, path, body, onOk) {
  if (waiting) return;
  const form = $(formId);
  const button = form.querySelector("button[type=submit]");
  say("");
  if (button) button.disabled = true;
  try {
    const r = await api(path, body);
    const d = r.data;
    if (r.status === 200) {
      onOk(d);
      return;
    }
    if (r.status === 429) {
      wait(retrySeconds(d, r.retryAfter), d.why);
      return;
    }
    if (r.status === 423) {
      say(d.why || "This account is locked.");
      return;
    }
    if (r.status === 403 && !d.field) {
      show("not-https");
      say(d.why || "");
      return;
    }
    if (d.setup) {
      show("setup");
      say(d.why || "");
      return;
    }
    if (d.restart) {
      $("c-code").value = "";
      show("signin");
      say(d.why || "Sign in again.");
      return;
    }
    const field = d.field ? FIELDS[formId][d.field] : null;
    say(d.why || "Something went wrong (" + r.status + ").", field);
    if (!field) {
      const pw = form.querySelector("input[type=password]");
      if (pw) pw.focus();
    }
  } catch (e) {
    say("Couldn't reach Subplot. Check your connection and try again.");
  } finally {
    if (!waiting && button) button.disabled = false;
  }
}

function signedIn(d) {
  $("si-pw").value = "";
  if (d && d.code_due) {
    $("c-code").value = "";
    show("code");
    say("");
    return;
  }
  $("su-pw").value = "";
  location.replace("./");
}

$("signin").addEventListener("submit", e => {
  e.preventDefault();
  submit("signin", "api/login",
    {user: $("si-user").value, password: $("si-pw").value, keep: $("si-keep").checked}, signedIn);
});

$("setup").addEventListener("submit", e => {
  e.preventDefault();
  submit("setup", "api/setup-account",
    {code: $("su-code").value.trim(), user: $("su-user").value,
     password: $("su-pw").value, keep: $("su-keep").checked}, signedIn);
});

$("code").addEventListener("submit", e => {
  e.preventDefault();
  submit("code", "api/login/code", {code: $("c-code").value.trim()}, signedIn);
});

$("restart").addEventListener("click", async () => {
  if (waiting) return;
  $("c-code").value = "";
  say("");
  show("signin");
  try { await api("api/logout", {}); } catch (e) { /* the unfinished sign-in also expires by itself */ }
});

async function init() {
  try {
    const r = await api("api/auth/state");
    const s = r.data;
    if (r.status === 403) {
      show("not-https");
      say(s.why || "");
      return;
    }
    if (r.status !== 200) {
      show("broken");
      return;
    }
    if (s.mode !== "login" || s.signed_in) {
      location.replace("./");
      return;
    }
    $("pw-min").textContent = String(s.min_length || 15);
    if (s.broken) {
      show("broken");
      return;
    }
    if (!s.https || location.protocol !== "https:") {
      show("not-https");
      return;
    }
    if (s.setup) {
      show("setup");
      return;
    }
    if (s.code_due) {
      show("code");
      return;
    }
    show("signin");
  } catch (e) {
    show("loading");
    $("loading").textContent = "Couldn't reach Subplot. Reload the page to try again.";
  }
}

init();
