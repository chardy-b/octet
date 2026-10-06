"""Read-only check of a DLNA renderer: transport state and volume. Plays nothing."""
import re
import sys
import urllib.request
from urllib.parse import urljoin

from octetmeasure import upnp

r = upnp.find_renderer(sys.argv[1] if len(sys.argv) > 1 else "wiim")
print(r.name, r.ip, r.model)
print("transport state:", upnp.transport_state(r))

desc = urllib.request.urlopen(r.location, timeout=4).read().decode()
m = re.search(r"RenderingControl:1</serviceType>.*?<controlURL>(.*?)</controlURL>", desc, re.S)
if m:
    rc = "urn:schemas-upnp-org:service:RenderingControl:1"
    env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:GetVolume xmlns:u="{rc}"><InstanceID>0</InstanceID><Channel>Master</Channel></u:GetVolume>'
           '</s:Body></s:Envelope>').encode()
    req = urllib.request.Request(urljoin(r.location, m.group(1)), env,
                                 {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{rc}#GetVolume"'})
    out = urllib.request.urlopen(req, timeout=5).read().decode()
    print("volume:", re.search(r"<CurrentVolume>(.*?)</CurrentVolume>", out).group(1))
else:
    print("no RenderingControl service found")
