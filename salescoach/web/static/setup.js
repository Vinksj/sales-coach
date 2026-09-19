/* Setup wizard: Load models and Test connection without leaving the page, copy buttons, and the
   short id made from a methodology's name. Plain JS, no libraries; every page works without it
   (the same buttons are ordinary form posts). A typed API key is sent once, in the POST body, and
   the field is cleared as soon as the server has answered. */
(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function callout(kind, label, text) {
    var box = el("div", "callout " + (kind === "good" ? "callout-good" : "callout-err"));
    box.appendChild(el("div", "label", label));
    box.appendChild(document.createTextNode(text));
    return box;
  }

  function fillSelect(select, models) {
    var current = select.value;
    select.textContent = "";
    if (!current || models.indexOf(current) === -1) {
      var first = el("option", null, current ? current : "Choose a model");
      first.value = current || "";
      select.appendChild(first);
    }
    models.forEach(function (m) {
      var option = el("option", null, m);
      option.value = m;
      if (m === current) option.selected = true;
      select.appendChild(option);
    });
  }

  function keyStatus(form, isSet) {
    var pill = form.querySelector("[data-key-status]");
    var input = form.querySelector("input[name=api_key]");
    if (input) {
      input.value = "";
      if (isSet) input.placeholder = "Leave empty to keep the stored key";
    }
    if (!pill) return;
    pill.className = "pill key-status " + (isSet ? "pill-good" : "pill-muted");
    pill.textContent = "";
    pill.appendChild(el("i"));
    pill.appendChild(document.createTextNode(isSet ? "key is set" : "not set"));
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-ajax]");
    if (!btn || !btn.form || !window.fetch || !window.FormData) return;
    e.preventDefault();
    if (btn.getAttribute("aria-busy") === "true") return;
    var form = btn.form;
    var result = document.getElementById("model-result");
    var label = btn.textContent;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = btn.dataset.busy || "Working";
    fetch(btn.formAction, {method: "POST", body: new FormData(form), credentials: "same-origin",
                           headers: {"Accept": "application/json"}})
      .then(function (r) { return r.json(); })
      .then(function (data) {
        keyStatus(form, !!data.key_set);
        result.textContent = "";
        if (btn.dataset.ajax === "models") {
          if (data.ok) {
            form.querySelectorAll("select[data-tier]").forEach(function (s) { fillSelect(s, data.models); });
            result.appendChild(callout("good", "Models loaded", data.models.length + " models. Choose one for each tier."));
          } else {
            result.appendChild(callout("err", "Could not load the models", data.error || "The provider did not answer."));
          }
        } else if (data.ok) {
          result.appendChild(callout("good", "Connection works",
            (data.model || "The model") + " answered in " + ((data.latency_ms || 0) / 1000).toFixed(1) + " seconds."));
        } else {
          result.appendChild(callout("err", "Could not connect", data.error || "The provider did not answer."));
        }
      })
      .catch(function () {
        result.textContent = "";
        result.appendChild(callout("err", "Something went wrong", "The coach did not answer. Is it still running?"));
      })
      .then(function () {
        btn.removeAttribute("aria-busy");
        btn.textContent = label;
      });
  });

  // Copy buttons: data-copy names the element whose text is copied.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-copy]");
    if (!btn) return;
    var source = document.querySelector(btn.dataset.copy);
    if (!source) return;
    var text = source.textContent;
    var done = function () {
      var was = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = was; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { selectText(source); });
    } else {
      selectText(source);
    }
  });
  function selectText(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // The once-shown webhook secret: bring it into view and to the screen reader.
  var secret = document.getElementById("webhook-secret");
  if (secret) secret.focus();

  // Methodology builder: the short id follows the name until the user edits the id.
  var builder = document.querySelector("form[data-builder]");
  if (builder) {
    var name = builder.querySelector("input[name=name]");
    var key = builder.querySelector("input[name=key]");
    if (name && key) {
      var touched = key.value !== "";
      key.addEventListener("input", function () { touched = key.value !== ""; });
      name.addEventListener("input", function () {
        if (touched) return;
        key.value = name.value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
      });
    }
  }
})();
