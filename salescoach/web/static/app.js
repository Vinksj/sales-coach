/* Sales coach UI: confirm dialogs, one submit per click, evidence audio,
   auto-refresh while the pipeline runs, and the live transcript over SSE.
   Plain JS, no libraries, no network calls other than this app's own. */
(function () {
  "use strict";

  // Confirm before anything irreversible or external. Send names its recipients.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-confirm], button[data-confirm-send]");
    if (!btn || btn.disabled) return;
    var msg = btn.getAttribute("data-confirm");
    if (btn.hasAttribute("data-confirm-send") && btn.form) {
      var to = (btn.form.querySelector("[name=to]") || {}).value || "";
      var cc = (btn.form.querySelector("[name=cc]") || {}).value || "";
      msg = "Send this email now from your Gmail?\n\nTo: " + (to.trim() || "(nobody)") +
        (cc.trim() ? "\nCc: " + cc.trim() : "") + "\n\nIt cannot be unsent." +
        (btn.dataset.warn ? "\n\n" + btn.dataset.warn : "");
    }
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);

  // One submit per form: a double click must never post twice.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.method && form.method.toLowerCase() === "get") return;
    if (form.dataset.submitted) { e.preventDefault(); return; }
    form.dataset.submitted = "1";
    setTimeout(function () {
      form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    }, 0);
  });
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("form[data-submitted]").forEach(function (f) {
      delete f.dataset.submitted;
      f.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
    });
  });

  // Auto-refresh while work is running, unless the user is typing or editing.
  var dirty = false;
  document.addEventListener("input", function () { dirty = true; });
  var every = parseInt(document.body.dataset.autorefresh || "0", 10);
  if (every > 0) {
    setInterval(function () {
      if (dirty || document.visibilityState !== "visible") return;
      if (document.querySelector("details.edit[open], details.add-loop[open]")) return;
      window.location.reload();
    }, every * 1000);
  }

  // Evidence audio: one clip at a time.
  var audio = null;
  var playing = null;
  function resetBtn(btn) { btn.classList.remove("playing"); btn.textContent = "▶"; }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button.play");
    if (!btn) return;
    e.preventDefault();
    if (playing === btn && audio && !audio.paused) { audio.pause(); return; }
    if (audio) audio.pause();
    audio = new Audio(btn.dataset.src);
    playing = btn;
    btn.classList.add("playing");
    btn.textContent = "■";
    audio.addEventListener("pause", function () { resetBtn(btn); });
    audio.addEventListener("ended", function () { resetBtn(btn); });
    audio.addEventListener("error", function () {
      resetBtn(btn);
      btn.classList.add("broken");
      btn.title = "No audio for this moment";
    });
    audio.play().catch(function () { resetBtn(btn); });
  });

  // Evidence links jump into the transcript, opening it first.
  function openTarget() {
    var id = window.location.hash.slice(1);
    if (!/^t\d+$/.test(id)) return;
    var el = document.getElementById(id);
    if (!el) return;
    var d = el.closest("details");
    if (d && !d.open) d.open = true;
    el.scrollIntoView({ block: "center" });
  }
  window.addEventListener("hashchange", openTarget);
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a.tref");
    if (a && a.getAttribute("href").charAt(0) === "#") setTimeout(openTarget, 0);
  });
  openTarget();

  // ---- live call view ------------------------------------------------------
  var live = document.getElementById("live");
  if (live && live.dataset.events) startLive(live);

  function mmss(s) {
    if (s === null || s === undefined || isNaN(s)) return "";
    s = Math.max(0, Math.floor(s));
    var m = Math.floor(s / 60), r = s % 60;
    return (m < 10 ? "0" : "") + m + ":" + (r < 10 ? "0" : "") + r;
  }

  function startLive(root) {
    var silenceS = parseFloat(root.dataset.silence || "60");
    var pane = document.getElementById("live-pane");
    var list = document.getElementById("live-turns");
    var statusEl = document.getElementById("live-status");
    var alerts = document.getElementById("live-alerts");
    var stick = true;

    pane.addEventListener("scroll", function () {
      stick = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 48;
    });
    function toBottom() { if (stick) pane.scrollTop = pane.scrollHeight; }
    toBottom();

    var started = Date.parse(root.dataset.started || "");
    var elapsed = document.getElementById("live-elapsed");
    if (elapsed && !isNaN(started)) {
      setInterval(function () { elapsed.textContent = mmss((Date.now() - started) / 1000); }, 1000);
    }

    function onLevel(m) {
      ["me", "them"].forEach(function (ch) {
        var d = m[ch];
        var box = document.getElementById("meter-" + ch);
        if (!d || !box) return;
        var pct = Math.max(0, Math.min(100, ((d.rms_dbfs + 60) / 60) * 100));
        box.querySelector(".meter-fill").style.width = (d.receiving ? pct : 0) + "%";
        box.querySelector(".meter-db").textContent = d.receiving ? Math.round(d.rms_dbfs) + " dBFS" : "no frames";
        var silent = !d.receiving || d.silent_s >= silenceS;
        box.classList.toggle("silent", silent);
        box.querySelector(".meter-note").textContent = !d.receiving ? "Not receiving audio on this channel."
          : (d.silent_s >= silenceS ? "Silent for " + Math.round(d.silent_s) + " s. Check this channel."
            : (d.silent_s > 5 ? "Quiet for " + Math.round(d.silent_s) + " s" : "Receiving audio"));
      });
    }

    function onSegment(m) {
      var empty = document.getElementById("live-empty");
      if (empty) empty.remove();
      var li = document.createElement("li");
      li.className = "turn turn-" + (m.channel === "me" ? "me" : "them");
      var meta = document.createElement("span");
      meta.className = "turn-meta";
      var who = document.createElement("span");
      who.className = "who";
      who.textContent = m.channel === "me" ? "Me" : "Them";
      meta.appendChild(who);
      meta.appendChild(document.createTextNode(" " + mmss(m.t_start)));
      var text = document.createElement("span");
      text.className = "turn-text";
      text.textContent = m.text || "";
      li.appendChild(meta);
      li.appendChild(text);
      list.appendChild(li);
      toBottom();
    }

    function onAlert(m) {
      var li = document.createElement("li");
      var label = document.createElement("span");
      label.className = "label";
      label.textContent = String(m.kind || "alert").replace(/_/g, " ");
      li.appendChild(label);
      li.appendChild(document.createTextNode(" " + (m.message || "")));
      alerts.insertBefore(li, alerts.firstChild);
      alerts.hidden = false;
    }

    function onStatus(m) {
      if (m.event === "log") return;
      if (m.event === "heartbeat") {
        statusEl.textContent = "Capture healthy · last heartbeat " + new Date().toLocaleTimeString();
        return;
      }
      statusEl.textContent = String(m.event || "status").replace(/_/g, " ") + (m.message ? ": " + m.message : "");
    }

    var es = new EventSource(root.dataset.events);
    es.onopen = function () { statusEl.textContent = "Connected to the capture stream"; };
    es.onmessage = function (e) {
      var m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.type === "level") onLevel(m);
      else if (m.type === "segment") onSegment(m);
      else if (m.type === "alert") onAlert(m);
      else if (m.type === "status") onStatus(m);
      else if (m.type === "ended") {
        es.close();
        statusEl.textContent = "Call ended. Opening the review.";
        setTimeout(function () { window.location.href = root.dataset.callUrl; }, 800);
      }
    };
    es.onerror = function () {
      statusEl.textContent = "Lost the capture stream. Reconnecting. The recording itself is not affected.";
    };
  }
})();
