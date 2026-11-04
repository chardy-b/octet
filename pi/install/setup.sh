set -e
install_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sudo apt-get install -y -qq libasound2t64 alsa-utils python3-venv curl >/dev/null
sudo mkdir -p /opt/camilladsp/{configs,coeffs}
cd /tmp
curl -sLO https://github.com/HEnquist/camilladsp/releases/download/v4.1.3/camilladsp-linux-aarch64.tar.gz
curl -sLO https://github.com/HEnquist/camillagui-backend/releases/download/v4.1.0/bundle_linux_aarch64.tar.gz
sudo tar xzf camilladsp-linux-aarch64.tar.gz -C /opt/camilladsp
sudo mkdir -p /opt/camilladsp/gui && sudo tar xzf bundle_linux_aarch64.tar.gz -C /opt/camilladsp/gui --strip-components=0
sudo chmod +x /opt/camilladsp/camilladsp
ls /opt/camilladsp /opt/camilladsp/gui
# Keep logs across reboots so a hardware fault can be diagnosed afterwards.
sudo install -D -m 0644 "$install_dir/../etc/60-octet-journal.conf" /etc/systemd/journald.conf.d/60-octet-journal.conf
sudo systemctl restart systemd-journald
sudo journalctl --flush
# loopback for player input
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf >/dev/null
echo 'options snd-aloop index=10 pcm_substreams=3' | sudo tee /etc/modprobe.d/snd-aloop.conf >/dev/null
sudo modprobe snd-aloop index=10 pcm_substreams=3 || true
/opt/camilladsp/camilladsp --version
