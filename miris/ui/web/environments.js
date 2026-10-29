/* Lighting picker for dialog.js, loaded as a plain script before it.
 * The trigger reads like a dropdown; the popover is a filterable grid of
 * Painter's environment previews, led by Miris default and the user's files. */
"use strict";

const COLUMNS = 4;

// Tiles without a Painter preview get a 512x256 image too. Painter's web
// engine sizes a tile's thumbnail by its image, not by CSS aspect-ratio.
const tile = (body) =>
  "data:image/svg+xml," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="512" height="256" viewBox="0 0 512 256">${body}</svg>`
  );

// Miris default and files from disk: neither has a picture to show.
const NO_PREVIEW = tile(
  '<rect width="512" height="256" fill="#1b1c20"/>' +
    '<g fill="none" stroke="#9a9ca4" stroke-width="1.75" stroke-linecap="round" ' +
    'stroke-linejoin="round" transform="translate(184 68) scale(6)">' +
    '<rect x="4" y="3" width="16" height="14" rx="2"/><circle cx="9" cy="8" r="1.5"/>' +
    '<path d="m20 13-3-3a2 2 0 0 0-2.8 0L7 17"/></g>'
);

const ADD = tile(
  '<path d="M256 88v80M216 128h80" stroke="#9a9ca4" stroke-width="8" stroke-linecap="round"/>'
);

function createEnvironmentPicker(root, onChange, onBrowse, onForget) {
  root.classList.add("dropdown", "env-picker");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "dropdown-btn env-trigger";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  btn.innerHTML =
    '<span class="env-stack" aria-hidden="true"></span>' +
    '<span class="dropdown-label"></span>' +
    '<svg class="chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';

  const pop = document.createElement("div");
  pop.className = "env-popover";
  pop.hidden = true;
  const filter = document.createElement("input");
  filter.type = "search";
  filter.className = "env-filter";
  filter.placeholder = "Filter environments";
  filter.setAttribute("aria-label", "Filter environments");
  const list = document.createElement("div");
  list.className = "env-scroll";
  list.setAttribute("role", "listbox");
  const hint = document.createElement("p");
  hint.className = "env-hint";
  pop.append(filter, list, hint);
  root.append(btn, pop);

  let items = [];
  let value = [];
  // null until a gate says otherwise: a picker with no gate is always single.
  let multiple = null;

  const byId = (id) => items.find((item) => item.id === id);
  const defaultId = () => (items[0] ? items[0].id : "");

  // A file from disk has no Painter sidecar, so no preview.
  function thumb(src, className) {
    const box = document.createElement("span");
    box.className = className;
    const img = document.createElement("img");
    img.src = src || NO_PREVIEW;
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    box.append(img);
    return box;
  }

  function paintTrigger() {
    const chosen = value.map(byId).filter(Boolean);
    const stack = btn.querySelector(".env-stack");
    stack.textContent = "";
    for (const item of chosen.filter((c) => c.id !== defaultId()).slice(0, 3)) {
      stack.append(thumb(item.preview, "env-mini"));
    }
    stack.hidden = !stack.childElementCount;
    const first = chosen[0] ? chosen[0].label : "";
    btn.querySelector(".dropdown-label").textContent =
      chosen.length > 1 ? `${first} + ${chosen.length - 1} more` : first;
  }

  function paintHint() {
    hint.hidden = multiple === null;
    hint.textContent = multiple
      ? "Click to add or remove. Each one publishes its own copy."
      : "Turn on Skip preview to pick more than one.";
  }

  function option(item) {
    const el = document.createElement("button");
    el.type = "button";
    el.setAttribute("role", "option");
    el.dataset.id = item.id;
    el.title = item.label;
    el.className = "env-tile";
    const name = document.createElement("span");
    name.className = "env-name";
    name.textContent = item.label;
    const image = thumb(item.preview, "env-thumb");
    el.append(image, name);
    if (item.group === "Your files") {
      // A span, not a button: a button may not sit inside the tile's button.
      const remove = document.createElement("span");
      remove.className = "env-remove";
      remove.title = "Remove from the list";
      remove.setAttribute("aria-hidden", "true");
      remove.innerHTML = '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>';
      remove.addEventListener("click", (e) => {
        e.stopPropagation();
        onForget(item.id);
      });
      image.append(remove);
      el.addEventListener("keydown", (e) => {
        if (e.key === "Delete" || e.key === "Backspace") onForget(item.id);
      });
    }
    el.addEventListener("click", (e) => {
      // Inside a <label> the browser forwards a synthetic click to the
      // label's control, the picker's own button, which reopened it.
      e.stopPropagation();
      pick(item.id);
    });
    return el;
  }

  function addGroup(name) {
    const grid = document.createElement("div");
    grid.className = "env-grid";
    if (name) {
      const heading = document.createElement("p");
      heading.className = "env-group";
      heading.textContent = name;
      list.append(heading);
    }
    list.append(grid);
    return grid;
  }

  function renderList() {
    list.textContent = "";
    if (!items.length) return;
    addGroup("").append(option(items[0]));
    // Picked files come first from the plugin, so they land after this tile.
    let group = "Your files";
    let grid = addGroup(group);
    const add = document.createElement("button");
    add.type = "button";
    add.className = "env-tile env-add";
    const name = document.createElement("span");
    name.className = "env-name";
    name.textContent = "Choose a file…";
    add.append(thumb(ADD, "env-thumb"), name);
    add.addEventListener("click", (e) => {
      e.stopPropagation();
      onBrowse();
    });
    grid.append(add);
    for (const item of items.slice(1)) {
      if (item.group !== group) {
        group = item.group;
        grid = addGroup(group);
      }
      grid.append(option(item));
    }
    const empty = document.createElement("p");
    empty.className = "env-empty";
    empty.textContent = "No environments match.";
    empty.hidden = true;
    list.append(empty);
    paintSelection();
    applyFilter();
  }

  function paintSelection() {
    for (const el of list.querySelectorAll("[role=option]")) {
      el.setAttribute("aria-selected", String(value.includes(el.dataset.id)));
    }
  }

  function applyFilter() {
    const needle = filter.value.trim().toLowerCase();
    let shown = 0;
    for (const grid of list.querySelectorAll(".env-grid")) {
      let inGroup = 0;
      for (const tile of grid.children) {
        const item = byId(tile.dataset.id);
        const match = item
          ? !needle || `${item.label} ${item.group}`.toLowerCase().includes(needle)
          : !needle;
        tile.hidden = !match;
        if (match) inGroup += 1;
      }
      grid.hidden = !inGroup;
      if (grid.previousElementSibling) grid.previousElementSibling.hidden = !inGroup;
      shown += inGroup;
    }
    const empty = list.querySelector(".env-empty");
    if (empty) empty.hidden = shown > 0;
  }

  function pick(id) {
    if (!multiple) {
      value = [id];
      close();
      btn.focus();
    } else if (value.includes(id)) {
      value = value.filter((v) => v !== id);
      if (!value.length) value = [defaultId()];
    } else if (value.length === 1 && value[0] === defaultId()) {
      // The default was only the starting value; a first pick replaces it.
      value = [id];
    } else {
      value = [...value, id];
    }
    paintSelection();
    paintTrigger();
    paintHint();
    onChange(value.slice());
  }

  /* Keep the popover inside the window rather than growing it, as the
     dropdown does: the window is sized to the page, not to what floats. */
  function place() {
    root.classList.remove("drop-up");
    list.style.maxHeight = "";
    const GAP = 4;
    const MARGIN = 8;
    const box = btn.getBoundingClientRect();
    const below = window.innerHeight - box.bottom - GAP - MARGIN;
    const above = box.top - GAP - MARGIN;
    const chrome = pop.offsetHeight - list.offsetHeight;
    const up = pop.offsetHeight > below && above > below;
    if (up) root.classList.add("drop-up");
    list.style.maxHeight = Math.max(120, (up ? above : below) - chrome) + "px";
  }

  function open() {
    filter.value = "";
    applyFilter();
    pop.hidden = false;
    root.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
    place();
    const selected = list.querySelector('[aria-selected="true"]');
    if (selected) selected.scrollIntoView({ block: "nearest" });
    filter.focus();
  }

  function close() {
    pop.hidden = true;
    root.classList.remove("open", "drop-up");
    btn.setAttribute("aria-expanded", "false");
  }

  function visibleOptions() {
    return [...list.querySelectorAll("[role=option], .env-add")].filter(
      (el) => !el.hidden && !(el.parentElement && el.parentElement.hidden)
    );
  }

  // Arrows move by the grid: left and right by one tile, up and down by a row.
  function move(from, key) {
    const all = visibleOptions();
    const i = all.indexOf(from);
    if (i < 0) return;
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -COLUMNS, ArrowDown: COLUMNS }[key];
    let next = i + step;
    if (next < 0 && key === "ArrowUp") return filter.focus();
    if (next >= 0 && next < all.length) all[next].focus();
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    pop.hidden ? open() : close();
  });
  btn.addEventListener("keydown", (e) => {
    if (pop.hidden && ["ArrowDown", "Enter", " "].includes(e.key)) {
      e.preventDefault();
      open();
    }
  });
  filter.addEventListener("input", applyFilter);
  filter.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      const first = visibleOptions()[0];
      if (first) first.focus();
    }
  });
  pop.addEventListener("click", (e) => e.stopPropagation());
  pop.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      btn.focus();
    } else if (e.target.matches("[role=option], .env-add") && e.key.startsWith("Arrow")) {
      e.preventDefault();
      move(e.target, e.key);
    }
  });
  document.addEventListener(
    "click",
    (e) => { if (!root.contains(e.target)) close(); },
    true
  );

  return {
    set(newItems, newValue) {
      items = newItems;
      value = newValue && newValue.length ? newValue.slice() : [defaultId()];
      renderList();
      paintTrigger();
      paintHint();
    },
    setMultiple(flag) {
      multiple = flag;
      if (!multiple && value.length > 1) {
        value = [value.find((v) => v !== defaultId())];
        paintSelection();
        paintTrigger();
        onChange(value.slice());
      }
      paintHint();
    },
    /* After the file dialog: the list gained the file, and it is picked the
       way a click on its tile would pick it. */
    chose(newItems, id) {
      items = newItems;
      value = value.filter((v) => byId(v));
      if (!value.length) value = [defaultId()];
      renderList();
      if (id && !value.includes(id)) {
        pick(id);
      } else {
        paintTrigger();
        onChange(value.slice());
      }
    },
    refused(text) {
      hint.hidden = false;
      hint.textContent = text;
    },
    get value() { return value.slice(); },
  };
}
