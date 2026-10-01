// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright 2026 Bogdan Shapovalov and the Fury authors

//! Talking to a team server about bundles and locks.
//!
//! # Where the credentials come from
//!
//! The agent has no session of its own. The shell holds the token — in Rust and
//! in the OS keychain, never in the webview — and passes it down with the
//! launch that needs it. That keeps one copy of the credential instead of two
//! stores to keep in step, and it means an agent left running after the shell
//! quits cannot start talking to a server on its own behalf.
//!
//! # Why the heartbeat is here and not in the shell
//!
//! The lock says "this profile is open on my machine", and what makes that true
//! is the browser process, which the agent owns. A shell that quits while a
//! profile is still open would stop renewing a lock that is still deserved, and
//! a colleague would take over a profile whose browser is running — the exact
//! data loss the lock exists to prevent.

use std::time::Duration;

use anyhow::Context as _;

/// What to say when moving a whole profile to or from the server breaks off.
///
/// reqwest's own words, "error sending request for url (.../bundle)", were all
/// a tester's log held when every upload failed, and they point nowhere. The
/// case that produced them, 27.09.2026: the team server behind Cloudflare's
/// proxy, where small requests pass and a transfer of a whole profile does not
/// (100 MB and 100 s on the free plan). docs/14 has the fix.
const TRANSFER_HINT: &str = "moving this profile's data to or from the team server broke off. \
    Small requests reaching the server and this failing usually means something in between \
    limits large transfers -- a proxy such as Cloudflare's (set the record to DNS only), \
    an antivirus, or a very slow link. See docs/14-team-server.md";

/// A refusal that came from Cloudflare rather than from the Fury server says so.
fn refused_by(res: &reqwest::Response) -> &'static str {
    let cf = res
        .headers()
        .get(reqwest::header::SERVER)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("cloudflare"));
    if cf {
        " -- answered by Cloudflare's proxy, not by the Fury server; set the record to DNS only (docs/14)"
    } else {
        ""
    }
}

/// What asking for a profile's bundle came back with.
pub enum Fetched {
    /// The profile has never been uploaded -- shared before anyone opened it.
    Nothing,
    /// The server still holds the version this machine already has.
    Unchanged(i32),
    Bundle {
        bytes: Vec<u8>,
        wrapped: String,
        version: i32,
    },
}

/// The validator a bundle version travels under. Written the same way by the
/// server (api.rs, download_bundle); nothing checks the two against each
/// other, and a drift costs only the saving, never correctness: a mismatch is
/// a full download.
pub fn bundle_etag(version: i32) -> String {
    format!("\"v{version}\"")
}

/// A server the agent may talk to for the duration of one launch.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct Server {
    pub url: String,
    pub token: String,
}

impl Server {
    fn client() -> anyhow::Result<reqwest::Client> {
        Ok(reqwest::Client::builder()
            // Generous: a bundle is tens of megabytes and a loaded box is slow,
            // but not unbounded — a wedged server must surface, not hang a
            // launch forever.
            .timeout(Duration::from_secs(300))
            // But a server that does not answer the connection at all is not a
            // slow server, and five minutes of silence in front of a launch
            // reads as a hang. Fail that in fifteen, saying why.
            .connect_timeout(Duration::from_secs(15))
            .build()?)
    }

    /// Fetch the current bundle, unless this machine already holds it.
    ///
    /// `have` is the version the profile directory here was last pulled at or
    /// pushed as. Sent as `If-None-Match`, and a server that still holds that
    /// version answers 304 with no body. A team profile used to come down
    /// whole on every launch, on the machine that had uploaded it a minute
    /// before -- a tester's profile is tens of megabytes, both ways, every time
    /// (01.10.2026). A server older than this ignores the header and sends the
    /// bundle, which the caller then recognises by its version.
    pub async fn fetch_bundle(&self, profile_id: &str, have: Option<i32>) -> anyhow::Result<Fetched> {
        let mut req = Self::client()?
            .get(format!("{}/v1/profiles/{profile_id}/bundle", self.url))
            .bearer_auth(&self.token);
        if let Some(v) = have {
            req = req.header(reqwest::header::IF_NONE_MATCH, bundle_etag(v));
        }
        let res = req.send().await.context(TRANSFER_HINT)?;

        if res.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(Fetched::Nothing);
        }
        if res.status() == reqwest::StatusCode::NOT_MODIFIED {
            if let Some(v) = have {
                return Ok(Fetched::Unchanged(v));
            }
        }
        if !res.status().is_success() {
            anyhow::bail!("the server refused the bundle ({}){}", res.status(), refused_by(&res));
        }

        let wrapped = header(&res, "x-fury-wrapped-key")
            .ok_or_else(|| anyhow::anyhow!("the server sent a bundle with no key"))?;
        let version: i32 = header(&res, "x-fury-version")
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);
        let bytes = res.bytes().await.context(TRANSFER_HINT)?.to_vec();
        Ok(Fetched::Bundle { bytes, wrapped, version })
    }

    /// Push a new version, refusing to clobber someone else's.
    ///
    /// The lock token goes with it. The session says who this is; the lock says
    /// this machine is the one running the browser, and only that machine may
    /// write what the browser produced.
    pub async fn push_bundle(
        &self,
        profile_id: &str,
        bytes: &[u8],
        wrapped_key: &str,
        sha256: &str,
        base_version: i32,
        lock_token: &str,
    ) -> anyhow::Result<i32> {
        let res = Self::client()?
            .post(format!("{}/v1/profiles/{profile_id}/bundle", self.url))
            .bearer_auth(&self.token)
            .header("x-fury-sha256", sha256)
            .header("x-fury-wrapped-key", wrapped_key)
            .header("x-fury-base-version", base_version.to_string())
            .header("x-fury-lock-token", lock_token)
            .body(bytes.to_vec())
            .send()
            .await
            .context(TRANSFER_HINT)?;

        if res.status() == reqwest::StatusCode::CONFLICT {
            // Said in full rather than as "conflict". The operator's next
            // question is always what happened to their work, and the answer is
            // that it is still on this machine.
            let body: serde_json::Value = res.json().await.unwrap_or_default();
            anyhow::bail!(
                "{} — your work is still here, on this machine, and nothing was overwritten",
                body.get("message").and_then(|v| v.as_str()).unwrap_or("someone uploaded first")
            );
        }
        if !res.status().is_success() {
            anyhow::bail!("the upload was refused ({}){}", res.status(), refused_by(&res));
        }
        let body: serde_json::Value = res.json().await?;
        Ok(body.get("version").and_then(|v| v.as_i64()).unwrap_or(0) as i32)
    }

    /// Give the lock back, now rather than in ninety seconds.
    ///
    /// Stopping the heartbeat is enough for correctness — the lock lapses on
    /// its own — and it is not enough for the person looking at the screen.
    /// Closing the browser from its own window left the row saying "in use
    /// here" and a colleague's saying "In use — MacBook Air" for up to a minute
    /// and a half after the window was gone, which reads as the application
    /// having lost track rather than as a lock timing out. Reported as exactly
    /// that: "I closed the browser and it still hangs there as if it were
    /// open."
    ///
    /// The shell's own Close button has always released it here; only the
    /// close-by-hand path did not, and the whole argument for the reaper is
    /// that those two paths should be one.
    pub async fn release_lock(&self, profile_id: &str, lock_token: &str) -> anyhow::Result<()> {
        let res = Self::client()?
            .post(format!("{}/v1/profiles/{profile_id}/unlock", self.url))
            .bearer_auth(&self.token)
            .json(&serde_json::json!({ "lock_token": lock_token }))
            .send()
            .await?;
        if !res.status().is_success() {
            anyhow::bail!("the server refused to release the lock ({})", res.status());
        }
        Ok(())
    }

    async fn beat(&self, profile_id: &str, lock_token: &str) -> anyhow::Result<Beat> {
        let res = Self::client()?
            .post(format!("{}/v1/profiles/{profile_id}/lock/heartbeat", self.url))
            .bearer_auth(&self.token)
            .json(&serde_json::json!({ "lock_token": lock_token }))
            .send()
            .await?;
        if res.status() == reqwest::StatusCode::CONFLICT {
            return Ok(Beat::Lost);
        }
        if !res.status().is_success() {
            anyhow::bail!("heartbeat refused ({})", res.status());
        }
        Ok(Beat::Held)
    }
}

/// The lock lives 90 seconds; renewed every 30.
///
/// Three chances before it lapses. One would mean a single dropped packet hands
/// the profile to whoever asks next while its browser is still running, and a
/// laptop that sleeps for a moment is not a laptop that abandoned the profile.
const BEAT_EVERY: Duration = Duration::from_secs(30);

/// Keep a lock alive until the returned handle is aborted.
/// What the server said to one renewal.
enum Beat {
    Held,
    /// 409: the lock is not ours any more -- it lapsed, or someone took the
    /// profile over. No later renewal can bring it back.
    Lost,
}

/// Renew a profile's lock until the handle is aborted.
///
/// Started when the launch starts, not when the browser does. A lock is taken
/// by the shell for ninety seconds before it calls the agent, and a team launch
/// can outlast that: a tester's launch spent fifteen seconds on an exit that did
/// not answer and two minutes pulling a 6 MB bundle through Cloudflare,
/// 27.09.2026. The renewals began after all of that, found the lock already
/// lapsed, and the close then could not upload -- "this profile is not locked
/// by you" -- so the session, saved passwords included, never reached the
/// server.
pub fn keep_alive(
    server: Server,
    profile_id: String,
    lock_token: String,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(BEAT_EVERY).await;
            match server.beat(&profile_id, &lock_token).await {
                Ok(Beat::Held) => {}
                Ok(Beat::Lost) => {
                    // Said once, then stopped. It used to be retried every
                    // thirty seconds for as long as the browser ran: 1,098
                    // identical lines in one afternoon's log, each true and
                    // none of them useful after the first.
                    tracing::warn!(
                        profile = %profile_id,
                        "this machine no longer holds the profile's lock (it lapsed, or it \
                         was taken over). The browser keeps running; closing it will keep \
                         the session here rather than upload it"
                    );
                    return;
                }
                Err(e) => {
                    // Logged and retried rather than fatal: a request that
                    // failed is not a lock that is gone, and killing a running
                    // browser because one request failed is not an answer.
                    tracing::warn!(profile = %profile_id, error = %e, "heartbeat failed");
                }
            }
        }
    })
}

fn header(res: &reqwest::Response, name: &str) -> Option<String> {
    res.headers().get(name).and_then(|v| v.to_str().ok()).map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_heartbeat_leaves_room_for_failures() {
        // 90-second lock, renewed every 30: three attempts before it lapses.
        // At 45 a single dropped request would already be half the budget.
        assert!(BEAT_EVERY.as_secs() * 3 <= 90);
    }

    #[test]
    fn a_server_needs_both_halves() {
        let parsed: Result<Server, _> =
            serde_json::from_value(serde_json::json!({ "url": "https://x" }));
        // A token-less server would send anonymous requests and be reported to
        // the operator as an expired session.
        assert!(parsed.is_err());
    }
}
