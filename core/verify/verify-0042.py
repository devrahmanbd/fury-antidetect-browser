#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
"""The nineteen "Google ..." voices, which only Google Chrome has.

Chrome's network text-to-speech is a component extension (Google Network
Speech, chrome/browser/resources/network_speech_synthesis) that Chromium loads
only under GOOGLE_CHROME_BRANDING. Its voices -- "Google US English", "Google
Deutsch" and seventeen more, every one remote -- are in getVoices() of every
Google Chrome on Windows and macOS and of no Chromium. demo.fingerprint.com
named the core "Chromium-Based Browser" with no version until they were there,
and "Chrome 155" with them (23.10.2026).

Claims:

  * the nineteen voices are listed, as remote voices (localService false), in
    the order Chrome lists them: after the platform's own.
  * speaking with one ENDS. Without Chrome's API key the request to Google is
    refused, and what the page must see is an utterance that finishes with an
    error -- as on a real Chrome that cannot reach Google -- not one that hangs.

The patch is not config-driven, so the control is the same core with component
extensions that have a background page switched off: the voices must go with
the extension, or they did not come from it.

Usage: core/verify/verify-0042.py <core binary>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Claims, launch  # noqa: E402

CORE = sys.argv[1]

GOOGLE = [
    "Google Deutsch", "Google US English", "Google UK English Female",
    "Google UK English Male", "Google español",
    "Google español de Estados Unidos", "Google français", "Google हिन्दी",
    "Google Bahasa Indonesia", "Google italiano", "Google 日本語", "Google 한국의",
    "Google Nederlands", "Google polski", "Google português do Brasil",
    "Google русский", "Google 普通话（中国大陆）", "Google 粤語（香港）",
    "Google 國語（臺灣）",
]

READ = """
new Promise(resolve => {
  const done = () => resolve(JSON.stringify(speechSynthesis.getVoices().map(
      v => ({name: v.name, local: v.localService}))));
  // The extension's voices arrive after the platform's, each with its own
  // voiceschanged; give the list time to settle rather than taking the first.
  speechSynthesis.getVoices();
  setTimeout(done, 3000);
})
"""

SPEAK = """
new Promise(resolve => {
  const v = speechSynthesis.getVoices().find(v => v.name === 'Google US English');
  if (!v) return resolve('no voice');
  const u = new SpeechSynthesisUtterance('fury');
  u.voice = v;
  u.onend = () => resolve('end');
  u.onerror = e => resolve('error:' + e.error);
  speechSynthesis.speak(u);
  setTimeout(() => resolve('hung'), 15000);
})
"""


def main():
    claims = Claims("0042 — Google network voices", CORE)

    with launch(CORE, {}) as s:
        voices = json.loads(s.js(READ))
        names = [v["name"] for v in voices]
        google = [v for v in voices if v["name"].startswith("Google")]
        print(f"  {len(voices)} voices, {len(google)} of them Google's")

        claims.check(sorted(v["name"] for v in google) == sorted(GOOGLE),
                     f"all nineteen Google voices are listed, and nothing else "
                     f"named Google ({len(google)})")
        claims.check(all(not v["local"] for v in google),
                     "every one of them is remote, as in Chrome: localService "
                     "is false")
        first_google = min(names.index(n) for n in GOOGLE if n in names)
        claims.check(all(not n.startswith("Google")
                         for n in names[:first_google]) and first_google > 0,
                     f"they come after the platform's own voices, where Chrome "
                     f"puts them (first at {first_google} of {len(names)})")

        spoken = s.js(SPEAK, timeout=30000)
        print(f"  speak with Google US English: {spoken}")
        claims.check(spoken in ("end",) or str(spoken).startswith("error:"),
                     f"speaking with one finishes, with an end or an error event, "
                     f"instead of hanging the page's speech queue (got {spoken})")

    with launch(CORE, {},
                extra_args=["--disable-component-extensions-with-background-pages"]) as s:
        bare = json.loads(s.js(READ))
        bare_google = [v for v in bare if v["name"].startswith("Google")]
        claims.control(
            not bare_google and len(bare) > 0,
            f"with component extensions switched off the platform's voices "
            f"remain and Google's are gone -- they come from the extension "
            f"0042 loads ({len(bare)} voices, {len(bare_google)} Google)",
        )

    return claims.done()


if __name__ == "__main__":
    sys.exit(main())
