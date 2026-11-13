/* Renderer for the publish window.
 *
 * Python owns all state and pushes it as JSON; this file renders it and
 * forwards intents. Nothing here names a destination: what a destination is
 * called, whether it signs in, and whether it has workspaces all come from
 * the catalog.
 */
"use strict";

let bridge = null;
let tags = [];
let workspaceDropdown = null;
let lastState = null;
let workspaceSignature = null;

const $ = (id) => document.getElementById(id);
const PAGES = ["form", "publishing", "done"];
const SUPPORT_URL = "https://miris.com/support";
const TOOLTIP_DELAY_MS = 1000;

function renderUnitChoices(s) {
  unitDropdown.set(
    (s.unitChoices || []).map((c) => ({ value: c.id, label: c.label })),
    s.unitId || "cm"
  );
}

function renderTags() {
  const box = $("tagBox");
  for (const chip of box.querySelectorAll(".chip")) chip.remove();
  const input = $("tagInput");
  for (const tag of tags) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = tag;
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "Remove " + tag;
    x.addEventListener("click", () => {
      tags = tags.filter((t) => t !== tag);
      renderTags();
    });
    chip.appendChild(x);
    box.insertBefore(chip, input);
  }
}

function addTag(raw) {
  const tag = raw.trim().replace(/,+$/, "");
  if (tag && tag !== "substance-painter" && !tags.includes(tag)) {
    tags.push(tag);
    renderTags();
  }
}

function wireTags() {
  const input = $("tagInput");
  $("tagBox").addEventListener("click", () => input.focus());
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag(input.value);
      input.value = "";
    } else if (e.key === "Backspace" && input.value === "" && tags.length) {
      tags.pop();
      renderTags();
    }
  });
  input.addEventListener("blur", () => {
    addTag(input.value);
    input.value = "";
  });
}

let formNonce = null;
let unitDropdown = null;
let ackedPage = null;
/* Client side only: Python's page stays "form" for both steps. */
let formStep = "destination";
let slideTimer = null;
let showingSettings = false;
/* Optimistic local flag so the sign-in button reacts on click. Python's
   auth[id].connecting is what knows the flow ended, however it ended. */
const startingSignIn = {};
// The card whose click started a sign-in. The form advances to it when Python
// reports signed in, and forgets it once a flow it saw running has ended.
let pendingAdvance = null;

let catalog = [];

/* A stored credential can be revoked while the window sits open, so picking a
   destination asks Python to re-check it. */
let verified = null;

function entry(id) {
  return catalog.find((e) => e.id === id) || null;
}

function destination() {
  const picked = document.querySelector('input[name="destination"]:checked');
  return picked ? picked.value : "";
}

function signsIn(e) {
  return Boolean(e && e.signs_in);
}

function authOf(s, id) {
  return (s.auth && s.auth[id]) || {};
}

/* Build one card per catalog entry. The wordmark goes in as markup because it
   is inline SVG; that is safe while the catalog ships inside the plugin, and
   MUST grow a sanitiser on the day it is fetched from anywhere else. */
function buildCards() {
  const host = document.querySelector(".dest-cards");
  host.textContent = "";
  for (const e of catalog) {
    if (e.status === "hidden") continue;
    const selectable = e.status === "available";
    const card = document.createElement("label");
    card.className = "dest-card" + (selectable ? "" : " is-soon");

    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "destination";
    radio.value = e.id;
    radio.disabled = !selectable;
    radio.setAttribute("aria-label", e.title);

    // Ring, disc, spinner, or a "soon" pill. paintStatus() keeps it current.
    const indicator = document.createElement("span");
    indicator.className = "dest-indicator";
    indicator.dataset.for = e.id;

    const logo = document.createElement("span");
    logo.className = "dest-logo";
    logo.setAttribute("aria-hidden", "true");
    if (e.logo && e.logo.svg) {
      logo.innerHTML = e.logo.svg;
    } else if (e.logo && e.logo.file) {
      const img = document.createElement("img");
      img.src = e.logo.file;
      img.alt = "";
      logo.append(img);
    } else {
      logo.classList.add("dest-logo--text");
      logo.textContent = e.title;
    }

    card.append(radio, indicator, logo);
    if (signsIn(e)) card.append(signOutButton(e));
    host.append(card);
  }
  wireCards();
}

function statusOf(e, s) {
  if (e.status !== "available") return "soon";
  const a = authOf(s, e.id);
  if (a.connecting || startingSignIn[e.id]) return "working";
  // An expired session reads as signed out; the notice says why.
  return a.signedIn ? "ready" : "idle";
}

function paintStatus(s) {
  for (const node of document.querySelectorAll(".dest-indicator")) {
    const e = entry(node.dataset.for);
    if (!e) continue;
    const state = statusOf(e, s);
    node.dataset.state = state;
    node.textContent = state === "soon" ? "soon" : "";
    const card = node.closest(".dest-card");
    const signOut = card.querySelector(".dest-signout");
    if (signOut) signOut.hidden = state !== "ready";
  }
}

/* One notice for the whole form. Python's form message wins, then the chosen
   destination's sign-in outcome, then any other destination's. */
function paintNotice(s) {
  let text = s.formMessage || "";
  let tone = s.formTone || "note";
  if (!text) {
    const ids = [destination(), ...Object.keys(s.auth || {})];
    for (const id of ids) {
      const a = id ? authOf(s, id) : {};
      if (a.message && !a.signedIn && !a.connecting) {
        text = a.message;
        tone = a.tone || "note";
        break;
      }
    }
  }
  const show = Boolean(text);
  const notice = $("notice");
  notice.hidden = !show;
  notice.dataset.tone = show ? tone : "note";
  $("noticeText").textContent = show ? text : "";
}

/* Sits inside the card's label, so its click must neither check the radio nor
   reach the card's own handler. */
function signOutButton(e) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "dest-signout";
  button.hidden = true;
  button.innerHTML =
    '<svg viewBox="0 0 16 16" aria-hidden="true" width="13" height="13">' +
    '<path fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" ' +
    'stroke-linejoin="round" d="M6.5 2.5H3.5a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h3M10.5 11l3-3-3-3M13.5 8H6"/></svg>';
  button.title = `Sign out of ${e.title}`;
  button.setAttribute("aria-label", `Sign out of ${e.title}`);
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    startingSignIn[e.id] = false;
    bridge.signOut(e.id);
  });
  return button;
}

/* Listeners for cards that did not exist when wire() ran. */
function wireCards() {
  for (const radio of document.querySelectorAll('input[name="destination"]')) {
    // Only a keyboard focus earns a ring; a click already shows itself.
    radio.addEventListener("focus", () => {
      radio.closest(".dest-card").classList.toggle(
        "is-focused", radio.matches(":focus-visible"));
    });
    radio.addEventListener("blur", () => {
      radio.closest(".dest-card").classList.remove("is-focused");
    });
    radio.addEventListener("change", () => {
      if (lastState) render(lastState);
    });
  }
  // The whole card advances, and on click rather than change: a destination
  // that is already checked fires no change event, so the step would never
  // move. Disabled cards are skipped by the radio rather than by the class.
  for (const card of document.querySelectorAll(".dest-card")) {
    card.addEventListener("click", () => {
      const radio = card.querySelector("input");
      if (radio.disabled) return;
      const id = radio.value;
      const a = lastState ? authOf(JSON.parse(lastState), id) : {};
      if (signsIn(entry(id)) && !a.signedIn) {
        // No button: picking the card is the ask. The row shows the wait and
        // the form moves on by itself once Python says the grant landed.
        if (!a.connecting && !startingSignIn[id]) {
          startingSignIn[id] = true;
          bridge.signIn(id);
        }
        pendingAdvance = { id, seen: Boolean(a.connecting) };
        if (lastState) {
          const now = JSON.parse(lastState);
          paintStatus(now);
          paintNotice(now);
        }
        return;
      }
      goToStep("details");
    });
  }
}

/* Called after anything that changes either pane's height, not just a step
   change: signing in reveals more fields and the track would keep its old
   height. */
function syncSteps() {
  const track = document.querySelector(".form-track");
  const active = $(formStep === "destination" ? "step-destination" : "step-details");
  const other = $(formStep === "destination" ? "step-details" : "step-destination");
  // Lay both panes out and clip the track BEFORE the transform changes. The
  // incoming pane is display:none until is-sliding lands, and an element that
  // becomes displayed and moves in the same frame has no start value to
  // animate from: it would jump instead of slide. The offsetHeight read is the
  // flush that gives it one.
  track.classList.add("is-sliding");
  void track.offsetHeight;

  track.dataset.step = formStep;
  active.removeAttribute("aria-hidden");
  other.setAttribute("aria-hidden", "true");
  track.style.height = active.offsetHeight + "px";

  // A timer rather than transitionend, because with reduced motion there is no
  // transition and the event never arrives, leaving the track clipped forever.
  clearTimeout(slideTimer);
  slideTimer = setTimeout(() => track.classList.remove("is-sliding"), 260);

  reportHeight();
}

function goToStep(step) {
  if (formStep === step) return;
  formStep = step;
  syncSteps();
}

/* Rebuilt only when the destination or the export changes, so a tick survives
   state pushes. Every new export starts unticked: some options spend credits. */
let publishOptionsFor = null;
let environmentPickers = {};
function buildPublishOptions(e, ready) {
  const list = (e && e.publish_options) || [];
  const hosts = { main: $("publishOptions"), advanced: $("advancedOptions") };
  hosts.main.hidden = !ready || !list.some((o) => !o.advanced);
  hosts.advanced.hidden = !ready || !list.some((o) => o.advanced);
  const key = e ? `${e.id}:${formNonce}` : "";
  if (key === publishOptionsFor) return;
  publishOptionsFor = key;
  hosts.main.textContent = "";
  hosts.advanced.textContent = "";
  environmentPickers = {};
  for (const option of list) {
    const host = option.advanced ? hosts.advanced : hosts.main;
    if (option.type === "environments") {
      buildEnvironmentPicker(option, host);
      continue;
    }
    const row = document.createElement("label");
    row.className = "publish-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = `opt-${option.id}`;
    input.dataset.option = option.id;
    input.checked = Boolean(option.default);
    const text = document.createElement("span");
    text.className = "publish-option-label";
    text.textContent = option.label;
    row.append(input, text);
    if (option.help) {
      const tip = document.createElement("button");
      tip.type = "button";
      tip.className = "info-tip";
      tip.title = option.help;
      row.append(tip);
    }
    host.append(row);
    if (option.caution) {
      const caution = document.createElement("p");
      caution.className = "dest-note scale-caution publish-caution";
      caution.textContent = option.caution;
      host.append(caution);
    }
  }
  for (const option of list) {
    if (!option.requires) continue;
    const gate = $(`opt-${option.requires}`);
    const picker = environmentPickers[option.id];
    if (picker) {
      // Several lightings means a set, and a set only streams if it skips preview.
      const follow = () => picker.setMultiple(gate.checked);
      gate.addEventListener("change", follow);
      follow();
      continue;
    }
    const gated = $(`opt-${option.id}`);
    const follow = () => {
      gated.disabled = !gate.checked;
      if (!gate.checked) gated.checked = false;
      gated.closest(".publish-option").classList.toggle("is-gated", gated.disabled);
    };
    gate.addEventListener("change", follow);
    follow();
  }
}

function publishOptionValues() {
  const values = {};
  for (const input of document.querySelectorAll("input[data-option]")) {
    values[input.dataset.option] = input.checked;
  }
  for (const [id, picker] of Object.entries(environmentPickers)) {
    values[id] = picker.value;
  }
  return values;
}

function buildEnvironmentPicker(option, host) {
  const field = document.createElement("div");
  field.className = "field env-field";
  const label = document.createElement("span");
  label.className = "env-label";
  label.textContent = option.label;
  if (option.help) {
    const tip = document.createElement("button");
    tip.type = "button";
    tip.className = "info-tip";
    tip.title = option.help;
    label.append(tip);
  }
  const mount = document.createElement("div");
  mount.id = `opt-${option.id}`;
  const caution = document.createElement("p");
  caution.className = "dest-note publish-caution";
  caution.hidden = true;
  field.append(label, mount, caution);
  host.append(field);

  const picker = createEnvironmentPicker(
    mount,
    (chosen) => {
      caution.hidden = chosen.length < 2;
      caution.textContent =
        `Publishes ${chosen.length} copies and uses ${chosen.length} generation credits.`;
    },
    () =>
      bridge.chooseEnvironment((json) => {
        const result = JSON.parse(json);
        picker.chose(result.choices, result.id);
        if (result.error) picker.refused(result.error);
      }),
    (id) => bridge.forgetEnvironment(id, (json) => picker.chose(JSON.parse(json), ""))
  );
  environmentPickers[option.id] = picker;
  // Listed by Python: the shelves are Painter's, and so are the previews.
  bridge.environments((json) => picker.set(JSON.parse(json)));
}

function applyDestination(s) {
  // An early state push can beat the catalog; render() runs again once it lands.
  if (!catalog.length) return;
  for (const seg of document.querySelectorAll(".dest-card")) {
    seg.classList.toggle("is-selected", seg.querySelector("input").checked);
  }
  const e = entry(destination());
  const title = e ? e.title : "";
  const caps = (e && e.capabilities) || {};

  // Ask once per selection, not on every state push: render() runs on every
  // progress tick and this is a round trip to the destination.
  if (e && verified !== e.id) {
    verified = e.id;
    bridge.selectDestination(e.id);
  }

  const auth = e ? authOf(s, e.id) : {};
  const ready = Boolean(auth.signedIn);

  $("destNote").textContent = ready && e ? e.description : "";
  $("publishLabel").textContent = title ? `Publish to ${title}` : "Publish";

  $("destNote").hidden = !ready;

  $("nameField").hidden = !ready;
  $("publishBtn").hidden = !ready;
  $("tagsField").hidden = !ready || !caps.tags;
  $("workspaceField").hidden = !ready || !caps.workspaces;
  buildPublishOptions(e, ready);
  syncSteps();
  return auth;
}

function render(json) {
  lastState = json;
  const s = JSON.parse(json);
  document.body.dataset.page = s.page;
  if (s.page !== "form") showingSettings = false;
  for (const p of PAGES) $("page-" + p).hidden = p !== s.page;
  $("page-settings").hidden = !showingSettings;
  if (showingSettings) $("page-form").hidden = true;
  $("settingsBtn").hidden = s.page !== "form" || showingSettings;

  // Clear the form only when Python says a NEW export started. Keying this on
  // "the page just became the form" is wrong in both directions: a second
  // export while already on the form would keep the previous asset's name and
  // tags, and a failed upload returning to the form would wipe what the user
  // typed just as they go to retry.
  if (s.formNonce !== formNonce) {
    formNonce = s.formNonce;
    tags = [];
    renderTags();
    $("tagInput").value = "";
    $("assetName").value = s.assetName || "";
    renderUnitChoices(s);
    $("keepExports").checked = Boolean(s.keepExports);
    formStep = "destination";
    // Re-check the credential, however long the window was closed.
    verified = null;
  }

  // Python's flag is authoritative once it has spoken about this destination.
  for (const id of Object.keys(s.auth || {})) {
    const a = s.auth[id];
    if (a.signedIn || a.connecting === false) startingSignIn[id] = false;
  }
  if (pendingAdvance) {
    const a = (s.auth || {})[pendingAdvance.id] || {};
    if (a.connecting) pendingAdvance.seen = true;
    if (a.signedIn) {
      pendingAdvance = null;
      goToStep("details");
    } else if (pendingAdvance.seen && a.connecting === false) {
      pendingAdvance = null;
    }
  }

  const auth = applyDestination(s) || {};
  paintStatus(s);
  // Expired or signed out while the form was open: the row is where sign-in
  // lives now, so that is where to be.
  const current = entry(destination());
  if (formStep === "details" && current && signsIn(current) && !auth.signedIn) {
    goToStep("destination");
  }

  // Rebuilding on every push would close the menu mid-interaction; the
  // signature keeps it stable unless the workspaces or selection change.
  const list = auth.workspaces || [];
  const chosen = auth.workspace || "";
  const signature = JSON.stringify([destination(), list, chosen]);
  if (signature !== workspaceSignature) {
    workspaceSignature = signature;
    workspaceDropdown.set(
      list.map((w) => ({ value: w.id, label: w.name })),
      chosen || null
    );
  }
  // A credential pinned to one workspace shows it and says how to change it.
  const fixed = Boolean(auth.workspaceFixed);
  workspaceDropdown.disable(fixed);
  $("workspaceNote").hidden = !fixed;
  $("workspaceNote").textContent = fixed && current
    ? `Chosen when you signed in to ${current.title}. Sign out to switch.`
    : "";

  paintNotice(s);
  $("stageLine").textContent = s.stage || "";
  $("progressFill").style.width = (s.progressPct || 0) + "%";
  $("bytesLine").textContent = s.bytesLine || "";
  $("shareUrl").textContent = s.shareUrl || "";
  $("doneLine").textContent = s.doneMessage || (s.shareUrl ? "Share link:" : "Published.");
  $("shareUrl").parentElement.hidden = !s.shareUrl;
  $("openBtn").hidden = !s.shareUrl;

  // Tell Python which page is on screen, once the browser has actually painted
  // it. Publishing blocks Painter's main thread, so it may only start after the
  // progress page is visible. Two frames: the first schedules the paint, the
  // second runs after it has happened.
  if (s.page !== ackedPage) {
    ackedPage = s.page;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => bridge.pageShown(s.page))
    );
  }
  reportHeight();
}

/* Body height rather than the viewport, which is whatever size the window
   happens to be. */
function reportHeight() {
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      if (!bridge) return;
      bridge.contentHeight(measuredHeight());
    })
  );
}

/* Open menus deliberately do not count: growing the window when a menu opens
   moves everything under the pointer. The menu keeps itself inside the window
   instead, by opening upward when there is no room below (see dropdown.js). */
function measuredHeight() {
  const style = getComputedStyle(document.body);
  return Math.ceil(
    document.body.getBoundingClientRect().bottom + parseFloat(style.marginBottom)
  );
}

/* Whether to keep exports is decided per publish on the form, so nothing here
   is gated on it. */
function applySettings(settings) {
  $("revealExports").checked = settings.revealExports;
  $("exportFolderPath").textContent = settings.exportRoot;
  $("resetFolderBtn").hidden = settings.exportRoot === settings.defaultRoot;
  reportHeight();
}

function loadSettings() {
  bridge.settings((json) => applySettings(JSON.parse(json)));
}

function showSettings(on) {
  showingSettings = on;
  if (on) loadSettings();
  if (lastState) render(lastState);
}

// One tooltip element serves every [data-tip] control. It waits a beat so a
// pointer crossing the header does not flash labels.
function wireTooltips() {
  const tip = document.createElement("div");
  tip.id = "tip";
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  let timer = null;
  let owner = null;

  const hide = () => {
    clearTimeout(timer);
    timer = null;
    tip.hidden = true;
    tip.classList.remove("tip-shown");
    if (owner) owner.removeAttribute("aria-describedby");
    owner = null;
  };
  const show = (btn) => {
    owner = btn;
    tip.textContent = btn.dataset.tip;
    tip.hidden = false;
    const box = btn.getBoundingClientRect();
    const width = tip.offsetWidth;
    // Centred under the control, but kept inside the window's edges.
    const left = Math.min(box.left + box.width / 2 - width / 2, window.innerWidth - width - 6);
    tip.style.left = Math.max(6, left) + "px";
    tip.style.top = box.bottom + 6 + "px";
    btn.setAttribute("aria-describedby", tip.id);
    requestAnimationFrame(() => {
      if (owner === btn) tip.classList.add("tip-shown");
    });
  };
  const arm = (btn) => {
    hide();
    timer = setTimeout(() => show(btn), TOOLTIP_DELAY_MS);
  };
  for (const btn of document.querySelectorAll("[data-tip]")) {
    btn.addEventListener("mouseenter", () => arm(btn));
    // Keyboard focus only: a click also focuses, and its tooltip would be noise.
    btn.addEventListener("focus", () => {
      if (btn.matches(":focus-visible")) arm(btn);
    });
    for (const name of ["mouseleave", "blur", "mousedown", "click"]) btn.addEventListener(name, hide);
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hide();
  });
  document.addEventListener("scroll", hide, true);
}

function wireSettings() {
  $("helpBtn").addEventListener("click", () => bridge.openExternal(SUPPORT_URL));
  $("settingsBtn").addEventListener("click", () => showSettings(true));
  $("settingsBackBtn").addEventListener("click", () => showSettings(false));

  $("revealExports").addEventListener("change", (e) => {
    bridge.setRevealExports(e.target.checked);
  });

  // Both return the root now in effect, so neither needs a second round trip.
  $("chooseFolderBtn").addEventListener("click", () => {
    bridge.chooseExportFolder(() => loadSettings());
  });
  $("resetFolderBtn").addEventListener("click", () => {
    bridge.resetExportFolder(() => loadSettings());
  });
}

function wire() {
  $("noticeDismiss").addEventListener("click", () => {
    $("notice").hidden = true;  // gone now; Python's next push confirms it
    bridge.dismissNotice();
  });
  wireTags();
  wireSettings();
  wireTooltips();


  $("keepExports").addEventListener("change", (e) => bridge.setKeepExports(e.target.checked));
  // The window sizes itself to its content, so a fold must report its height.
  $("advanced").addEventListener("toggle", reportHeight);
  unitDropdown = createDropdown($("unitDropdown"), () => {});
  workspaceDropdown = createDropdown($("workspaceDropdown"), (value) => {
    const id = destination();
    if (value && id) bridge.setWorkspace(id, value);
  });
  $("backBtn").addEventListener("click", () => goToStep("destination"));

  $("cancelBtn").addEventListener("click", () => bridge.closeWindow());
  $("closeBtn").addEventListener("click", () => bridge.closeWindow());
  $("cancelPublishBtn").addEventListener("click", () => bridge.cancelPublish());
  $("publishBtn").addEventListener("click", () => {
    addTag($("tagInput").value);
    $("tagInput").value = "";
    bridge.publish(
      $("assetName").value.trim(),
      JSON.stringify(tags),
      destination(),
      JSON.stringify(publishOptionValues()),
      unitDropdown.value || "cm"
    );
  });
  $("openBtn").addEventListener("click", () => bridge.openExternal($("shareUrl").textContent));

  let copiedTimer = null;
  $("copyBtn").addEventListener("click", () => {
    bridge.copyLink($("shareUrl").textContent);
    const btn = $("copyBtn");
    btn.classList.add("copied");
    clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => btn.classList.remove("copied"), 1200);
  });

  $("assetName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("publishBtn").click();
  });
}

new QWebChannel(qt.webChannelTransport, (channel) => {
  bridge = channel.objects.bridge;
  wire();  // builds the dropdown render() depends on, before any state arrives
  bridge.catalog((json) => {
    catalog = JSON.parse(json).integrations || [];
    buildCards();
    if (lastState) render(lastState);  // state may have arrived first
  });
  bridge.stateChanged.connect(render);
  bridge.ready();
});
