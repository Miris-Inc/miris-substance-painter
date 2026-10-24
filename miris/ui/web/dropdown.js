/* Combobox dropdown for dialog.js, loaded as a plain script before it. */
"use strict";

function createDropdown(root, onChange) {
  root.classList.add("dropdown");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "dropdown-btn";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  btn.innerHTML =
    '<span class="dropdown-label"></span>' +
    '<svg class="chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
  const menu = document.createElement("ul");
  menu.className = "dropdown-menu";
  menu.hidden = true;
  menu.setAttribute("role", "listbox");
  root.append(btn, menu);

  let options = [];
  let value = null;
  let activeIndex = -1;

  const label = () => btn.querySelector(".dropdown-label");

  function renderMenu() {
    menu.innerHTML = "";
    options.forEach((opt, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(opt.value === value));
      li.classList.toggle("active", i === activeIndex);
      const name = document.createElement("span");
      name.textContent = opt.label;
      li.appendChild(name);
      li.insertAdjacentHTML(
        "beforeend",
        '<svg class="check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>'
      );
      li.addEventListener("click", (e) => {
        // Inside a <label> the browser forwards a synthetic click to the
        // label's control, the dropdown's own button, which reopened the menu.
        e.stopPropagation();
        select(opt.value);
      });
      menu.appendChild(li);
    });
  }

  function select(v) {
    close();
    if (v === value) return;
    value = v;
    label().textContent = (options.find((o) => o.value === v) || {}).label || "";
    renderMenu();
    onChange(v);
  }

  /* Keep the menu inside the window rather than growing it. The window is
     sized to the page, so a menu running past the bottom is either clipped or
     makes the dialog jump taller as it opens. */
  function place() {
    root.classList.remove("drop-up");
    menu.style.maxHeight = "";

    const GAP = 4;      // matches the offset in the stylesheet
    const MARGIN = 8;   // never flush against the window edge
    const box = btn.getBoundingClientRect();
    const below = window.innerHeight - box.bottom - GAP - MARGIN;
    const above = box.top - GAP - MARGIN;
    const wanted = menu.scrollHeight;

    // Only flip when down does not fit AND up is genuinely roomier: flipping
    // into an even tighter space would just scroll in a worse place.
    const up = wanted > below && above > below;
    if (up) root.classList.add("drop-up");
    menu.style.maxHeight = Math.max(72, up ? above : below) + "px";
  }

  function open() {
    activeIndex = options.findIndex((o) => o.value === value);
    renderMenu();
    menu.hidden = false;
    root.classList.add("open");
    btn.setAttribute("aria-expanded", "true");
    place();  // after it is visible, or it cannot be measured
  }

  function close() {
    menu.hidden = true;
    root.classList.remove("open");
    root.classList.remove("drop-up");
    btn.setAttribute("aria-expanded", "false");
  }

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });
  btn.addEventListener("keydown", (e) => {
    if (menu.hidden) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
        e.preventDefault();
        open();
      }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = Math.min(activeIndex + 1, options.length - 1);
      renderMenu();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      renderMenu();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (activeIndex >= 0) select(options[activeIndex].value);
    }
  });
  // capture phase so the popover's stopPropagation can't keep menus stuck open
  document.addEventListener(
    "click",
    (e) => { if (!root.contains(e.target)) close(); },
    true
  );

  return {
    disable(flag) {
      // A locked picker still shows its value; it just does not open.
      if (flag) close();
      btn.disabled = flag;
      root.classList.toggle("is-disabled", flag);
    },
    set(newOptions, newValue) {
      options = newOptions;
      value = newValue;
      label().textContent = (options.find((o) => o.value === value) || {}).label || "";
      renderMenu();
    },
    get value() { return value; },
  };
}
