# Octet Wi-Fi now playing publisher

Copy this directory to a staging folder on Octet and run `sudo python3 install.py`.
The matching source is also kept in the Octet project at `pi/now-playing/`.
Requires the existing Octet remote service at `127.0.0.1:8767`, Python 3, iproute2,
systemd and busctl. No new packages are required on the current Pi.

The installer backs up the Octet music page, adds a **Now playing · Wi-Fi** link
to the song display, and starts a separate socket and publisher service. The UI
files are read per request; reload the Music page to see the link. It does not
restart Octet remote, Shairport, MPD or CamillaDSP. The standalone page shows
AirPlay or Tidal album cover, title, artist and album without changing the music controls.

- `http://octet.local:8768/`: a standalone song display on Octet.
- `http://octet.local:8768/api/now-playing`: the read-only JSON feed for the mirror.
- `/api/artwork/<content hash>`: the current cover, with the same Wi-Fi restrictions.
- `octet-now-playing.socket`: socket bound to `wlan0` with `BindToDevice`.
- `octet-now-playing.service`: unprivileged publisher activated by the socket.

Requests must arrive on Wi-Fi from Octet's directly connected Wi-Fi subnet, target
its current Wi-Fi IP, and use its Wi-Fi IP or `octet.local` Host header. Interface
addresses are discovered per request, including after DHCP changes. Loopback,
Tailscale, Ethernet, public clients and forwarded proxies cannot read this feed.
CORS permits only the existing Octet music UI on its Wi-Fi hostname/address.
There are no write routes or playback controls. Other existing Octet services keep
their current network policies; this restriction applies to the new song feed.

The JSON contains version, source, source label, playback state, title, artist,
album, elapsed/duration, timestamp and a local artwork path. It excludes credentials,
queue contents, stream URLs and upstream artwork URLs. Shairport metadata comes from its existing local MPRIS
interface ([upstream documentation](https://github.com/mikebrady/shairport-sync)).
The snapshot cache lasts at most two seconds, failures return 503, and the mirror
rejects snapshots older than 30 seconds. The Pis should keep their clocks synced.

AirPlay artwork is read from Shairport's cover cache through a read-only systemd
bind mount at `/run/octet-shairport`; the publisher keeps its private `/tmp`.
For Tidal, only HTTPS artwork on `resources.tidal.com/images/` is fetched and
cached by Octet, without redirects. The mirror never contacts the artwork provider.
JPEG, PNG and WebP covers are limited to 2 MiB; unsupported or unavailable images
do not hide song details. Only the current image is kept in memory and published
under its SHA-256 hash. Switching source or stopping playback clears it.

Check `systemctl status octet-now-playing.socket octet-now-playing.service` and
`journalctl -u octet-now-playing.service` on Octet. From the mirror, verify:

```sh
ip route get 192.168.68.63
curl --interface wlan0 http://octet.local:8768/api/now-playing
```

The route must show `dev wlan0`. Connecting to Octet's Tailscale address on port
8768 must fail. Guest Wi-Fi/client isolation must permit the two Pis to communicate.
There is no router port forwarding or internet endpoint. The mirror needs only
outbound HTTP; its web-server whitelist does not need to be widened.

Run `python3 -m unittest discover -s . -p 'test_now_playing.py'` in this directory.
To disable the feature, stop/disable `octet-now-playing.socket` and stop
`octet-now-playing.service`; remove the mirror module from its config. UI originals
are saved under `/var/backups/octet-now-playing/` on Octet.
