/**
 * Frigate ABR Overlay
 *
 * Injected into the Frigate UI via nginx sub_filter. Adds adaptive bitrate
 * streaming support by:
 * 1. Rewriting HLS VOD URLs to the ABR sidecar's master playlist
 * 2. Intercepting WebSocket connections for live stream quality switching
 * 3. Adding a quality selector UI to video players
 *
 * Graceful degradation: if the ABR sidecar is unreachable or disabled,
 * this script does nothing and Frigate works normally.
 */
(function () {
  "use strict";

  var ABR_CONFIG_URL = "/abr/config";
  var STORAGE_KEY_LIVE = "frigate-abr-live-quality";
  var STORAGE_KEY_RECORDING = "frigate-abr-recording-quality";

  var abrConfig = null;
  var abrEnabled = false;

  // --- Initialization ---

  function init() {
    fetchConfig()
      .then(function (cfg) {
        abrConfig = cfg;
        abrEnabled = cfg && cfg.enabled && cfg.tiers && cfg.tiers.length > 0;
        if (!abrEnabled) return;

        console.log("[ABR] Enabled with tiers:", cfg.tiers.map(function (t) { return t.name; }));

        interceptHlsLoadSource();
        interceptWebSocket();
        observePlayerMounts();
      })
      .catch(function () {
        // Sidecar not running - silently disable
        console.log("[ABR] Sidecar unavailable, ABR disabled");
      });
  }

  function fetchConfig() {
    return fetch(ABR_CONFIG_URL).then(function (r) {
      if (!r.ok) throw new Error("ABR config fetch failed");
      return r.json();
    });
  }

  // --- Recording ABR: Intercept HLS.loadSource ---

  function interceptHlsLoadSource() {
    // Wait for Hls to be available (hls.js is loaded async by the app)
    var checkInterval = setInterval(function () {
      if (typeof Hls === "undefined") return;
      clearInterval(checkInterval);

      var origLoadSource = Hls.prototype.loadSource;
      Hls.prototype.loadSource = function (url) {
        try {
          var quality = getRecordingQuality();
          if (quality !== "original" && isVodUrl(url)) {
            var abrUrl = rewriteVodUrl(url);
            console.log("[ABR] Rewriting HLS source:", url, "->", abrUrl);
            return origLoadSource.call(this, abrUrl);
          }
        } catch (e) {
          console.warn("[ABR] HLS intercept error:", e);
        }
        return origLoadSource.call(this, url);
      };

      console.log("[ABR] HLS loadSource intercepted");
    }, 500);

    // Give up after 30 seconds
    setTimeout(function () { clearInterval(checkInterval); }, 30000);
  }

  function isVodUrl(url) {
    return url && /\/vod\/[^/]+\/start\//.test(url) && url.indexOf("/abr/") === -1;
  }

  function rewriteVodUrl(url) {
    // /vod/{camera}/start/{ts}/end/{ts}/master.m3u8
    // -> /abr/vod/{camera}/start/{ts}/end/{ts}/master.m3u8
    return url.replace(/\/vod\//, "/abr/vod/");
  }

  // --- Live ABR: Intercept WebSocket connections ---

  var OriginalWebSocket = window.WebSocket;

  function interceptWebSocket() {
    window.WebSocket = function (url, protocols) {
      try {
        var quality = getLiveQuality();
        if (quality !== "original" && quality !== "auto" && isLiveWsUrl(url)) {
          var newUrl = rewriteLiveWsUrl(url, quality);
          console.log("[ABR] Rewriting live WS:", url, "->", newUrl);
          url = newUrl;
        }
      } catch (e) {
        console.warn("[ABR] WebSocket intercept error:", e);
      }

      if (protocols !== undefined) {
        return new OriginalWebSocket(url, protocols);
      }
      return new OriginalWebSocket(url);
    };

    // Preserve prototype chain and static properties
    window.WebSocket.prototype = OriginalWebSocket.prototype;
    window.WebSocket.CONNECTING = OriginalWebSocket.CONNECTING;
    window.WebSocket.OPEN = OriginalWebSocket.OPEN;
    window.WebSocket.CLOSING = OriginalWebSocket.CLOSING;
    window.WebSocket.CLOSED = OriginalWebSocket.CLOSED;
  }

  function isLiveWsUrl(url) {
    return url && (/\/live\/mse\/api\/ws\?/.test(url) || /\/live\/webrtc\/api\/ws\?/.test(url));
  }

  var ABR_VARIANT_PREFIX = "_abr_";

  function rewriteLiveWsUrl(url, quality) {
    // Change src=camera_name to src=camera_name_abr_720p
    var tierSuffix = ABR_VARIANT_PREFIX + quality;
    return url.replace(/([?&]src=)([^&]+)/, function (match, prefix, camera) {
      // Strip existing ABR suffix if present (e.g., _abr_1080p -> base name)
      var abrIdx = camera.indexOf(ABR_VARIANT_PREFIX);
      if (abrIdx !== -1) {
        camera = camera.substring(0, abrIdx);
      }
      return prefix + camera + tierSuffix;
    });
  }

  // --- Quality Persistence ---

  function getLiveQuality() {
    try {
      return localStorage.getItem(STORAGE_KEY_LIVE) || "original";
    } catch (e) {
      return "original";
    }
  }

  function setLiveQuality(q) {
    try { localStorage.setItem(STORAGE_KEY_LIVE, q); } catch (e) { /* noop */ }
  }

  function getRecordingQuality() {
    try {
      return localStorage.getItem(STORAGE_KEY_RECORDING) || "auto";
    } catch (e) {
      return "auto";
    }
  }

  function setRecordingQuality(q) {
    try { localStorage.setItem(STORAGE_KEY_RECORDING, q); } catch (e) { /* noop */ }
  }

  // --- Quality Selector UI ---

  function observePlayerMounts() {
    // Watch for video elements appearing in the DOM
    var observer = new MutationObserver(function (mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var added = mutations[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) {
            processElement(added[j]);
          }
        }
      }
    });

    observer.observe(document.body, { childList: true, subtree: true });

    // Also process existing elements
    processElement(document.body);
  }

  function processElement(el) {
    // Look for video containers that don't already have our selector
    var videos = el.querySelectorAll ? el.querySelectorAll("video") : [];
    for (var i = 0; i < videos.length; i++) {
      var video = videos[i];
      var container = findPlayerContainer(video);
      if (container && !container.querySelector(".abr-quality-selector")) {
        injectQualitySelector(container, video);
      }
    }

    // Also check if el itself is a video
    if (el.tagName === "VIDEO") {
      var cont = findPlayerContainer(el);
      if (cont && !cont.querySelector(".abr-quality-selector")) {
        injectQualitySelector(cont, el);
      }
    }
  }

  function findPlayerContainer(videoEl) {
    // Walk up to find the relative/absolute positioned container
    var el = videoEl.parentElement;
    var depth = 0;
    while (el && depth < 8) {
      var style = window.getComputedStyle(el);
      if (style.position === "relative" || style.position === "absolute") {
        return el;
      }
      // Also check for data-camera attribute (Frigate's LivePlayer)
      if (el.dataset && el.dataset.camera) {
        return el;
      }
      el = el.parentElement;
      depth++;
    }
    return videoEl.parentElement;
  }

  function createGearIcon() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");

    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z");
    svg.appendChild(path);

    var circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", "12");
    circle.setAttribute("cy", "12");
    circle.setAttribute("r", "3");
    svg.appendChild(circle);

    return svg;
  }

  function injectQualitySelector(container, videoEl) {
    var isLive = isLiveContext(container);

    var wrapper = document.createElement("div");
    wrapper.className = "abr-quality-selector";

    var btn = document.createElement("button");
    btn.className = "abr-quality-btn";
    btn.title = "Video Quality";
    btn.appendChild(createGearIcon());

    var menu = document.createElement("div");
    menu.className = "abr-quality-menu";
    menu.style.display = "none";

    // Build options
    var options = [];
    if (isLive) {
      options.push({ label: "Original", value: "original" });
    } else {
      options.push({ label: "Auto", value: "auto" });
      options.push({ label: "Original", value: "original" });
    }
    for (var i = 0; i < abrConfig.tiers.length; i++) {
      var t = abrConfig.tiers[i];
      options.push({ label: t.name, value: t.name });
    }

    var currentQuality = isLive ? getLiveQuality() : getRecordingQuality();

    for (var k = 0; k < options.length; k++) {
      (function (opt) {
        var item = document.createElement("div");
        item.className = "abr-quality-option" + (opt.value === currentQuality ? " active" : "");
        item.textContent = opt.label;
        item.addEventListener("click", function (e) {
          e.stopPropagation();
          if (isLive) {
            setLiveQuality(opt.value);
            window.dispatchEvent(new CustomEvent("abr-quality-change", {
              detail: { type: "live", quality: opt.value }
            }));
            // Force the live player to reconnect by simulating visibility change
            forcePlayerReconnect();
          } else {
            setRecordingQuality(opt.value);
            window.dispatchEvent(new CustomEvent("abr-quality-change", {
              detail: { type: "recording", quality: opt.value }
            }));
            // For recordings, reload to pick up the new quality via intercepted loadSource
            window.location.reload();
          }
          // Update active state
          var siblings = menu.querySelectorAll(".abr-quality-option");
          for (var s = 0; s < siblings.length; s++) {
            siblings[s].classList.remove("active");
          }
          item.classList.add("active");
          menu.style.display = "none";
        });
        menu.appendChild(item);
      })(options[k]);
    }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      menu.style.display = menu.style.display === "none" ? "block" : "none";
    });

    // Close menu when clicking outside
    document.addEventListener("click", function () {
      menu.style.display = "none";
    });

    wrapper.appendChild(btn);
    wrapper.appendChild(menu);
    container.appendChild(wrapper);
  }

  function isLiveContext(container) {
    // Check if this is a live player (has data-camera or is inside a live view)
    if (container.dataset && container.dataset.camera) return true;
    // Check URL - live views are typically at /cameras/ or root
    var path = window.location.pathname;
    if (path === "/" || path.indexOf("/cameras") === 0) return true;
    // Recording views are at /review or /history
    if (path.indexOf("/review") === 0 || path.indexOf("/history") === 0) return false;
    return false;
  }

  function forcePlayerReconnect() {
    // Dispatch a brief visibility toggle to force WebSocket reconnection.
    // MSEPlayer and WebRTCPlayer disconnect on visibility loss.
    var origHidden = document.hidden;
    Object.defineProperty(document, "hidden", { value: true, writable: true, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    setTimeout(function () {
      Object.defineProperty(document, "hidden", { value: origHidden, writable: true, configurable: true });
      document.dispatchEvent(new Event("visibilitychange"));
    }, 100);
  }

  // --- Boot ---

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
