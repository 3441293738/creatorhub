// Shared navigation/accessibility behavior; no account or task writes.
(function () {
  "use strict";
  const byId = id => document.getElementById(id);
  const sidebar = byId("main-sidebar"), toggle = byId("nav-toggle");
  const mobile = window.matchMedia("(max-width: 860px)");
  let drawerOpen = false;
  const inertBefore = new Map();
  function closeDrawer(restore = true) {
    if (!drawerOpen) return;
    drawerOpen = false;
    document.body.classList.remove("nav-open");
    sidebar.removeAttribute("role"); sidebar.removeAttribute("aria-modal");
    toggle.setAttribute("aria-expanded", "false");
    inertBefore.forEach((value, node) => { node.inert = value; });
    inertBefore.clear();
    if (mobile.matches && globalThis.CreatorHubMotion) {
      document.body.classList.add("nav-closing");
      globalThis.CreatorHubMotion.exit(sidebar, () => document.body.classList.remove("nav-closing"), { transform: "translateX(-20px)" });
    }
    if (restore) toggle.focus({ preventScroll: true });
  }
  function openDrawer() {
    if (!mobile.matches || drawerOpen) return;
    drawerOpen = true;
    globalThis.CreatorHubMotion?.cancelExit(sidebar);
    document.body.classList.remove("nav-closing");
    document.body.classList.add("nav-open");
    sidebar.setAttribute("role", "dialog"); sidebar.setAttribute("aria-modal", "true");
    toggle.setAttribute("aria-expanded", "true");
    [document.querySelector("header"), byId("main-content"), byId("backtop")].forEach(node => {
      if (node) { inertBefore.set(node, node.inert); node.inert = true; }
    });
    byId("nav-close").focus();
  }
  toggle.addEventListener("click", openDrawer);
  byId("nav-close").addEventListener("click", () => closeDrawer());
  byId("nav-backdrop").addEventListener("click", () => closeDrawer());
  sidebar.addEventListener("click", event => {
    if (drawerOpen && event.target.closest("[data-tab]")) {
      closeDrawer(false);
      byId("page-title").focus({ preventScroll: true });
    }
  });
  mobile.addEventListener("change", () => {
    if (!mobile.matches) {
      closeDrawer(false); globalThis.CreatorHubMotion?.cancelExit(sidebar);
      document.body.classList.remove("nav-closing");
    }
  });
  document.addEventListener("keydown", event => {
    if (!drawerOpen) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); closeDrawer(); }
    if (event.key !== "Tab") return;
    const nodes = [...sidebar.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled)')].filter(node => node.offsetParent !== null);
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }, true);

  const dialog = byId("command-dialog"), search = byId("command-search"), results = byId("command-results");
  let searchTrigger = null, commandNavigated = false;
  function renderResults() {
    const query = search.value.trim().toLocaleLowerCase();
    results.replaceChildren();
    [...sidebar.querySelectorAll("[data-tab]")].filter(nav => !nav.classList.contains("hidden")).forEach(nav => {
      const name = nav.dataset.tab;
      const meta = PAGE_META[name];
      const label = nav.querySelector(".nav-label").textContent;
      if (query && !`${label} ${meta.title} ${meta.desc}`.toLocaleLowerCase().includes(query)) return;
      const button = document.createElement("button");
      button.className = "command-result"; button.type = "button";
      const icon = nav.querySelector("svg").cloneNode(true); icon.setAttribute("aria-hidden", "true");
      const copy = document.createElement("span"), title = document.createElement("b"), detail = document.createElement("small");
      title.textContent = meta.title; detail.textContent = meta.desc;
      copy.append(title, detail); button.append(icon, copy);
      button.addEventListener("click", () => { commandNavigated = true; dialog.close(); switchTab(name, true); byId("page-title").focus({ preventScroll: true }); });
      results.append(button);
    });
    if (!results.children.length) {
      const empty = document.createElement("p");
      empty.className = "command-empty"; empty.setAttribute("role", "status");
      empty.textContent = "没有找到匹配功能，试试「账号」「下载」或「设置」。";
      results.append(empty);
    }
  }
  function openSearch() {
    if (dialog.open || _visibleModal() || globalThis.CreatorHubWorkbench?.isInteracting?.()) return;
    searchTrigger = drawerOpen ? toggle : document.activeElement;
    commandNavigated = false;
    closeDrawer(false);
    search.value = ""; renderResults(); document.body.classList.add("command-open"); dialog.showModal(); search.focus();
  }
  byId("command-trigger").addEventListener("click", openSearch);
  byId("command-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", event => { event.preventDefault(); dialog.close(); });
  dialog.addEventListener("close", () => {
    document.body.classList.remove("command-open");
    if (!commandNavigated && searchTrigger?.isConnected) searchTrigger.focus({ preventScroll: true });
  });
  search.addEventListener("input", renderResults);
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("keydown", event => {
    if (event.isComposing) return;
    // A search input normally consumes Escape to clear itself; our visible hint
    // promises dismissal, including when the query has no matches.
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dialog.close(); return; }
    const buttons = [...results.querySelectorAll("button")];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === "Enter" && document.activeElement === search && buttons.length) {
      event.preventDefault(); buttons[0].click();
    } else if (["ArrowDown", "ArrowUp"].includes(event.key) && buttons.length) {
      event.preventDefault();
      const next = index < 0 ? (event.key === "ArrowDown" ? 0 : buttons.length - 1) : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    }
  });
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k" && !event.isComposing) {
      event.preventDefault(); openSearch();
    }
  });

  // Keyboard users can scroll dense tables, and identify their containing module.
  document.querySelectorAll(".table-wrap").forEach(wrapper => {
    wrapper.tabIndex = 0;
    wrapper.setAttribute("role", "region");
    const title = wrapper.closest(".card")?.querySelector("h2")?.textContent.trim() || "数据";
    wrapper.setAttribute("aria-label", `${title}表格，可横向滚动`);
  });
  document.querySelectorAll(".navitem svg, .card-ic svg, .stat-ic svg, .qa-ic svg").forEach(icon => icon.setAttribute("aria-hidden", "true"));
})();
