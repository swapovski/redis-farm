"""Installer helpers tested in temp directories, not against systemd or Redis."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from redis_farm import __version__
from redis_farm.server import parse_config, check_certificate

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('farm_installer', ROOT/'deploy/ubuntu/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallHelperTests(unittest.TestCase):
    def test_current_link_switch_preserves_other_release(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(installer, 'ROOT', Path(temp)):
            a, b = Path(temp)/'a', Path(temp)/'b'
            a.mkdir(); b.mkdir()
            installer.point_current(a)
            self.assertEqual((Path(temp)/'current').resolve(), a)
            installer.point_current(b)
            self.assertEqual((Path(temp)/'current').resolve(), b)
            self.assertTrue(a.is_dir())

    def test_atomic_config_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'config.json'
            installer.atomic_text(path, '{}', uid=os.getuid(), gid=os.getgid())
            self.assertEqual(path.read_text(), '{}')
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            installer.atomic_text(path, '{"x":1}', uid=os.getuid(), gid=os.getgid())
            self.assertEqual(json.loads(path.read_text()), {'x':1})
            self.assertEqual(len(list(Path(temp).iterdir())), 1)

    def test_bound_port_is_not_replaced(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); sock.listen()
            conf = parse_config({'bind_host':'127.0.0.1', 'port':sock.getsockname()[1]})
            with self.assertRaises(OSError):
                installer.verify_bind(conf, False)

    def test_unused_interface_is_rejected(self):
        conf = parse_config({'bind_host':'192.0.2.240', 'allowed_client_cidrs':['192.0.2.0/24']})
        with self.assertRaises(OSError):
            installer.verify_bind(conf, False)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL is required for certificate integration checks')
class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root/'tls').mkdir()
        cls.conf = parse_config({'bind_host':'127.0.0.1',
            'tls_cert':str(cls.root/'tls/server.crt'), 'tls_key':str(cls.root/'tls/server.key')})
        with patch.object(installer, 'CONF_DIR', cls.root), patch('os.chown'):
            installer.certificate(cls.conf, os.getgid())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_certificate_pair_and_ip_san(self):
        check_certificate(self.conf)
        result = subprocess.run(['openssl','x509','-in',str(self.conf.tls_cert),
                                 '-noout','-ext','subjectAltName'], capture_output=True, check=True)
        self.assertIn(b'IP Address:127.0.0.1', result.stdout)
        self.assertEqual(self.conf.tls_key.stat().st_mode & 0o777, 0o640)

    def test_existing_certificate_not_replaced(self):
        before = self.conf.tls_key.read_bytes()
        with patch.object(installer, 'CONF_DIR', self.root):
            installer.certificate(self.conf, os.getgid())
        self.assertEqual(self.conf.tls_key.read_bytes(), before)

    def test_partial_pair_not_overwritten(self):
        conf = replace(self.conf, tls_key=self.root/'tls/missing.key')
        with self.assertRaises(RuntimeError):
            installer.certificate(conf, os.getgid())
        self.assertFalse(conf.tls_key.exists())

    def test_real_loopback_tls_with_cert_validation(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({'status':'ok','version':__version__}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body))); self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = HTTPServer(('127.0.0.1',0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.conf.tls_cert, self.conf.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        config = replace(self.conf, settings=replace(self.conf.settings, port=server.server_address[1]))
        try:
            installer.health(config, seconds=3)
            # A domain not present in this certificate must fail; never CERT_NONE.
            with self.assertRaises(RuntimeError):
                installer.health(replace(config, public_host='mismatch.example'), seconds=.2)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__': unittest.main()
