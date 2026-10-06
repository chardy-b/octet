# Pi configuration

Raspberry Pi 5, Raspberry Pi OS (Debian 13 "trixie"), reachable over Tailscale as `octet`.

**These files were reconstructed from what was deployed, not copied from the live Pi** (it was unreachable when they were saved). Check them against the Pi before relying on them, for example `ssh octet@octet cat /etc/mpd.conf`.

```
Tidal / local files -> MPD (upmpdcli adds Tidal) -> ALSA loopback -> CamillaDSP -> HiFiBerry DAC8x (8 outputs)
                        \-> FIFO /run/mpd/mpd.fifo (pre-DSP tap for visualizers, e.g. cava)
```

| File here | Goes to on the Pi |
|---|---|
| `camilladsp/3way-lr4.yml`, `stereo-passthrough.yml` | `/opt/camilladsp/configs/` (the active one is named in `statefile.yml`) |
| `camilladsp/camillagui.yml`, `statefile.yml` | `/opt/camilladsp/` |
| `systemd/*.service` | `/etc/systemd/system/` |
| `etc/mpd.conf`, `etc/upmpdcli.conf` | `/etc/` |
| `etc/modules-load.d-snd-aloop.conf`, `etc/modprobe.d-snd-aloop.conf` | `/etc/modules-load.d/snd-aloop.conf`, `/etc/modprobe.d/snd-aloop.conf` |
| `etc/apt-upmpdcli.list` | `/etc/apt/sources.list.d/upmpdcli.list` |
| `home/cava-config` | `~/.config/cava/config` |
| `install/` | the scripts used for the first install |

- CamillaDSP 4.1.3 and its GUI (port 5005, websocket 1234) are installed under `/opt/camilladsp`, and run as services.
- The 3-way config is a placeholder: LR4 crossovers at 300 Hz and 3 kHz, an assumed output order, and a main volume of 0 dB.
- Tidal needs a one-time login. The `tidalapi` Python package is installed with pip into `/usr/local`, and the login credentials live in `/var/cache/upmpdcli/tidal/`. They are not stored here.
