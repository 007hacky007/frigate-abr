/**
 * Tests for inject.js URL rewriting and interception logic.
 *
 * Run with: node tests/test_inject.mjs
 *
 * These tests exercise the pure functions extracted from inject.js without
 * needing a browser environment.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

// --- Extracted logic from inject.js ---

const ABR_VARIANT_PREFIX = "_abr_";

function isVodUrl(url) {
  return (
    url &&
    /\/vod\/[^/]+\/start\//.test(url) &&
    url.indexOf("/vod_abr/") === -1
  );
}

function rewriteVodUrl(url, quality) {
  var newUrl = url.replace(/\/vod\//, "/vod_abr/");
  if (newUrl.indexOf("?") === -1) {
    newUrl += "?quality=" + quality;
  } else {
    newUrl += "&quality=" + quality;
  }
  return newUrl;
}

function isLiveWsUrl(url) {
  return (
    url &&
    (/\/live\/mse\/api\/ws\?/.test(url) ||
      /\/live\/webrtc\/api\/ws\?/.test(url))
  );
}

function rewriteLiveWsUrl(url, quality) {
  var tierSuffix = ABR_VARIANT_PREFIX + quality;
  return url.replace(/([?&]src=)([^&]+)/, function (match, prefix, camera) {
    var abrIdx = camera.indexOf(ABR_VARIANT_PREFIX);
    if (abrIdx !== -1) {
      camera = camera.substring(0, abrIdx);
    }
    return prefix + camera + tierSuffix;
  });
}

// --- VOD URL Detection Tests ---

describe("isVodUrl", () => {
  it("matches master.m3u8 URLs", () => {
    assert.equal(
      isVodUrl("/vod/vchod/start/123/end/456/master.m3u8"),
      true
    );
  });

  it("matches index.m3u8 URLs", () => {
    assert.equal(
      isVodUrl("/vod/vchod/start/123/end/456/index-v1-a1.m3u8"),
      true
    );
  });

  it("matches segment URLs", () => {
    assert.equal(
      isVodUrl("/vod/vchod/start/123/end/456/seg-1-v1-a1.m4s"),
      true
    );
  });

  it("matches init segment URLs", () => {
    assert.equal(
      isVodUrl("/vod/vchod/start/123/end/456/init-v1-a1.mp4"),
      true
    );
  });

  it("matches VOD URLs with full host", () => {
    assert.equal(
      isVodUrl(
        "http://192.168.1.1:5000/vod/camera1/start/100/end/200/master.m3u8"
      ),
      true
    );
  });

  it("rejects already-rewritten ABR URLs", () => {
    assert.equal(
      isVodUrl("/vod_abr/vchod/start/123/end/456/master.m3u8"),
      false
    );
  });

  it("rejects non-VOD URLs", () => {
    assert.equal(isVodUrl("/api/config"), false);
    assert.equal(isVodUrl("/abr/config"), false);
    assert.equal(isVodUrl("/live/mse/api/ws?src=cam"), false);
  });

  it("rejects empty and null", () => {
    assert.ok(!isVodUrl(""));
    assert.ok(!isVodUrl(null));
    assert.ok(!isVodUrl(undefined));
  });
});

// --- VOD URL Rewriting Tests ---

describe("rewriteVodUrl", () => {
  it("rewrites /vod/ to /vod_abr/ with quality parameter", () => {
    assert.equal(
      rewriteVodUrl("/vod/cam/start/1/end/2/master.m3u8", "720p"),
      "/vod_abr/cam/start/1/end/2/master.m3u8?quality=720p"
    );
  });

  it("rewrites segment URLs too", () => {
    assert.equal(
      rewriteVodUrl("/vod/cam/start/1/end/2/seg-5-v1-a1.m4s", "480p"),
      "/vod_abr/cam/start/1/end/2/seg-5-v1-a1.m4s?quality=480p"
    );
  });

  it("rewrites full URLs", () => {
    assert.equal(
      rewriteVodUrl("http://host:5000/vod/cam/start/1/end/2/master.m3u8", "1080p"),
      "http://host:5000/vod_abr/cam/start/1/end/2/master.m3u8?quality=1080p"
    );
  });

  it("only rewrites first /vod/ occurrence", () => {
    assert.equal(
      rewriteVodUrl("/vod/vod_camera/start/1/end/2/master.m3u8", "720p"),
      "/vod_abr/vod_camera/start/1/end/2/master.m3u8?quality=720p"
    );
  });

  it("appends quality with & if URL already has query params", () => {
    assert.equal(
      rewriteVodUrl("/vod/cam/start/1/end/2/master.m3u8?token=abc", "720p"),
      "/vod_abr/cam/start/1/end/2/master.m3u8?token=abc&quality=720p"
    );
  });
});

// --- Full VOD Flow Simulation ---

describe("VOD ABR flow (nginx-vod-module integration)", () => {
  it("rewrites all requests in an HLS session", () => {
    // Simulate the full sequence of HLS requests
    var quality = "720p";

    // 1. master.m3u8
    var master = "/vod/cam/start/100/end/200/master.m3u8";
    assert.ok(isVodUrl(master));
    var rewritten = rewriteVodUrl(master, quality);
    assert.equal(rewritten, "/vod_abr/cam/start/100/end/200/master.m3u8?quality=720p");

    // 2. After nginx-vod-module processes master, hls.js requests index
    //    These come back as /vod_abr/ URLs from the playlist
    var index = "/vod_abr/cam/start/100/end/200/index-v1-a1.m3u8";
    assert.ok(!isVodUrl(index), "already-rewritten index should NOT be rewritten again");

    // 3. Segments also come back as /vod_abr/ from the index playlist
    var seg = "/vod_abr/cam/start/100/end/200/seg-1-v1-a1.m4s";
    assert.ok(!isVodUrl(seg), "already-rewritten segment should NOT be rewritten again");
  });

  it("does not rewrite when quality is original", () => {
    // When quality is "original", isVodUrl still returns true but
    // the caller checks quality before calling rewriteVodUrl.
    // This test documents the expected caller behavior.
    var quality = "original";
    var url = "/vod/cam/start/100/end/200/master.m3u8";
    assert.ok(isVodUrl(url));
    // Caller should NOT call rewriteVodUrl when quality is "original"
  });
});

// --- Live WebSocket URL Detection Tests ---

describe("isLiveWsUrl", () => {
  it("matches MSE WebSocket URLs", () => {
    assert.equal(
      isLiveWsUrl("ws://host:5000/live/mse/api/ws?src=cam"),
      true
    );
  });

  it("matches WebRTC WebSocket URLs", () => {
    assert.equal(
      isLiveWsUrl("ws://host:5000/live/webrtc/api/ws?src=cam"),
      true
    );
  });

  it("rejects JSMpeg URLs (not rewritable)", () => {
    assert.equal(isLiveWsUrl("ws://host:5000/live/jsmpeg/cam"), false);
  });

  it("rejects non-live WebSocket URLs", () => {
    assert.equal(isLiveWsUrl("ws://host:5000/ws"), false);
  });

  it("rejects non-WebSocket URLs", () => {
    assert.equal(isLiveWsUrl("http://host:5000/api/config"), false);
  });

  it("rejects empty and null", () => {
    assert.ok(!isLiveWsUrl(""));
    assert.ok(!isLiveWsUrl(null));
  });
});

// --- Live WebSocket URL Rewriting Tests ---

describe("rewriteLiveWsUrl", () => {
  it("appends ABR variant suffix to camera name", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=front_door",
        "720p"
      ),
      "ws://host/live/mse/api/ws?src=front_door_abr_720p"
    );
  });

  it("replaces existing ABR suffix", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=cam_abr_1080p",
        "480p"
      ),
      "ws://host/live/mse/api/ws?src=cam_abr_480p"
    );
  });

  it("works with WebRTC URLs", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/webrtc/api/ws?src=cam",
        "480p"
      ),
      "ws://host/live/webrtc/api/ws?src=cam_abr_480p"
    );
  });

  it("handles camera names with underscores", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=back_yard_camera",
        "720p"
      ),
      "ws://host/live/mse/api/ws?src=back_yard_camera_abr_720p"
    );
  });

  it("handles camera names that look like tier names", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=driveway_1080p",
        "720p"
      ),
      "ws://host/live/mse/api/ws?src=driveway_1080p_abr_720p"
    );
  });

  it("preserves other query parameters", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=cam&video=all&audio=all",
        "720p"
      ),
      "ws://host/live/mse/api/ws?src=cam_abr_720p&video=all&audio=all"
    );
  });
});
