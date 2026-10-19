(function () {
  "use strict";
  var doc = document;

  // ---- tabs: every section is already in the page; this only shows one and hides the rest
  var tabs = Array.prototype.slice.call(doc.querySelectorAll('[role="tab"]'));
  var panels = Array.prototype.slice.call(doc.querySelectorAll('[role="tabpanel"]'));

  function select(index, focus) {
    tabs.forEach(function (tab, i) {
      var on = i === index;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      if (panels[i]) { panels[i].classList.toggle("is-inactive", !on); }
    });
    var active = tabs[index];
    if (active) {
      if (focus) { active.focus(); }
      if (active.scrollIntoView) { active.scrollIntoView({ block: "nearest", inline: "center" }); }
    }
  }

  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { select(i, false); });
    tab.addEventListener("keydown", function (event) {
      var next = null;
      if (event.key === "ArrowRight") { next = (i + 1) % tabs.length; }
      else if (event.key === "ArrowLeft") { next = (i - 1 + tabs.length) % tabs.length; }
      else if (event.key === "Home") { next = 0; }
      else if (event.key === "End") { next = tabs.length - 1; }
      if (next !== null) { event.preventDefault(); select(next, true); }
    });
  });
  if (tabs.length) { select(0, false); }

  // ---- relative times ("27m ago"); the clock time written into the page is the fallback without JavaScript
  function relTime(ms) {
    var minutes = ms / 60000;
    if (minutes < 1) { return "just now"; }
    if (minutes < 60) { return Math.floor(minutes) + "m ago"; }
    var hours = minutes / 60;
    if (hours < 24) { return Math.floor(hours) + "h ago"; }
    return Math.floor(hours / 24) + "d ago";
  }

  function updateTimes() {
    var now = Date.now();
    Array.prototype.forEach.call(doc.querySelectorAll("time[datetime]"), function (el) {
      var then = Date.parse(el.getAttribute("datetime"));
      if (isNaN(then)) { return; }
      if (!el.title) { el.title = el.textContent; }
      el.textContent = relTime(Math.max(0, now - then));
    });
  }
  updateTimes();
  setInterval(updateTimes, 60000);
  doc.addEventListener("visibilitychange", function () { if (!doc.hidden) { updateTimes(); } });

  // ---- a thumbnail that fails to load disappears, and the headline takes the full width
  function dropThumb(img) {
    var top = img.closest ? img.closest(".top") : null;
    var thumb = img.closest ? img.closest(".thumb") : null;
    if (thumb && thumb.parentNode) { thumb.parentNode.removeChild(thumb); }
    if (top) { top.classList.add("noimg"); }
  }
  Array.prototype.forEach.call(doc.querySelectorAll(".thumb img"), function (img) {
    img.addEventListener("error", function () { dropThumb(img); });
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) { dropThumb(img); }
  });

  // ---- the note in the header opens the status log
  var status = doc.getElementById("status");
  function openStatus() { if (status) { status.open = true; } }
  Array.prototype.forEach.call(doc.querySelectorAll('a[href="#status"]'), function (a) {
    a.addEventListener("click", openStatus);
  });
  if (location.hash === "#status") { openStatus(); }

  window.plainNews = { relTime: relTime, select: select };
})();
