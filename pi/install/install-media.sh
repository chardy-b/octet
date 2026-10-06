#!/bin/bash
set -e
sudo install -d /usr/share/keyrings
sudo gpg --dearmor --yes -o /usr/share/keyrings/lesbonscomptes.gpg /tmp/k.gpg
echo "deb [signed-by=/usr/share/keyrings/lesbonscomptes.gpg] https://www.lesbonscomptes.com/upmpdcli/downloads/raspbian/ trixie main" | sudo tee /etc/apt/sources.list.d/upmpdcli.list >/dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mpd mpc cava upmpdcli upmpdcli-tidal
sudo systemctl stop mpd mpd.socket upmpdcli 2>/dev/null || true
sudo cp /tmp/mpd.conf /etc/mpd.conf
sudo install -d -o mpd -g audio /var/lib/mpd/music
sudo cp /tmp/upmpdcli.conf /etc/upmpdcli.conf
mkdir -p ~/.config/cava && cp /tmp/cava.conf ~/.config/cava/config
sudo systemctl enable --now mpd upmpdcli
sleep 3
systemctl is-active mpd upmpdcli
mpc outputs
ls -l /run/mpd/
echo DONE
