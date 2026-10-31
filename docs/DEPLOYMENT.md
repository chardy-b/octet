# Deploying Octet to the Raspberry Pi

Two jobs, two sections:

- **[Update a running Pi](#update-a-running-pi)**: one command from your PC. This is what you do almost every time.
- **[Set up a new Pi from scratch](#set-up-a-new-pi-from-scratch)**: the full ordered procedure.

Then [verify AirPlay](#verify-airplay-quietly) and, if it breaks, [troubleshoot it](#troubleshooting-airplay).

## How the pieces fit

```
Tidal / files -> MPD ------------> Loopback sub 0 \
AirPlay  -> Shairport-Sync ------> Loopback sub 1  >-> CamillaDSP -> HiFiBerry DAC8x (8 outputs)
Bluetooth -> BlueALSA (optional) -> Loopback sub 2 /
                       ^ octet-remote (web UI + AirPlay/Bluetooth controllers) mutes, routes and unmutes
```

| What | Where on the Pi | Source in this repo |
|---|---|---|
| Web remote, AirPlay and Bluetooth controllers | `/opt/octet-remote/` | `pi/remote/`, listed in `pi/remote/remote_manifest.py` |
| Which commit is running | `/opt/octet-remote/REVISION` | written by `deploy.py` |
| Shairport config and service override | `/etc/shairport-sync.conf`, `shairport-sync.service.d/octet.conf` | `pi/etc/shairport-sync.conf`, written by `install_airplay.py` |
| octet-remote drop-ins | `/etc/systemd/system/octet-remote.service.d/` (`airplay.conf`, `bluetooth.conf`, `lan.conf`) | written by the installers |
| CamillaDSP, its GUI, MPD, upmpdcli | `/opt/camilladsp`, `/etc/mpd.conf`, ... | `pi/camilladsp/`, `pi/etc/`, `pi/systemd/` |
| Auto Tune | `/opt/octet-measure`, `/var/lib/octet-autotune` | `measure/`, `pi/deploy_autotune.py` |
| Now-playing publisher (Wi-Fi, for the Magic Mirror) | `/opt/octet-now-playing` | `pi/now-playing/` |

CamillaDSP's WebSocket stays on `127.0.0.1:1234`; there is no socket proxy.

## Update a running Pi

From a checkout of `main` on your PC, with `ssh octet@octet` working (Tailscale SSH is fine):

```bash
python pi/remote/deploy.py --dry-run    # read-only checks; changes nothing
python pi/remote/deploy.py              # deploy the commit you have checked out
python pi/remote/deploy.py --rollback   # put back the previous files
```

Commit first. The script refuses to ship uncommitted files in `pi/` (override with `--allow-dirty`), so the Pi can always be traced to a commit.

What it does, in order:

1. Builds a package of exactly the files in `remote_manifest.py` plus a `REVISION` file naming the commit.
2. **Pre-flight (read-only), on the Pi:** `octet-remote`, `camilladsp` and `mpd` are active; the web remote answers; no AirPlay cast is active; nothing is playing; every file compiles; and every module `server.py` imports is in the manifest. If any check fails it stops and tells you why.
3. Backs up the current files and drop-ins to `/var/backups/octet-remote-deploy/<timestamp>/`.
4. Repairs drop-in drift: any `RuntimeDirectoryMode=0700` becomes `0750` (see [Troubleshooting](#troubleshooting-airplay)).
5. Installs files atomically, then restarts `octet-remote` only (Shairport follows it). The restart ends **muted**.
6. **Verifies:** web remote ready and muted; AirPlay controller reports no error; Shairport active; `/run/octet-airplay` is `0750`; the Shairport user can reach the control socket; `REVISION` matches.
7. If verification fails it rolls back by itself and reports why.

It never plays or unmutes audio, and never touches MPD or its queue, CamillaDSP, speaker profiles, volume, Auto Tune, the Shairport config or visualizer media.

Not covered by `deploy.py` (they have their own installers; see the setup list below): the now-playing publisher, Auto Tune's measurement engine, and the visualizer media. The `*-playback.mp4` media are Git LFS files; fetch them with `git lfs pull` before a first-time install.

**Never edit files in `/opt/octet-remote` by hand or copy files there manually.** Every past AirPlay breakage traced back to a Pi that had drifted from any commit. If you must hot-fix, commit the fix, then run `deploy.py`.

Before any change, record what is running:

```bash
ssh octet@octet 'cat /opt/octet-remote/REVISION'
```

## Set up a new Pi from scratch

Do these in order. Anything marked **(quiet)** produces no sound. Keep speakers muted or amps off until the acceptance step.

1. **OS and network.** Raspberry Pi OS (Debian 13 "trixie") on a Pi 5, user `octet`, hostname `octet`, Tailscale installed and joined (`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up --ssh`). Enable the HiFiBerry DAC8x driver; the card must appear as `snd_rpi_hifiberry_dac8x` in `cat /proc/asound/cards`. Hardware: [`SYSTEM_HARDWARE.md`](SYSTEM_HARDWARE.md).
2. **Packages the installers do not fetch.** Classic (not AirPlay 2) Shairport, mDNS and ALSA tools:
   `sudo apt install shairport-sync avahi-daemon alsa-utils`. `install_airplay.py` rejects AirPlay 2 builds; no `nqptp` is needed. Confirm with `shairport-sync -V` (must say `ALSA` and `Avahi`, not `AirPlay2`).
3. **CamillaDSP, GUI and the loopback (quiet).** Copy `pi/` to the Pi (for example `scp -r pi octet@octet:~/octet-pi`) and run `bash install/setup.sh` from it. This installs CamillaDSP 4.1.3 and its GUI under `/opt/camilladsp` and creates a **three-substream** `snd-aloop` loopback (MPD = 0, AirPlay = 1, Bluetooth = 2). **Reboot** afterwards so the module loads with three substreams, then check `ls /proc/asound/Loopback/pcm0p/` shows `sub0 sub1 sub2`.
4. **MPD, upmpdcli (Tidal) and cava.** Follow `pi/install/install-media.sh` (it expects `/tmp/mpd.conf`, `/tmp/upmpdcli.conf`, `/tmp/cava.conf` and the upmpdcli signing key `/tmp/k.gpg` to be staged first; the files are `pi/etc/mpd.conf`, `pi/etc/upmpdcli.conf`, `pi/home/cava-config`). Do the one-time Tidal login described in [`pi/README.md`](../pi/README.md).
5. **systemd units and DSP config.** Copy `pi/systemd/camilladsp.service` and `camillagui.service` to `/etc/systemd/system/`; `pi/camilladsp/3way-lr4.yml` and `stereo-passthrough.yml` to `/opt/camilladsp/configs/`; and `pi/camilladsp/camillagui.yml` and `statefile.yml` to `/opt/camilladsp/`. CamillaDSP must listen on **127.0.0.1**, start muted and at −36 dB: `-p 1234 -a 127.0.0.1 ... --mute --gain=-36`. Then `sudo systemctl daemon-reload && sudo systemctl enable --now camilladsp camillagui`.
6. **The Octet remote (first-time installer).** On the Pi, from the staged `pi/remote/`: `sudo python3 install.py`. This is the *first-time* installer: it stops MPD and CamillaDSP, resets the CamillaDSP state file and rewrites `mpd.conf`. **Never run it on a Pi that is already set up**; use `deploy.py`. It leaves the system muted.
7. **AirPlay (quiet).** `sudo python3 install_airplay.py`. It validates the loopback, writes the Shairport config and service override, creates the `octet-airplay` group and the `airplay.conf` drop-in (with `RuntimeDirectoryMode=0750`), restarts the remote muted and starts Shairport. `--rollback` undoes it.
8. **Bluetooth.** Any paired phone can play; there is no allow-list. `python3 bluetooth_preflight.py` (read-only), then `sudo python3 install_bluetooth.py --apply` installs the A2DP receiver unit and the `bluetooth.conf` drop-in. Then make it work from boot with no attention:
   - Start the receiver at boot: `sudo systemctl add-wants multi-user.target octet-bluealsa.service` (the unit has no `[Install]` section, so `systemctl enable` does not apply).
   - Automatic sessions (a phone that starts streaming takes over, pausing Tidal): copy `pi/systemd/octet-remote-bluetooth-auto.conf` to `/etc/systemd/system/octet-remote.service.d/bluetooth-auto.conf`.
   - "Just Works" pairing with no code: copy `pi/systemd/octet-bluetooth-justworks.service` to `/etc/systemd/system/` and `sudo systemctl enable --now octet-bluetooth-justworks`. It sets the controller to NoInputNoOutput (and the Hi-Fi audio device class) every time `bluetooth.service` starts.
   - **No pairing agent may run.** The desktop taskbar's Bluetooth applet registers one that shows a passkey prompt on the Pi's own screen, so phones fail to pair. Remove it: copy `pi/home/wf-panel-pi.ini` to `~/.config/wf-panel-pi/wf-panel-pi.ini` (as user `octet`) and log in again or reboot.
   - `sudo systemctl daemon-reload`, then deploy. New phones pair from the **Bluetooth** panel on the Music page (**Let new phones pair**, on by default); octet-remote trusts each newly paired phone, and paired phones can always reconnect.
9. **Now-playing (Wi-Fi) publisher.** Copy `pi/now-playing/` to the Pi and run `sudo python3 install.py` there.
10. **Auto Tune (optional).** `pi/deploy_autotune.py` installs the measurement engine and service; see [`measure/docs/AUTO_TUNE.md`](../measure/docs/AUTO_TUNE.md).
11. **Record the revision.** From your PC run `python pi/remote/deploy.py`. This writes `REVISION` and proves the update path works on the new Pi.
12. **Acceptance.** [Verify AirPlay quietly](#verify-airplay-quietly), then do the one attended listening test at low level.

## Verify AirPlay quietly

These checks play nothing:

```bash
ssh octet@octet 'cat /opt/octet-remote/REVISION'
ssh octet@octet 'systemctl is-active shairport-sync octet-remote camilladsp mpd'
ssh octet@octet 'stat -c "%U:%G %a %n" /run/octet-airplay'   # must be upmpdcli:octet-airplay 750
ssh octet@octet 'curl -s http://127.0.0.1:8767/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[\"muted\"],d[\"airplay\"])"'
ssh octet@octet 'journalctl -u shairport-sync -n 5 --no-pager'   # no "hook failed"
```

Expected: services `active`; directory `750`; muted `True`; `airplay` is `{'active': False, 'preparing': False, 'error': None}`; no `Octet AirPlay hook failed` lines.

The attended test: with speakers at a low level, pick **Octet** in the AirPlay menu on a phone on the same Wi-Fi. The remote should show *Preparing*, then *AirPlay cast active* at −18 dB; stopping the cast mutes. Details: [`pi/remote/README.md`](../pi/remote/README.md#classic-airplay-wil-248).

## Troubleshooting AirPlay

**Casting is refused; the phone never connects; the journal says `Octet AirPlay hook failed`.**
Shairport runs a hook before each session that asks the Octet controller for permission over `/run/octet-airplay/control.sock`. If the hook cannot connect, Shairport refuses the session. The usual cause is the directory mode:

```bash
ssh octet@octet 'stat -c "%a %n" /run/octet-airplay; systemctl show octet-remote -p RuntimeDirectoryMode'
```

It must be `750`. systemd applies the **last** `RuntimeDirectoryMode` among the `octet-remote` drop-ins to every runtime directory, so a `0700` in `bluetooth.conf` locks Shairport (group `octet-airplay`) out of `/run/octet-airplay`. Fix: `python pi/remote/deploy.py` (it repairs the drop-ins), or by hand: set `RuntimeDirectoryMode=0750` in every drop-in, `sudo systemctl daemon-reload`, and `sudo chmod 750 /run/octet-airplay` for the running instance. The change takes full effect at the next `octet-remote` restart.

**The AirPlay status shows an error.** `curl -s http://127.0.0.1:8767/api/status` prints `airplay.error`; common causes are Auto Tune owning the audio outputs, an unapproved DSP profile, or a Bluetooth session holding the audio. The design is fail-closed: it stays muted and tells you why.

**Octet does not appear in the AirPlay list.** Phone and Pi must be on the same LAN (the Pi is on `wlan0`; Tailscale carries no mDNS). Check `systemctl is-active avahi-daemon shairport-sync`.

## Troubleshooting Bluetooth pairing

**A phone shows a code and then "cannot connect" / "pairing unsuccessful".** Something made the Pi ask for a passkey confirmation that nobody answers. Check for a pairing agent, which must not exist (bluetoothd logs `Calling Agent.RequestConfirmation` when one is registered):

```bash
ssh octet@octet 'systemctl is-active octet-bluetooth-justworks; grep -c bluetooth ~/.config/wf-panel-pi/wf-panel-pi.ini'
```

Expected: `active` and `0`. If the taskbar still lists `bluetooth`, restore `pi/home/wf-panel-pi.ini`. To see the handshake, run `sudo btmon` while pairing: the Pi's `IO Capability Request Reply` must say `NoInputNoOutput`; if it says `DisplayYesNo`, run `sudo systemctl restart octet-bluetooth-justworks`.

**Which code is the Pi running?** `cat /opt/octet-remote/REVISION`. If that file does not exist the Pi was installed by hand: run `deploy.py` to put it under version control.
