/* Live coach card on the live console: one nudge at a time, auto-fade, dismiss.
   Fed by /coach/live/stream (the same stream the macOS overlay reads). */
(function () {
  var root = document.getElementById("coach-live");
  if (!root || root.hidden || !root.dataset.stream) return;
  var callId = root.dataset.call;
  var card = document.getElementById("coach-card");
  var trig = document.getElementById("coach-trigger");
  var text = document.getElementById("coach-text");
  var dismissBtn = document.getElementById("coach-dismiss");
  var statusText = document.getElementById("coach-status-text");
  var current = null, fadeTimer = null, hideTimer = null;

  function hide() {
    clearTimeout(fadeTimer); clearTimeout(hideTimer);
    card.classList.remove("in", "fading");
    card.hidden = true;
    current = null;
  }

  function fade() {
    card.classList.add("fading");
    hideTimer = setTimeout(hide, 650);
  }

  function show(m) {
    if (m.call_id && callId && m.call_id !== callId) return;
    clearTimeout(fadeTimer); clearTimeout(hideTimer);
    current = m;
    trig.textContent = m.label || String(m.trigger || "").replace(/_/g, " ");
    text.textContent = m.text || "";
    card.hidden = false;
    card.classList.remove("fading", "in");
    void card.offsetWidth;
    card.classList.add("in");
    fadeTimer = setTimeout(fade, (Number(m.ttl_s) || 12) * 1000);
  }

  function status(m) {
    var mine = m.state === "listening" && (!m.call_id || m.call_id === callId);
    root.classList.toggle("listening", mine);
    statusText.textContent = mine ? (m.mode === "replay" ? "Coach replaying" : "Coach listening")
      : (m.state === "listening" ? "Coach on another call" : "Coach idle");
  }

  dismissBtn.addEventListener("click", function () {
    if (!current) return;
    var url = "/coach/live/" + encodeURIComponent(current.call_id || callId) + "/nudges/" + current.id + "/dismiss";
    fetch(url, { method: "POST", credentials: "same-origin" }).catch(function () {});
    hide();
  });

  var es = new EventSource(root.dataset.stream);
  es.onmessage = function (e) {
    var m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (m.type === "nudge") show(m);
    else if (m.type === "clear") { if (current && current.id === m.id) hide(); }
    else if (m.type === "status") status(m);
  };
  es.onerror = function () { statusText.textContent = "Coach reconnecting"; root.classList.remove("listening"); };
})();
