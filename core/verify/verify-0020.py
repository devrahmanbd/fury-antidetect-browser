#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
"""The screen, and the arithmetic between its four numbers.

`screen.width` alone is one lookup. What makes this a script is that the values
have to AGREE with each other and with the window, and a detector checks the
arithmetic rather than the numbers:

  * `availWidth`/`availHeight` cannot exceed the screen. A taskbar or a menu bar
    takes some, so on a real machine `avail` is smaller — and equal on both axes
    is what a naive spoof produces.
  * `outerWidth - innerWidth` is the browser's own chrome. It is a small,
    stable number on a real browser; if the screen is spoofed and the window is
    not, the two stop being consistent with each other.
  * CSS asks the same questions. `matchMedia('(device-width: 1920px)')`,
    `(resolution: 1dppx)` and `(color: 8)` read ScreenInfo directly rather than
    through Screen, and until 27.09.2026 they answered with the HOST's screen
    beside a persona's screen.width — measured on 0.2.2 as JS 1920x1080 at 1x
    against CSS 1470 at 2dppx. fv.pro called that "screen is not real" and
    iphey flagged it twice; neither did once CSS agreed.
  * a Worker has no `screen` at all, so there is nothing to disagree — which is
    why this one is checked in an IFRAME instead, where there is.
  * `outerWidth`/`outerHeight` describe the WINDOW, so every frame in it gives
    the same pair. Until 24.10.2026 an iframe answered with its own box plus
    the chrome (300x289 for a default 300x150 iframe beside 1004x748 in the
    page, measured on the Windows build box), so it is checked in the page, a
    srcdoc iframe and a cross-site iframe in its own process, with the page
    showing a classic vertical scrollbar that innerWidth includes.

Usage: core/verify/verify-0020.py <core binary>
"""

import http.server
import json
import os
import socketserver
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Claims, launch  # noqa: E402

CORE = sys.argv[1]

# A 1920x1080 Windows machine: a taskbar 40 px tall, no side bars. Deliberately
# not this Mac, which is neither that size nor that shape.
SCREEN = {
    "width": 1920,
    "height": 1080,
    "availWidth": 1920,
    "availHeight": 1040,
    "availLeft": 0,
    "availTop": 0,
    "colorDepth": 24,
    "devicePixelRatio": 1,
}

READ = """
(() => {
  const s = screen;
  const out = {
    width: s.width, height: s.height,
    availWidth: s.availWidth, availHeight: s.availHeight,
    availLeft: s.availLeft, availTop: s.availTop,
    colorDepth: s.colorDepth, pixelDepth: s.pixelDepth,
    dpr: devicePixelRatio,
    outerWidth, innerWidth, outerHeight, innerHeight,
  };
  const mm = (q) => matchMedia(q).matches;
  out.css = {
    width: mm(`(device-width: ${s.width}px)`),
    height: mm(`(device-height: ${s.height}px)`),
    aspect: mm(`(device-aspect-ratio: ${s.width}/${s.height})`),
    resolution: mm(`(resolution: ${devicePixelRatio}dppx)`),
    webkitRatio: mm(`(-webkit-device-pixel-ratio: ${devicePixelRatio})`),
    color: mm(`(color: ${s.colorDepth / 3})`),
  };
  const f = document.createElement('iframe');
  f.src = 'about:blank';
  document.body.appendChild(f);
  const i = f.contentWindow.screen;
  out.iframe = {width: i.width, height: i.height, availHeight: i.availHeight};
  f.remove();
  return JSON.stringify(out);
})()
"""


# A Windows window's chrome, deliberately unlike this Mac's (0 and about 87),
# so a frame that reports the host's own window cannot pass for configured.
CHROME = {"chromeWidthDelta": 16, "chromeHeightDelta": 79}

# The page is on 127.0.0.1 and one iframe on localhost: two sites, so with site
# isolation that iframe is a separate renderer with its own widget, the case an
# answer computed from the frame tree in one process would get wrong. The page
# forces a classic 15 px scrollbar: overlay scrollbars on this Mac take no
# width, and innerWidth includes the one a Windows page shows.
MAIN_PAGE = """<!doctype html>
<style>::-webkit-scrollbar{width:15px;height:15px}
body{margin:0;height:4000px}</style>
<script>
const frames = {};
addEventListener('message', (e) => { frames[e.data.when] = e.data; });
window.ask = () => new Promise((resolve) => {
  addEventListener('message', (e) => {
    if (e.data.when === 'asked') resolve(e.data);
  });
  document.getElementById('cross').contentWindow.postMessage('ask', '*');
});
</script>
<iframe id="same" srcdoc="<p>same"></iframe>
<iframe id="cross" src="http://localhost:{port}/child"></iframe>
"""

CHILD_PAGE = """<!doctype html><script>
const size = (when) => ({when, outerWidth, outerHeight, innerWidth, innerHeight});
parent.postMessage(size('loaded'), '*');
addEventListener('message', () => parent.postMessage(size('asked'), '*'));
</script>"""

FRAMES = """
(async () => {
  const same = document.getElementById('same').contentWindow;
  const cross = await window.ask();
  return JSON.stringify({
    main: {outerWidth, outerHeight, innerWidth, innerHeight,
           scrollbar: innerWidth - document.documentElement.clientWidth},
    same: {outerWidth: same.outerWidth, outerHeight: same.outerHeight,
           innerWidth: same.innerWidth, innerHeight: same.innerHeight},
    loaded: frames.loaded || null,
    cross,
  });
})()
"""


def serve():
    """Both pages from one port; the site is the host name the page asks for."""
    class Pages(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (CHILD_PAGE if self.path == "/child"
                    else MAIN_PAGE.replace("{port}", str(self.server.server_address[1])))
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def measure_frames(s):
    got = json.loads(s.js(FRAMES))
    # The cross-site iframe has to really be another renderer, or the claim
    # about it tests the same code path as the srcdoc one.
    targets = s.ws.call("Target.getTargets")["targetInfos"]
    got["oopif"] = any(t["type"] == "iframe" and "localhost" in t["url"]
                       for t in targets)
    return got


def window_pair(frame):
    return (frame["outerWidth"], frame["outerHeight"])


def main():
    claims = Claims("0020 — screen and window", CORE)
    server = serve()
    page = f"http://127.0.0.1:{server.server_address[1]}/"

    with launch(CORE, {"screen": SCREEN}) as s:
        got = json.loads(s.js(READ))
        print(f"  measured: {got}")

        for key in ("width", "height", "availWidth", "availHeight", "colorDepth"):
            claims.check(got[key] == SCREEN[key],
                         f"screen.{key} is {SCREEN[key]} (got {got[key]})")

        claims.check(got["pixelDepth"] == got["colorDepth"],
                     f"pixelDepth matches colorDepth, as it does everywhere real "
                     f"({got['pixelDepth']} vs {got['colorDepth']})")

        # The arithmetic, which is what a detector actually checks.
        claims.check(got["availWidth"] <= got["width"]
                     and got["availHeight"] <= got["height"],
                     "the available area does not exceed the screen")
        claims.check(got["availHeight"] < got["height"],
                     f"and something is taken by a taskbar — equal on both axes is "
                     f"what a naive spoof produces ({got['availHeight']} < {got['height']})")

        # The window has to be consistent with the screen it claims to be on.
        chrome_h = got["outerHeight"] - got["innerHeight"]
        claims.check(0 <= chrome_h <= 200,
                     f"outerHeight - innerHeight is a real browser's chrome, not a "
                     f"leftover from the host ({chrome_h} px)")
        claims.check(got["outerWidth"] <= got["width"],
                     f"the window fits on the screen it claims "
                     f"({got['outerWidth']} <= {got['width']})")

        for feature, ok in got["css"].items():
            claims.check(ok, f"CSS agrees with JS on {feature} — a media query "
                             f"answering with the host's screen contradicts "
                             f"screen.* ({got['css']})")

        claims.check(got["iframe"]["width"] == SCREEN["width"]
                     and got["iframe"]["availHeight"] == SCREEN["availHeight"],
                     f"an iframe measures the same screen: {got['iframe']}")

    # Twice: on the monitor's own screen, which is what a desktop profile gets
    # since 0.4.13, and on a pinned 1x screen, which on this 2x Mac goes through
    # Fury's scale-factor emulation (patch 0131) and halves the viewport the
    # renderer hands to its iframes -- a window computed from that came out
    # 466x386 on 24.10.2026.
    for label, screen in (("real monitor", dict(CHROME)),
                          ("pinned 1x screen", {**SCREEN, **CHROME})):
        with launch(CORE, {"screen": screen}, page=page) as s:
            got = measure_frames(s)
        print(f"  frames, {label}: {got}")
        main_frame = got["main"]

        claims.check(main_frame["scrollbar"] == 15,
                     f"{label}: the page shows a 15 px scrollbar, inside "
                     f"innerWidth ({main_frame['scrollbar']})")
        claims.check(
            (main_frame["outerWidth"] - main_frame["innerWidth"],
             main_frame["outerHeight"] - main_frame["innerHeight"])
            == (CHROME["chromeWidthDelta"], CHROME["chromeHeightDelta"]),
            f"{label}: the page's outer size is its viewport plus the "
            f"configured chrome ({window_pair(main_frame)} vs inner "
            f"{main_frame['innerWidth']}x{main_frame['innerHeight']})")
        claims.check(window_pair(got["same"]) == window_pair(main_frame),
                     f"{label}: a srcdoc iframe reports the page's window, not "
                     f"its own box plus the chrome ({window_pair(got['same'])} "
                     f"vs {window_pair(main_frame)})")
        claims.check(got["oopif"],
                     f"{label}: the localhost iframe runs in its own renderer")
        claims.check(window_pair(got["cross"]) == window_pair(main_frame),
                     f"{label}: so does a cross-site iframe in another process "
                     f"({window_pair(got['cross'])} vs {window_pair(main_frame)})")
        claims.check(got["loaded"] is not None
                     and window_pair(got["loaded"]) == window_pair(main_frame),
                     f"{label}: and already while its page loads "
                     f"({got['loaded']})")

    with launch(CORE, None, page=page) as s:
        bare_frames = measure_frames(s)
        print(f"  unconfigured frames: {bare_frames}")
        bare_main = bare_frames["main"]
        claims.control(
            (bare_main["outerWidth"] - bare_main["innerWidth"],
             bare_main["outerHeight"] - bare_main["innerHeight"])
            != (CHROME["chromeWidthDelta"], CHROME["chromeHeightDelta"]),
            f"an unconfigured build reports the host's own chrome "
            f"(got {window_pair(bare_main)})")

    with launch(CORE, None) as s:
        bare = json.loads(s.js(
            "JSON.stringify({w: screen.width, h: screen.height, "
            "css1920: matchMedia('(device-width: 1920px)').matches})"))
        print(f"  unconfigured: {bare}")
        claims.control(
            (bare["w"], bare["h"]) != (SCREEN["width"], SCREEN["height"])
            and not bare["css1920"],
            f"an unconfigured build reports the host's own screen (got {bare})",
        )

    server.shutdown()
    return claims.done()


if __name__ == "__main__":
    sys.exit(main())
