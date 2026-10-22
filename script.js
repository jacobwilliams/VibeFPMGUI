// VibeFPMGUI website: light/dark toggle, Tests/Run tabs, image lightbox,
// and sections fading in as they scroll into view.
(function () {
  "use strict";
  var root = document.documentElement;

  // ---- light / dark mode (remembered; the system setting is the default)
  var toggle = document.getElementById("theme-toggle");
  function setTheme(theme, remember) {
    root.setAttribute("data-theme", theme);
    toggle.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    if (remember) {
      try { localStorage.setItem("vibefpmgui-theme", theme); } catch (e) { /* storage blocked */ }
    }
  }
  setTheme(root.getAttribute("data-theme") || "dark", false);
  toggle.addEventListener("click", function () {
    setTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
  });
  // Follow the system while the visitor hasn't chosen.
  try {
    matchMedia("(prefers-color-scheme: light)").addEventListener("change", function (e) {
      var saved = null;
      try { saved = localStorage.getItem("vibefpmgui-theme"); } catch (err) { /* ignore */ }
      if (!saved) setTheme(e.matches ? "light" : "dark", false);
    });
  } catch (e) { /* old browsers */ }

  // ---- tabs (Tests / Run screenshots)
  document.querySelectorAll("[data-tabs]").forEach(function (tabs) {
    var section = tabs.closest("section");
    tabs.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.querySelectorAll(".tab").forEach(function (t) { t.classList.toggle("active", t === tab); });
        section.querySelectorAll(".tab-panel").forEach(function (panel) {
          panel.classList.toggle("active", panel.getAttribute("data-panel") === tab.getAttribute("data-tab"));
        });
      });
    });
  });

  // ---- lightbox: click a screenshot to see it full size
  var box = document.getElementById("lightbox");
  var boxImg = box.querySelector("img");
  function closeBox() { box.hidden = true; boxImg.removeAttribute("src"); }
  document.querySelectorAll(".shot img, .window img, .about-strip img").forEach(function (img) {
    img.addEventListener("click", function () {
      boxImg.src = img.currentSrc || img.src;
      boxImg.alt = img.alt;
      box.hidden = false;
    });
  });
  box.addEventListener("click", closeBox);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !box.hidden) closeBox(); });

  // ---- reveal sections as they come into view
  var targets = document.querySelectorAll(".card, .split, .stacked, .shot.full, .grid-2 > div, .grid-3 > *, .window.wide, .code-card, .about-strip");
  var animate = "IntersectionObserver" in window && !/[?&]static\b/.test(location.search);
  if (animate) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          observer.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    targets.forEach(function (el) {
      // Only what starts below the screen fades in; the rest is just shown.
      if (el.getBoundingClientRect().top < window.innerHeight) return;
      el.classList.add("reveal");
      observer.observe(el);
    });
  }
})();
