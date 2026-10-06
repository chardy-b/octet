set -e
sudo apt-get install -y -qq libasound2t64 alsa-utils python3-venv curl >/dev/null
sudo mkdir -p /opt/camilladsp/{configs,coeffs}
cd /tmp
curl -sLO https://github.com/HEnquist/camilladsp/releases/download/v4.1.3/camilladsp-linux-aarch64.tar.gz
curl -sLO https://github.com/HEnquist/camillagui-backend/releases/download/v4.1.0/bundle_linux_aarch64.tar.gz
sudo tar xzf camilladsp-linux-aarch64.tar.gz -C /opt/camilladsp
sudo mkdir -p /opt/camilladsp/gui && sudo tar xzf bundle_linux_aarch64.tar.gz -C /opt/camilladsp/gui --strip-components=0
sudo chmod +x /opt/camilladsp/camilladsp
ls /opt/camilladsp /opt/camilladsp/gui
# loopback for player input
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf >/dev/null
echo 'options snd-aloop index=10 pcm_substreams=2' | sudo tee /etc/modprobe.d/snd-aloop.conf >/dev/null
sudo modprobe snd-aloop index=10 pcm_substreams=2 || true
/opt/camilladsp/camilladsp --version
