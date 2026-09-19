/* Sales intelligence fragments. The core renders deal, deals and coach pages;
   this fills in the intelligence from the plugin's own routes. Plain JS, same
   origin only. Forms inside the fragments are ordinary POST forms, so app.js's
   confirm and submit-once handlers (delegated on document) apply to them. */
(function () {
  "use strict";

  function editing(el) {
    return el.querySelector("details.edit[open]") || el.contains(document.activeElement) &&
      /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  }

  function load(el) {
    fetch(el.getAttribute("data-intel-src"), { credentials: "same-origin", headers: { "Accept": "text/html" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (html) {
        el.innerHTML = html;
        var id = window.location.hash.slice(1);
        if (id && id !== el.id) {
          var target = document.getElementById(id);
          if (target && el.contains(target)) target.scrollIntoView({ block: "start" });
        }
        var auto = el.querySelector("[data-refresh]");
        if (auto) schedule(el, parseInt(auto.getAttribute("data-refresh"), 10) || 5);
      })
      .catch(function (err) {
        el.innerHTML = "";
        var p = document.createElement("p");
        p.className = "meta";
        p.textContent = "Could not load this section (" + err.message + "). Reload the page to try again.";
        el.appendChild(p);
      });
  }

  function schedule(el, seconds) {
    setTimeout(function () {
      if (document.visibilityState !== "visible" || editing(el)) { schedule(el, seconds); return; }
      load(el);
    }, seconds * 1000);
  }

  document.querySelectorAll("[data-intel-src]").forEach(load);

  // Deals list: health and next best action per row.
  var cells = document.querySelectorAll("[data-intel-health]");
  if (cells.length) {
    fetch("/intel/deals.json", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (data) {
        cells.forEach(function (td) {
          var d = data[td.getAttribute("data-intel-health")];
          if (!d || d.score === null || d.score === undefined) return;
          td.textContent = "";
          var n = document.createElement("span");
          n.className = "strong";
          n.textContent = d.score;
          var l = document.createElement("div");
          l.className = "meta";
          l.textContent = d.label || "";
          td.appendChild(n);
          td.appendChild(l);
        });
        document.querySelectorAll("[data-intel-nba]").forEach(function (td) {
          var d = data[td.getAttribute("data-intel-nba")];
          if (!d || !d.next_best_action) return;
          td.textContent = "";
          var a = document.createElement("div");
          a.textContent = d.next_best_action;
          td.appendChild(a);
          if (d.by_when) {
            var m = document.createElement("div");
            m.className = "meta";
            m.textContent = "by " + d.by_when + (d.owner === "me" ? " · you" : "");
            td.appendChild(m);
          }
        });
      })
      .catch(function () { /* the list still works without it */ });
  }
})();
