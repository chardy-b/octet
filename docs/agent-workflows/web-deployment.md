# Octet web deployment workflow

Use this repository-local guide for the Tailscale music web app, now-playing bridge, browser tests and deployments. For Bluetooth/ALSA/PipeWire/CamillaDSP work, use [audio deployment gates](audio-deployment.md) as well.

## Reconcile before editing

Inspect the current worktree/branch, `origin/main`, and the running `/opt/octet-remote` files before writing. A feature worktree can lack modules already live on the Pi and can contain unrelated dirty edits. Use a separate worktree when necessary; compare complete target files with the live copies, including UI chrome/footer links. Preserve live-only changes and unrelated files such as `error.log`. Do not copy the checkout wholesale over live.

The web UI is at `http://100.100.177.81:8767/` over Tailscale; `octet-remote.service` owns it. The separate now-playing publisher listens on `octet.local:8768` only on wlan0, not directly to Tailscale browser clients. `pi/remote/now_playing_bridge.py` provides bounded same-host `/api/now-playing` and hash-validated artwork; stopped/stale metadata must clear. `/api/status` also carries Tidal title/artist/album/cover. Verify both routes after any related deployment.

## Test the deployable set

Inspect the installer's entire copy list and the actual bytes of media assets. Git LFS pointer text is not a video; a broad install can overwrite working live assets with pointers. Stage only the reviewed files or fetch and verify real objects. Trace every installed Python script's transitive local imports and test imports from a scratch directory containing **only** the files the installer copies; tests in the source tree can hide a missing deployed helper. Keep that test separate from preflight against the current live tree, which may legitimately lack the new helper before upgrade.

For browser checks, a local checkout may lack Playwright: install it into a scratch npm prefix and set `NODE_PATH` to that prefix's `node_modules`. Use `/home/hermes/.local/bin/google-chrome` through `OCTET_BROWSER_EXECUTABLE` for `test_sources_browser.cjs`. Point `OCTET_WEB_ROOT` at a live-file snapshot rather than exercising live audio controls in a browser test. The Pi lacks `node`; run JS syntax/browser checks on the development host and Python `py_compile` on the Pi. A full Python suite may have a pre-existing tiny-fixture `test_visualizer` range-expectation failure; report it separately from focused test results rather than claiming every test passed.

## Publish and verify

Pin the exact reviewed commit and CI result. Compare every remote target hash to its captured live snapshot, back up affected files/overrides, install only the narrow manifest atomically with modes preserved, and restart only affected services under approval. A restart can mute audio; never auto-unmute. Read back target hashes, `/api/status`, `/api/now-playing`, and unit health. If another input or DSP ownership is involved, follow [audio deployment gates](audio-deployment.md) and separate installed code, disabled policy/unit, receiver registration, and physically accepted playback.
