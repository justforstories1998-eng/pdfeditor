(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var ua = navigator.userAgent || "";
    var isMac = /Mac|iPhone|iPad|iPod/i.test(ua);

    var WIN_DOWNLOAD = "https://github.com/justforstories1998-eng/pdfeditor/releases/download/v1.4.0/PDFStudio-1.4.0-windows-portable.zip";
    var MAC_DOWNLOAD = "https://github.com/justforstories1998-eng/pdfeditor/releases";

    var btnMac = document.getElementById("btn-mac");
    var btnWindows = document.getElementById("btn-windows");

    if (btnMac) btnMac.setAttribute("href", MAC_DOWNLOAD);
    if (btnWindows) {
      btnWindows.setAttribute("href", WIN_DOWNLOAD);
      btnWindows.setAttribute("download", "");
    }

    var modKeys = document.querySelectorAll('[id^="mod-key"]');
    modKeys.forEach(function (el) {
      el.textContent = isMac ? "⌘" : "Ctrl";
    });

    document.querySelectorAll('a[href^="#"]').forEach(function (anchor) {
      anchor.addEventListener("click", function (e) {
        var targetId = this.getAttribute("href");
        if (!targetId || targetId === "#") return;
        var target = document.querySelector(targetId);
        if (target) {
          e.preventDefault();
          target.scrollIntoView({ behavior: "smooth", block: "start" });
        }
      });
    });
  });
})();
