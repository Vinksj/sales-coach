/* An upload bigger than the server takes is refused here, before it is sent: on a serverless deployment the
   platform itself refuses a body over its limit with a bare error page, so the page says it first.
   Any <input type="file" data-max-bytes="N" data-max-label="4.4 MB"> is checked when its form is submitted. */
(function () {
  document.querySelectorAll("input[type=file][data-max-bytes]").forEach(function (input) {
    var form = input.form;
    if (!form) return;
    form.addEventListener("submit", function (ev) {
      var max = parseInt(input.dataset.maxBytes, 10), file = input.files && input.files[0];
      if (file && max && file.size > max) {
        ev.preventDefault();
        var msg = form.querySelector(".upload-too-large");
        if (!msg) {
          msg = document.createElement("p");
          msg.className = "callout upload-too-large";
          form.insertBefore(msg, form.firstChild.nextSibling);
        }
        msg.textContent = "Too large: this file is " + (file.size / 1048576).toFixed(1) + " MB and the limit is " +
          (input.dataset.maxLabel || max + " bytes") + ". Split the transcript, or connect your recorder instead.";
      }
    });
  });
})();
