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

function isVodMasterUrl(url) {
  return (
    url &&
    /\/vod\/[^/]+\/start\/.*master\.m3u8/.test(url) &&
    url.indexOf("/abr/") === -1
  );
}

function rewriteVodUrl(url) {
  return url.replace(/\/vod\//, "/abr/vod/");
}

function isLiveWsUrl(url) {
  return (
    url &&
    (/\/live\/mse\/api\/ws\?/.test(url) ||
      /\/live\/webrtc\/api\/ws\?/.test(url))
  );
}

function rewriteLiveWsUrl(url, quality, tierNames) {
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

describe("isVodMasterUrl", () => {
  it("matches standard VOD master.m3u8 URLs", () => {
    assert.equal(
      isVodMasterUrl("/vod/vchod/start/123/end/456/master.m3u8"),
      true
    );
  });

  it("matches VOD URLs with full host", () => {
    assert.equal(
      isVodMasterUrl(
        "http://192.168.1.1:5000/vod/camera1/start/100/end/200/master.m3u8"
      ),
      true
    );
  });

  it("rejects already-rewritten ABR URLs", () => {
    assert.equal(
      isVodMasterUrl("/abr/vod/vchod/start/123/end/456/master.m3u8"),
      false
    );
  });

  it("rejects index.m3u8 (not master)", () => {
    assert.equal(
      isVodMasterUrl("/vod/vchod/start/123/end/456/index.m3u8"),
      false
    );
  });

  it("rejects segment requests", () => {
    assert.equal(
      isVodMasterUrl("/vod/vchod/start/123/end/456/seg-1-v1-a1.m4s"),
      false
    );
  });

  it("rejects non-VOD URLs", () => {
    assert.equal(isVodMasterUrl("/api/config"), false);
    assert.equal(isVodMasterUrl("/abr/config"), false);
    assert.equal(isVodMasterUrl("/live/mse/api/ws?src=cam"), false);
  });

  it("rejects empty and null", () => {
    assert.ok(!isVodMasterUrl(""));
    assert.ok(!isVodMasterUrl(null));
    assert.ok(!isVodMasterUrl(undefined));
  });
});

// --- VOD URL Rewriting Tests ---

describe("rewriteVodUrl", () => {
  it("rewrites /vod/ to /abr/vod/", () => {
    assert.equal(
      rewriteVodUrl("/vod/cam/start/1/end/2/master.m3u8"),
      "/abr/vod/cam/start/1/end/2/master.m3u8"
    );
  });

  it("rewrites full URLs", () => {
    assert.equal(
      rewriteVodUrl("http://host:5000/vod/cam/start/1/end/2/master.m3u8"),
      "http://host:5000/abr/vod/cam/start/1/end/2/master.m3u8"
    );
  });

  it("only rewrites first /vod/ occurrence", () => {
    assert.equal(
      rewriteVodUrl("/vod/vod_camera/start/1/end/2/master.m3u8"),
      "/abr/vod/vod_camera/start/1/end/2/master.m3u8"
    );
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
        "720p",
        ["1080p", "720p", "480p"]
      ),
      "ws://host/live/mse/api/ws?src=front_door_abr_720p"
    );
  });

  it("replaces existing ABR suffix", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=cam_abr_1080p",
        "480p",
        ["1080p", "720p", "480p"]
      ),
      "ws://host/live/mse/api/ws?src=cam_abr_480p"
    );
  });

  it("works with WebRTC URLs", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/webrtc/api/ws?src=cam",
        "480p",
        ["1080p", "720p", "480p"]
      ),
      "ws://host/live/webrtc/api/ws?src=cam_abr_480p"
    );
  });

  it("handles camera names with underscores", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=back_yard_camera",
        "720p",
        ["1080p", "720p", "480p"]
      ),
      "ws://host/live/mse/api/ws?src=back_yard_camera_abr_720p"
    );
  });

  it("handles camera names that look like tier names", () => {
    // Camera named "driveway_1080p" should NOT be confused with a variant
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=driveway_1080p",
        "720p",
        ["1080p", "720p", "480p"]
      ),
      "ws://host/live/mse/api/ws?src=driveway_1080p_abr_720p"
    );
  });

  it("preserves other query parameters", () => {
    assert.equal(
      rewriteLiveWsUrl(
        "ws://host/live/mse/api/ws?src=cam&video=all&audio=all",
        "720p",
        []
      ),
      "ws://host/live/mse/api/ws?src=cam_abr_720p&video=all&audio=all"
    );
  });
});
