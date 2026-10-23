#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
"""Element geometry: perturbed, on the layout grid, and never drifting.

`getBoundingClientRect()` returns sub-pixel numbers that depend on the font
stack, the rasteriser and the layout engine, which is why measuring a string of
text is a fingerprint. 0033 perturbs them -- and the danger is entirely in HOW.

The first version added under a thousandth of a pixel to every field, and
reported 73.78158569335938 where layout can only say 73.78125: Blink lays out in
1/64 px units, and a number off that grid is proof of tampering on its own.
demo.fingerprint.com flagged the profile as an anti-detect browser for it
(23.10.2026). The edges now move by whole layout units, and only the edges
text measurement produces.

Claims:

  * the values MOVED, and a different seed moves them differently.
  * every number is still on the layout grid, and the edges the page set
    itself (left: 37px) are exactly where it set them. The grid is 1/64 of a
    DEVICE pixel: with zoom-for-DSF a retina screen lays out at zoom 2, and an
    honest Chrome there reports 259.9921875 -- 1/128 of a CSS px.
  * they do NOT DRIFT: the same element measured a hundred times gives one
    answer, and the same text moved by whole pixels keeps its width.
  * the geometry is INTERNALLY CONSISTENT: right - left is width, two inline
    boxes that touch still share their edge, getClientRects() agrees with
    getBoundingClientRect(), and a Range over the text agrees with the box.

Usage: core/verify/verify-0033.py <core binary>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Claims, launch  # noqa: E402

CORE = sys.argv[1]

# Under 2^31, and that is load-bearing rather than arbitrary: the core reads
# seeds through FuryConfig::GetInt, which is a SIGNED 32-bit int. A larger value
# is not clamped — it is dropped, the seed lookup answers "not configured", and
# the surface goes entirely unnoised while nothing reports a problem. See the
# mask in shared-rs/src/persona.rs::sub_seed, which is what keeps real profiles
# inside the range.
SEED_A = 0x41EC7001
SEED_B = 0x41EC7002

READ = """
(() => {
  // Text, because text is what a geometry fingerprint actually measures: the
  // width of a rendered string is the font stack in one number.
  const host = document.createElement('div');
  host.style.cssText = 'position:absolute;left:37px;top:53px;font:16px serif;white-space:nowrap';
  const a = document.createElement('span');
  a.textContent = 'Fury \\u2014 geometry 0033 \\u2014 mmmmiiiillll';
  const b = document.createElement('span');
  b.textContent = 'xyzzy';
  host.append(a, b);
  document.body.appendChild(host);

  // The same string 100 whole pixels to the right.
  const moved = document.createElement('div');
  moved.style.cssText = 'position:absolute;left:137px;top:153px;font:16px serif;white-space:nowrap';
  moved.textContent = a.textContent;
  document.body.appendChild(moved);

  // More strings, for "a different seed measures differently".
  const widths = ['Hamburgefonstiv', 'The quick brown fox', '0123456789', 'WWWWiiii'].map(t => {
    const e = document.createElement('span');
    e.style.cssText = 'font:15px sans-serif;white-space:nowrap';
    e.textContent = t;
    document.body.appendChild(e);
    const w = e.getBoundingClientRect().width;
    e.remove();
    return w;
  });

  const r = a.getBoundingClientRect();
  const rect = {
    x: r.x, y: r.y, width: r.width, height: r.height,
    top: r.top, right: r.right, bottom: r.bottom, left: r.left,
  };

  // A hundred more reads. If any of them differs, the noise depends on
  // something other than the geometry.
  let drifted = 0;
  for (let i = 0; i < 100; i++) {
    const again = a.getBoundingClientRect();
    if (again.width !== r.width || again.left !== r.left ||
        again.height !== r.height || again.top !== r.top) {
      drifted++;
    }
  }

  const list = a.getClientRects();
  const first = list.length ? {
    width: list[0].width, height: list[0].height,
    left: list[0].left, top: list[0].top,
  } : null;

  const range = document.createRange();
  range.selectNodeContents(a.firstChild);
  const rr = range.getBoundingClientRect();

  const out = {
    rect, drifted, rectCount: list.length, first,
    bLeft: b.getBoundingClientRect().left,
    range: {left: rr.left, right: rr.right, width: rr.width},
    movedWidth: moved.getBoundingClientRect().width,
    widths,
    dpr: devicePixelRatio,
  };
  host.remove();
  moved.remove();
  return JSON.stringify(out);
})()
"""


def on_grid(v, dpr):
    return abs(v * 64 * dpr - round(v * 64 * dpr)) < 1e-9


def main():
    claims = Claims("0033 — client rects", CORE)

    with launch(CORE, {"noise": {"clientRectsSeed": SEED_A}}) as s:
        a = json.loads(s.js(READ))
        r = a["rect"]
        print(f"  seed A: width {r['width']!r} left {r['left']!r}")

        claims.check(a["drifted"] == 0,
                     f"a hundred further reads of the same element return the "
                     f"same numbers — noise that drifts is a stronger signal "
                     f"than no noise ({a['drifted']} differed)")
        dpr = a["dpr"]
        claims.check(all(on_grid(r[k], dpr) for k in r)
                     and on_grid(a["bLeft"], dpr),
                     f"every number is on Blink's layout grid of 1/64 device px "
                     f"(1/{64 * dpr:g} CSS px at devicePixelRatio {dpr:g}), as "
                     f"real Chrome's are — a value off it is tampering in plain "
                     f"sight ({r})")
        claims.check(r["left"] == 37 and r["top"] == 53,
                     f"edges the page set itself are untouched: left {r['left']}, "
                     f"top {r['top']} for left:37px; top:53px")
        claims.check(a["movedWidth"] == r["width"],
                     f"the same text 100 px to the right measures the same width "
                     f"({a['movedWidth']} vs {r['width']})")

        claims.check(abs((r["right"] - r["left"]) - r["width"]) < 1e-9
                     and abs((r["bottom"] - r["top"]) - r["height"]) < 1e-9,
                     f"right - left is width and bottom - top is height "
                     f"({r['right'] - r['left']} vs {r['width']})")
        claims.check(a["bLeft"] == r["right"],
                     f"the next inline box starts exactly where this one ends — "
                     f"a shared edge is moved once, for both "
                     f"({a['bLeft']} vs {r['right']})")
        claims.check(a["rectCount"] == 1 and a["first"] is not None
                     and abs(a["first"]["width"] - r["width"]) < 1e-9
                     and abs(a["first"]["left"] - r["left"]) < 1e-9,
                     f"getClientRects()[0] and getBoundingClientRect() agree "
                     f"(got {a['rectCount']} rects, {a['first']})")
        claims.check(a["range"]["left"] == r["left"]
                     and a["range"]["right"] == r["right"],
                     f"a Range over the text measures what its box measures "
                     f"({a['range']} vs left {r['left']} right {r['right']})")

    with launch(CORE, {"noise": {"clientRectsSeed": SEED_B}}) as s:
        b = json.loads(s.js(READ))
        print(f"  seed B: width {b['rect']['width']!r}, widths {b['widths']}")
        claims.check(b["widths"] != a["widths"] or b["rect"]["width"] != r["width"],
                     f"a different seed measures different text widths — "
                     f"otherwise every profile shares one geometry hash "
                     f"({b['widths']} vs {a['widths']})")

    with launch(CORE, None) as s:
        bare = json.loads(s.js(READ))
        print(f"  unconfigured: width {bare['rect']['width']!r} "
              f"left {bare['rect']['left']!r}")

        moved = abs(r["width"] - bare["rect"]["width"])
        claims.check(0 < moved <= 2 / (64 * dpr),
                     f"the configured build moved the text's edge by one or two "
                     f"layout units, no more ({moved * 64 * dpr:g} units)")
        claims.control(
            bare["drifted"] == 0 and bare["rect"]["width"] != r["width"]
            and all(on_grid(v, dpr) for v in bare["rect"].values()),
            f"an unconfigured build is stable too, measures its own width, and "
            f"is on the same grid the claim above holds the configured one to "
            f"(got {bare['rect']['width']})",
        )

    return claims.done()


if __name__ == "__main__":
    sys.exit(main())
