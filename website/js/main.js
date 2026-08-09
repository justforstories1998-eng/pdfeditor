(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var ua = navigator.userAgent || "";
    var isMac = /Mac|iPhone|iPad|iPod/i.test(ua);

    // Direct download URL — portable zip for Windows, release page for Mac
    var WIN_DOWNLOAD = "https://github.com/justforstories1998-eng/pdfeditor/releases/download/v1.4.0/PDFStudio-1.4.0-windows-portable.zip";
    var RELEASES_URL = "https://github.com/justforstories1998-eng/pdfeditor/releases";

    // Show only the relevant download button
    var btnMac = document.getElementById("btn-mac");
    var btnWindows = document.getElementById("btn-windows");

    if (isMac) {
      if (btnWindows) btnWindows.style.display = "none";
      if (btnMac) btnMac.setAttribute("href", RELEASES_URL);
    } else {
      if (btnMac) btnMac.style.display = "none";
      if (btnWindows) {
        btnWindows.setAttribute("href", WIN_DOWNLOAD);
        btnWindows.setAttribute("download", "");
      }
    }

    // Update modifier key display (⌘ on Mac, Ctrl on Windows)
    var modKeys = document.querySelectorAll('[id^="mod-key"]');
    modKeys.forEach(function (el) {
      el.textContent = isMac ? "⌘" : "Ctrl";
    });

    // Smooth scroll for anchor links
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
