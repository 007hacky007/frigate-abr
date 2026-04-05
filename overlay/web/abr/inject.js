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
  var ABR_VARIANT_PREFIX = "_abr_";

  var abrConfig = null;
  var abrEnabled = false;

  // Track active live WebSockets so we can close them on quality change
  var activeLiveWebSockets = [];

  // --- Initialization ---

  function init() {
    fetchConfig()
      .then(function (cfg) {
        abrConfig = cfg;
        abrEnabled = cfg && cfg.enabled && cfg.tiers && cfg.tiers.length > 0;
        if (!abrEnabled) return;

        console.log("[ABR] Enabled with tiers:", cfg.tiers.map(function (t) { return t.name; }));

        interceptVodRequests();
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

  // --- Recording ABR: Intercept XHR and fetch for VOD URLs ---

  function interceptVodRequests() {
    // hls.js uses XMLHttpRequest (not fetch) to load playlists and segments.
    // We intercept XHR.open() to rewrite master.m3u8 URLs to the ABR sidecar.
    // Also intercept fetch() as a fallback (some hls.js configs use fetch loader).
    var origXhrOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      try {
        var quality = getRecordingQuality();
        if (quality !== "original" && typeof url === "string" && isVodMasterUrl(url)) {
          var abrUrl = rewriteVodUrl(url);
          console.log("[ABR] Rewriting VOD XHR:", url, "->", abrUrl);
          arguments[1] = abrUrl;
        }
      } catch (e) {
        console.warn("[ABR] XHR intercept error:", e);
      }
      return origXhrOpen.apply(this, arguments);
    };

    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        var url = typeof input === "string" ? input : (input && input.url ? input.url : "");
        var quality = getRecordingQuality();
        if (quality !== "original" && isVodMasterUrl(url)) {
          var abrUrl = rewriteVodUrl(url);
          console.log("[ABR] Rewriting VOD fetch:", url, "->", abrUrl);
          if (typeof input === "string") {
            input = abrUrl;
          } else if (input && input.url) {
            input = new Request(abrUrl, input);
          }
        }
      } catch (e) {
        console.warn("[ABR] fetch intercept error:", e);
      }
      return origFetch.call(window, input, init);
    };

    console.log("[ABR] XHR and fetch intercepted for VOD URL rewriting");
  }

  function isVodMasterUrl(url) {
    // Only rewrite the master.m3u8 request, not segment or index requests.
    // hls.js will follow the variant URLs from the ABR master playlist.
    return url && /\/vod\/[^/]+\/start\/.*master\.m3u8/.test(url) && url.indexOf("/abr/") === -1;
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
      var isLive = false;
      try {
        var quality = getLiveQuality();
        if (quality !== "original" && quality !== "auto" && isLiveWsUrl(url)) {
          var newUrl = rewriteLiveWsUrl(url, quality);
          console.log("[ABR] Rewriting live WS:", url, "->", newUrl);
          url = newUrl;
          isLive = true;
        } else if (isLiveWsUrl(url)) {
          isLive = true;
        }
      } catch (e) {
        console.warn("[ABR] WebSocket intercept error:", e);
      }

      var ws;
      if (protocols !== undefined) {
        ws = new OriginalWebSocket(url, protocols);
      } else {
        ws = new OriginalWebSocket(url);
      }

      // Track live WebSockets for quality-change reconnection
      if (isLive) {
        activeLiveWebSockets.push(ws);
        console.log("[ABR] Tracking live WS (" + activeLiveWebSockets.length + " total):", url);
        ws.addEventListener("close", function (ev) {
          var idx = activeLiveWebSockets.indexOf(ws);
          if (idx !== -1) activeLiveWebSockets.splice(idx, 1);
          console.log("[ABR] Live WS closed (code=" + ev.code + ", reason=" + ev.reason + ", remaining=" + activeLiveWebSockets.length + "):", url);
        });
        ws.addEventListener("error", function () {
          console.log("[ABR] Live WS error:", url);
        });
      }

      return ws;
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

  // Expose debug helper on window for console troubleshooting
  window._abrDebug = function () {
    console.log("[ABR] === Debug Info ===");
    console.log("[ABR] Enabled:", abrEnabled);
    console.log("[ABR] Config:", JSON.stringify(abrConfig));
    console.log("[ABR] Live quality:", getLiveQuality());
    console.log("[ABR] Recording quality:", getRecordingQuality());
    console.log("[ABR] Active live WebSockets:", activeLiveWebSockets.length);
    for (var i = 0; i < activeLiveWebSockets.length; i++) {
      console.log("[ABR]   WS[" + i + "] readyState=" + activeLiveWebSockets[i].readyState + " url=" + activeLiveWebSockets[i].url);
    }
  };

  function closeAllLiveWebSockets() {
    // Close all tracked live WebSockets. The players will auto-reconnect,
    // and the new WebSocket creation goes through our interceptor with the
    // updated quality from localStorage.
    //
    // Note: MSEPlayer has a RECONNECT_TIMEOUT of 10s and goes through a
    // fallback chain (MSE -> WebRTC -> JSMpeg) on errors. Closing with
    // code 1000 (normal) triggers the reconnect. The player may take a few
    // seconds to settle on the new stream. This is inherent to Frigate's
    // player design and cannot be avoided without modifying Frigate source.
    var sockets = activeLiveWebSockets.slice(); // copy to avoid mutation during iteration
    console.log("[ABR] Closing", sockets.length, "live WebSocket(s) for quality switch");
    for (var i = 0; i < sockets.length; i++) {
      try {
        if (sockets[i].readyState === OriginalWebSocket.OPEN ||
            sockets[i].readyState === OriginalWebSocket.CONNECTING) {
          sockets[i].close(1000, "ABR quality change");
        }
      } catch (e) {
        // ignore
      }
    }
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
            console.log("[ABR] Quality switch: " + getLiveQuality() + " -> " + opt.value);
            setLiveQuality(opt.value);
          } else {
            console.log("[ABR] Recording quality switch: " + getRecordingQuality() + " -> " + opt.value);
            setRecordingQuality(opt.value);
          }
          // Reload the page to apply the new quality. The setting is persisted
          // in localStorage so the new page load picks it up immediately.
          // We can't seamlessly switch mid-stream because Frigate's players
          // don't auto-reconnect when sockets are closed externally.
          window.location.reload();
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

  // --- Boot ---

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
