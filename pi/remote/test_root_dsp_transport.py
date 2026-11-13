"""Apply the existing wire-protocol tests to the broker's isolated client too."""
from unittest.mock import patch
import dsp_client
import test_dsp_transport
import test_server


class RootTransport(test_dsp_transport.DSPTransport):
    def setUp(self):
        p = patch.object(test_dsp_transport, 'server', dsp_client)
        p.start()
        self.addCleanup(p.stop)


class RootHandshake(test_server.HandshakeFailures):
    def setUp(self):
        p = patch.object(test_server, 'server', dsp_client)
        p.start()
        self.addCleanup(p.stop)
        super().setUp()
