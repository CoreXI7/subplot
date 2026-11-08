"use strict";

(function () {
  const API = "api";
  const LOGIN = "login";
  const TIMEOUT = 15000;

  let boxEl = null;
  let panelEl = null;
  let liveEl = null;
  let totpDraft = null;
  let oneShot = null;
  let confirmBox = null;   // the password (and code) every change below needs: {pw, code}

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function time(t) {
    const n = Number(t);
    if (!isFinite(n) || n <= 0) return "";
    return new Date(n * 1000).toLocaleString();
  }

  async function api(path, body) {
    const options = {
      cache: "no-store",
      redirect: "manual",
      headers: {}
    };
    if (body === undefined) {
      options.method = "GET";
    } else {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Subs"] = "1";
      options.body = JSON.stringify(body);
    }
    const controller = new AbortController();
    const timer = setTimeout(function () {
      controller.abort();
    }, TIMEOUT);
    options.signal = controller.signal;
    try {
      const response = await fetch(API + path, options);
      let data = null;
      try {
        data = await response.json();
      } catch (err) {
        data = null;
      }
      return { status: response.status, data: data };
    } catch (err) {
      return { status: 0, data: null };
    } finally {
      clearTimeout(timer);
    }
  }

  async function call(path, body) {
    const result = await api(path, body);
    if (result.status === 401 && result.data && result.data.signin === true) {
      location.href = LOGIN;
      return result;
    }
    if (result.status === 409 && result.data && result.data.restart === true) {
      location.href = LOGIN;
    }
    return result;
  }

  function say(text) {
    if (!liveEl) return;
    liveEl.textContent = text ? String(text) : "";
    liveEl.hidden = !text;
  }

  function fail(result) {
    if (result.status === 401 && result.data && result.data.signin === true) return;
    if (result.status === 409 && result.data && result.data.restart === true) return;
    if (result.data && result.data.why) {
      say(result.data.why);
      return;
    }
    if (result.status === 0) {
      say("The board did not answer. Try again.");
      return;
    }
    say("That did not work. Try again.");
  }

  function ensureSummary() {
    let summary = null;
    for (let i = 0; i < boxEl.children.length; i += 1) {
      if (boxEl.children[i].tagName === "SUMMARY") {
        summary = boxEl.children[i];
        break;
      }
    }
    if (!summary) {
      const created = document.createElement("summary");
      created.appendChild(el("h2", "inline", "Account"));
      boxEl.insertBefore(created, boxEl.firstChild);
      return;
    }
    if (summary.children.length === 0 && !summary.textContent.trim()) {
      summary.appendChild(el("h2", "inline", "Account"));
    }
  }

  function ensureFrame() {
    if (panelEl && boxEl.contains(panelEl)) return;
    liveEl = el("p", "subs-error");
    liveEl.setAttribute("role", "status");
    liveEl.setAttribute("aria-live", "polite");
    liveEl.hidden = true;
    panelEl = el("div", "account-panel");
    boxEl.appendChild(liveEl);
    boxEl.appendChild(panelEl);
  }

  async function init() {
    const box = document.getElementById("account-box");
    if (!box) return;
    boxEl = box;
    const result = await call("/config");
    if (!result.data || result.data.auth !== "login") return;
    ensureSummary();
    ensureFrame();
    boxEl.hidden = false;
    await reload();
  }

  async function reload() {
    if (!boxEl) return;
    const result = await call("/account");
    if (result.status !== 200 || !result.data) {
      fail(result);
      return;
    }
    render(result.data);
    boxEl.hidden = false;
  }

  async function act(path, body) {
    say("");
    oneShot = null;
    const result = await call(path, body);
    if (result.status === 200 && result.data && result.data.ok === true) {
      await reload();
      return true;
    }
    fail(result);
    return false;
  }

  async function startTotp() {
    say("");
    const result = await call("/account/totp/start", withConfirm({}));
    if (
      result.status === 200 &&
      result.data &&
      typeof result.data.secret === "string" &&
      result.data.secret.length > 0
    ) {
      totpDraft = {
        secret: result.data.secret,
        uri: typeof result.data.uri === "string" ? result.data.uri : "",
        qr: typeof result.data.qr === "string" ? result.data.qr : ""
      };
      oneShot = null;
      await reload();
      return true;
    }
    fail(result);
    return false;
  }

  async function newRecovery() {
    say("");
    const result = await call("/account/recovery", withConfirm({}));
    if (result.status === 200 && result.data && Array.isArray(result.data.recovery)) {
      oneShot = result.data.recovery;
      await reload();
      return true;
    }
    fail(result);
    return false;
  }

  function section(title) {
    const part = document.createElement("section");
    part.className = "account-part";
    part.appendChild(el("h3", null, title));
    return part;
  }

  function field(prefix, labelText, name, type, autocomplete) {
    const id = prefix + "-" + name;
    const wrap = el("p", "account-field");
    const label = el("label", null, labelText);
    label.setAttribute("for", id);
    const input = document.createElement("input");
    input.type = type || "text";
    input.id = id;
    input.name = name;
    input.required = true;
    if (autocomplete) input.autocomplete = autocomplete;
    wrap.appendChild(label);
    wrap.appendChild(input);
    return { wrap: wrap, input: input };
  }

  function userLine(d) {
    const line = el("p", "account-user");
    line.appendChild(el("span", "subs-quiet", "Signed in as "));
    line.appendChild(el("strong", null, d.user ? String(d.user) : "the owner"));
    return line;
  }

  function passwordPart(d) {
    const part = section("Change password");
    const form = el("form", "account-form");
    const current = field("account-password", "Current password", "current", "password", "current-password");
    const next = field("account-password", "New password", "new", "password", "new-password");
    form.appendChild(current.wrap);
    form.appendChild(next.wrap);
    const minLength = Number(d.min_length);
    if (isFinite(minLength) && minLength > 0) {
      next.input.minLength = Math.floor(minLength);
    }
    let code = null;
    if (d.totp === true) {
      code = field("account-password", "Authenticator or recovery code", "code", "text", "one-time-code");
      form.appendChild(code.wrap);
    }
    const submit = el("button", "subs-btn subs-primary", "Change password");
    submit.type = "submit";
    form.appendChild(submit);
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const body = { password: current.input.value, new: next.input.value };
      if (code) body.code = code.input.value;
      submit.disabled = true;
      await act("/account/password", body);
      submit.disabled = false;
    });
    part.appendChild(form);
    return part;
  }

  function twoStepPart(d) {
    const part = section("Two-step sign-in");

    if (totpDraft) {
      part.appendChild(twoStepDraft());
      return part;
    }

    if (d.totp === true) {
      part.appendChild(el("p", null, "A code from your authenticator app is asked for at sign-in."));
      if (typeof d.recovery_left === "number") {
        part.appendChild(el("p", "subs-quiet", d.recovery_left + " recovery codes left."));
      }

      const form = el("form", "account-form");
      const password = field("account-totp-off", "Password", "password", "password", "current-password");
      const code = field("account-totp-off", "Authenticator or recovery code", "code", "text", "one-time-code");
      form.appendChild(password.wrap);
      form.appendChild(code.wrap);
      const off = el("button", "subs-btn", "Turn off two-step sign-in");
      off.type = "submit";
      form.appendChild(off);
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        off.disabled = true;
        await act("/account/totp/off", {
          password: password.input.value,
          code: code.input.value
        });
        off.disabled = false;
      });
      part.appendChild(form);

      const fresh = el("button", "subs-btn", "New recovery codes");
      fresh.type = "button";
      fresh.addEventListener("click", async function () {
        fresh.disabled = true;
        const ok = await newRecovery();
        if (!ok) fresh.disabled = false;
      });
      part.appendChild(fresh);
      return part;
    }

    part.appendChild(el("p", "subs-quiet", "Two-step sign-in is off. Add an authenticator app for a second step."));
    const start = el("button", "subs-btn subs-primary", "Start");
    start.type = "button";
    start.addEventListener("click", async function () {
      start.disabled = true;
      const ok = await startTotp();
      if (!ok) start.disabled = false;
    });
    part.appendChild(start);
    return part;
  }

  function twoStepDraft() {
    const wrap = el("div", "account-totp-draft");
    wrap.appendChild(
      el("p", null, "Scan this code with your authenticator app, then confirm with a code and your password.")
    );

    const svg = cleanSvg(totpDraft.qr);
    if (svg) {
      wrap.appendChild(svg);
    } else {
      wrap.appendChild(el("p", "subs-warn", "The QR code could not be shown. Enter the secret by hand."));
    }

    const secretLine = el("p", "account-secret");
    secretLine.appendChild(el("span", "subs-quiet", "Secret: "));
    secretLine.appendChild(el("code", null, totpDraft.secret));
    wrap.appendChild(secretLine);

    const form = el("form", "account-form");
    const code = field("account-totp-confirm", "Code from the app", "code", "text", "one-time-code");
    const password = field("account-totp-confirm", "Password", "password", "password", "current-password");
    form.appendChild(code.wrap);
    form.appendChild(password.wrap);
    const confirm = el("button", "subs-btn subs-primary", "Turn on two-step sign-in");
    confirm.type = "submit";
    form.appendChild(confirm);
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      confirm.disabled = true;
      say("");
      const result = await call("/account/totp/confirm", {
        code: code.input.value,
        password: password.input.value
      });
      confirm.disabled = false;
      if (result.status === 200 && result.data && result.data.ok === true) {
        totpDraft = null;
        oneShot = Array.isArray(result.data.recovery) ? result.data.recovery : [];
        await reload();
        return;
      }
      fail(result);
    });
    wrap.appendChild(form);

    const cancel = el("button", "subs-btn", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", async function () {
      say("");
      totpDraft = null;
      await reload();
    });
    wrap.appendChild(cancel);
    return wrap;
  }

  function cleanSvg(text) {
    if (typeof text !== "string" || text.length === 0) return null;
    let doc = null;
    try {
      doc = new DOMParser().parseFromString(text, "image/svg+xml");
    } catch (err) {
      return null;
    }
    if (!doc) return null;
    const root = doc.documentElement;
    if (!root || String(root.localName).toLowerCase() !== "svg") return null;
    if (root.namespaceURI && root.namespaceURI !== "http://www.w3.org/2000/svg") return null;
    if (doc.getElementsByTagName("parsererror").length > 0) return null;

    const nodes = [root];
    const all = root.getElementsByTagName("*");
    for (let i = 0; i < all.length; i += 1) nodes.push(all[i]);
    for (let i = 0; i < nodes.length; i += 1) {
      const node = nodes[i];
      const name = String(node.localName).toLowerCase();
      if (name === "script" || name === "foreignobject") {
        if (node.parentNode) node.parentNode.removeChild(node);
        continue;
      }
      const attributes = Array.prototype.slice.call(node.attributes);
      for (let j = 0; j < attributes.length; j += 1) {
        if (String(attributes[j].name).toLowerCase().indexOf("on") === 0) {
          node.removeAttribute(attributes[j].name);
        }
      }
    }
    const imported = document.importNode(root, true);
    imported.setAttribute("class", "account-qr");
    imported.setAttribute("role", "img");
    imported.setAttribute("aria-label", "Two-step sign-in QR code");
    return imported;
  }

  function recoveryBlock(codes) {
    const wrap = el("div", "account-recovery");
    wrap.appendChild(el("h3", null, "Recovery codes"));
    const pre = el("pre", "account-codes");
    pre.textContent = codes
      .map(function (code) {
        return String(code);
      })
      .join("\n");
    wrap.appendChild(pre);
    wrap.appendChild(el("p", "subs-quiet", "Save these now. They are shown once and cannot be seen again."));
    return wrap;
  }

  function noticeBlock(notice) {
    const wrap = el("div", "account-notice");
    const n = Number(notice.code) || 0;
    let text = n + (n === 1 ? " wrong authenticator code was" : " wrong authenticator codes were") +
      " typed after the right password: someone else may know it. If it wasn't you, change the password.";
    if (notice.t) text += " " + time(notice.t) + ".";
    wrap.appendChild(el("p", "subs-warn", text));
    const wasMe = el("button", "subs-btn", "That was me");
    wasMe.type = "button";
    wasMe.addEventListener("click", async function () {
      wasMe.disabled = true;
      const ok = await act("/account/notice/dismiss", withConfirm({ seen: Number(notice.code) || 0 }));
      if (!ok) wasMe.disabled = false;
    });
    wrap.appendChild(wasMe);
    return wrap;
  }

  function sessionsPart(d) {
    const part = section("Signed-in browsers");
    const list = Array.isArray(d.sessions) ? d.sessions : [];
    if (list.length === 0) {
      part.appendChild(el("p", "subs-quiet", "This browser is the only one signed in."));
    } else {
      const ul = el("ul", "account-sessions");
      list.forEach(function (item) {
        const li = el("li", "account-session");
        const what = [];
        what.push(item.ua ? String(item.ua).slice(0, 80) : "Unknown browser");
        if (item.ip) what.push("from " + item.ip);
        if (item.current === true) what.push("this browser");
        li.appendChild(el("span", "account-session-name", what.join(" · ")));
        const last = time(item.seen || item.created);
        if (last) li.appendChild(el("span", "subs-quiet", "Last seen " + last));
        const out = el("button", "subs-btn", "Sign out");
        out.type = "button";
        out.addEventListener("click", async function () {
          out.disabled = true;
          const ok = await act("/account/sessions/end", withConfirm({ id: item.id }));
          if (!ok) out.disabled = false;
        });
        li.appendChild(out);
        ul.appendChild(li);
      });
      part.appendChild(ul);
    }
    const all = el("button", "subs-btn", "Sign out everywhere else");
    all.type = "button";
    all.addEventListener("click", async function () {
      all.disabled = true;
      const ok = await act("/account/sessions/end", withConfirm({ all: true }));
      if (!ok) all.disabled = false;
    });
    part.appendChild(all);
    return part;
  }

  function eventList(items, emptyText) {
    const list = Array.isArray(items) ? items : [];
    if (list.length === 0) return el("p", "subs-quiet", emptyText);
    const ul = el("ul", "account-events");
    list.forEach(function (item) {
      const li = document.createElement("li");
      const when = time(item && item.t);
      if (when) li.appendChild(el("time", null, when));
      const what = item && item.kind ? String(item.kind) : "Something happened";
      li.appendChild(el("span", null, when ? " · " + what : what));
      if (item && item.ip) li.appendChild(el("span", "subs-quiet", " · from " + item.ip));
      ul.appendChild(li);
    });
    return ul;
  }

  function signOutRow() {
    const wrap = el("p", "account-signout");
    const out = el("button", "subs-btn", "Sign out");
    out.type = "button";
    out.addEventListener("click", async function () {
      out.disabled = true;
      say("");
      const result = await call("/logout", {});
      if (
        result.status === 200 ||
        result.status === 401 ||
        (result.data && result.data.ok === true)
      ) {
        location.href = LOGIN;
        return;
      }
      out.disabled = false;
      fail(result);
    });
    wrap.appendChild(out);
    return wrap;
  }

  function withConfirm(body) {
    /* Every account change sends the current password again (and the code when two-step is on): a session alone,
       say on a borrowed screen, can't change anything. */
    const out = Object.assign({}, body);
    if (confirmBox) {
      out.password = confirmBox.pw.input.value;
      if (confirmBox.code) out.code = confirmBox.code.input.value;
    }
    return out;
  }

  function confirmPart(d) {
    const part = section("Confirm changes");
    part.appendChild(el("p", "subs-quiet", "Starting two-step sign-in, new recovery codes, signing browsers out and " +
      "\u201cThat was me\u201d need your current password" + (d.totp ? " and a code" : "") + " here."));
    const pw = field("account-confirm", "Current password", "password", "password", "current-password");
    part.appendChild(pw.wrap);
    let code = null;
    if (d.totp) {
      code = field("account-confirm", "Authenticator or recovery code", "code", "text", "one-time-code");
      part.appendChild(code.wrap);
    }
    confirmBox = { pw: pw, code: code };
    return part;
  }

  function render(d) {
    while (panelEl.firstChild) panelEl.removeChild(panelEl.firstChild);

    if (d.notice && Number(d.notice.code) > 0) panelEl.appendChild(noticeBlock(d.notice));
    panelEl.appendChild(userLine(d));
    panelEl.appendChild(confirmPart(d));
    if (oneShot && oneShot.length > 0) {
      panelEl.appendChild(recoveryBlock(oneShot));
      oneShot = null;
    }
    panelEl.appendChild(passwordPart(d));
    panelEl.appendChild(twoStepPart(d));
    panelEl.appendChild(sessionsPart(d));

    const events = section("Recent events");
    events.appendChild(eventList(d.events, "No recent events."));
    panelEl.appendChild(events);

    const fails = section("Recent failed tries");
    fails.appendChild(eventList(d.fails, "No failed tries."));
    panelEl.appendChild(fails);

    panelEl.appendChild(signOutRow());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
