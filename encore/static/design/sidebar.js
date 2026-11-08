// Design Uniformity: the sidebar's Collapse button (spec 6.1, partials/sidebar_toggle.html). Collapsed, the sidebar
// is a 72px rail of icons. The choice is kept in this browser (localStorage "sidebar"); partials/theme_head.html
// applies it before the first paint. Plain script, no module, so it can load with defer anywhere.
(function () {
  var root = document.documentElement;
  function sync() {
    var collapsed = root.dataset.sidebar === "collapsed";
    document.querySelectorAll("[data-sidebar-toggle]").forEach(function (b) {
      b.setAttribute("aria-expanded", String(!collapsed));
      b.title = collapsed ? "Expand the sidebar" : "Collapse the sidebar";
      var label = b.querySelector("span");
      if (label) label.textContent = collapsed ? "Expand" : "Collapse";
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-sidebar-toggle]");
    if (!b) return;
    var collapsed = root.dataset.sidebar !== "collapsed";
    if (collapsed) root.dataset.sidebar = "collapsed"; else delete root.dataset.sidebar;
    try { localStorage.setItem("sidebar", collapsed ? "collapsed" : "open"); } catch (err) {}
    sync();
    window.dispatchEvent(new Event("resize"));          // charts and grids that size themselves redraw
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync); else sync();
})();
