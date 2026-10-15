"""Dedicated direct-TLS deployment. Do not preload or add application workers."""
import ssl
from redis_farm.server import load_config

farm = load_config()
bind = farm.bind
workers = 1
worker_class = 'gthread'
threads = farm.threads
worker_connections = 256
backlog = 128
timeout = 120
graceful_timeout = 10
keepalive = 5
preload_app = False
reload = False
max_requests = 0
certfile = str(farm.tls_cert)
keyfile = str(farm.tls_key)
# No reverse proxy at this stage: spoofable forwarded headers have no effect.
forwarded_allow_ips = ''
secure_scheme_headers = {}
proxy_protocol = False
accesslog = None
errorlog = '-'
loglevel = 'info'
capture_output = False
worker_tmp_dir = '/run/redis-farm'
umask = 0o077


def ssl_context(conf, default_ssl_context_factory):
    context = default_ssl_context_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def on_starting(server):
    if server.cfg.workers != 1 or server.cfg.preload_app or server.cfg.reload or server.cfg.max_requests:
        raise RuntimeError('Memory-only sessions require one worker, no preload/reload/max_requests.')
    if server.cfg.worker_class_str != 'gthread':
        raise RuntimeError('Use the supplied gthread worker configuration.')


def worker_exit(server, worker):
    app = getattr(worker, 'wsgi', None)
    extensions = getattr(app, 'extensions', {})
    farm_runtime = extensions.get('redis_farm')
    if farm_runtime:
        farm_runtime['shutdown']()
