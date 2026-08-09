(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var ua = navigator.userAgent || "";
    var isMac = /Mac|iPhone|iPad|iPod/i.test(ua);

    // Direct download URLs — update these with your actual release URLs
    var DOWNLOAD_URLS = {
      mac: "https://github.com/pdfstudio/pdfstudio/releases/latest/download/PDFStudio-latest.dmg",
      windows: "https://github.com/pdfstudio/pdfstudio/releases/latest/download/PDFStudio-latest-setup.exe"
    };

    // Show only the relevant download button
    var btnMac = document.getElementById("btn-mac");
    var btnWindows = document.getElementById("btn-windows");

    if (isMac) {
      if (btnWindows) btnWindows.style.display = "none";
      if (btnMac) {
        btnMac.setAttribute("href", DOWNLOAD_URLS.mac);
        btnMac.setAttribute("download", "");
      }
    } else {
      if (btnMac) btnMac.style.display = "none";
      if (btnWindows) {
        btnWindows.setAttribute("href", DOWNLOAD_URLS.windows);
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
