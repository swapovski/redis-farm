"""Server-only LDAP gate, stateless login CSRF and bounded login rate limit.

The session cookie is an opaque random ID. Identities and Redis credentials are
held by the existing Sessions object; LDAP passwords are never assigned to it.
"""
from collections import OrderedDict, deque
import hashlib
import hmac
import secrets
import threading
import time


class LoginLimiter:
    """Limit requests per peer and username without persisting identifiers."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.items = OrderedDict()
        self.key = secrets.token_bytes(32)

    def allow(self, peer, username):
        now = self.clock()
        keys = [(('peer', peer), 30), (('user', username.casefold()), 8)]
        with self.lock:
            # Expire old buckets. A fixed global cap prevents arbitrary-name growth.
            for key, stamps in list(self.items.items()):
                while stamps and stamps[0] < now - 60:
                    stamps.popleft()
                if not stamps:
                    del self.items[key]
            allowed = True
            for value, limit in keys:
                key = hmac.new(self.key, repr(value).encode(), hashlib.sha256).digest()
                if key not in self.items and len(self.items) >= 4096:
                    return False
                stamps = self.items.setdefault(key, deque(maxlen=limit))
                if len(stamps) >= limit:
                    allowed = False
                else:
                    stamps.append(now)
            return allowed


class LoginCSRF:
    """Signed pre-login token, reused across tabs until it expires.

    The signing key is process-local. A restart invalidates old tokens on
    purpose; /login/state supplies a new token before an intentional POST.
    Neither tokens nor this class contain user passwords or identities.
    """
    def __init__(self, key=None, max_age=900):
        from itsdangerous import URLSafeTimedSerializer
        self.signer = URLSafeTimedSerializer(
            key if key is not None else secrets.token_bytes(32),
            salt='redis-farm-login-csrf')
        self.max_age = max_age

    def valid(self, token):
        from itsdangerous import BadSignature
        if not isinstance(token, str) or not token or len(token) > 1024:
            return False
        try:
            value = self.signer.loads(token, max_age=self.max_age)
        except (BadSignature, ValueError, TypeError, UnicodeError):
            return False
        return isinstance(value, str) and 32 <= len(value) <= 128

    def issue(self, cookie):
        # Do not invalidate an already-open form by visiting /login again.
        return cookie if self.valid(cookie) else self.signer.dumps(secrets.token_urlsafe(32))

    def matches(self, supplied, cookie):
        if not isinstance(cookie, str) or not self.valid(supplied):
            return False
        if len(cookie) > 1024:
            return False
        return hmac.compare_digest(supplied.encode('utf-8'), cookie.encode('utf-8'))


def install_auth(app, settings, sessions, clear_runtime, authenticator, cookie_name):
    from flask import g, request, redirect, url_for, make_response, render_template, jsonify
    from .models import FarmError
    from .ldap_auth import LoginDenied, LoginUnavailable
    from .i18n import Translator
    t = Translator(settings.language, admin_user=settings.redis_admin_user)
    login_csrf = LoginCSRF()
    csrf_cookie = '__Host-redis_farm_login_csrf'
    limiter = LoginLimiter()

    def same_origin():
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise FarmError('Cross-site requests are not accepted.', 'origin', 403)
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            raise FarmError('Origin does not match this panel.', 'origin', 403)

    def set_login_token(response, token):
        if token != request.cookies.get(csrf_cookie):
            response.set_cookie(csrf_cookie, token, max_age=login_csrf.max_age,
                                secure=True, httponly=True, samesite='Strict', path='/')
        return response

    def login_page(error='', status=200, username='', error_code=''):
        token = login_csrf.issue(request.cookies.get(csrf_cookie))
        response = make_response(render_template('login.html', csrf=token,
            error=t(error), error_code=error_code, username=username,
            version=app.config['FARM_VERSION']), status)
        return set_login_token(response, token)

    def protect():
        # Called after network policy, before dispatching any panel/API route.
        if request.endpoint in ('login', 'login_state', 'static', 'health', 'ui_messages'):
            return None
        g.browser = sessions.get(request.cookies.get(cookie_name))
        if not g.browser or not g.browser.username:
            stale = request.cookies.get(cookie_name)
            if stale:
                clear_runtime(stale)
                sessions.remove(stale)
            if request.path.startswith('/api/'):
                raise FarmError('Sign in to Redis Farm.', 'login_required', 401)
            return redirect(url_for('login'))
        return None

    @app.get('/login/state')
    def login_state():
        # Only our same-origin JS requests a fresh form token. No LDAP bind,
        # session allocation or Redis commands occur in this endpoint.
        same_origin()
        if request.headers.get('X-RF-Login') != 'refresh':
            raise FarmError('A login refresh header is required.', 'origin', 403)
        existing = sessions.get(request.cookies.get(cookie_name))
        if existing and existing.username:
            return jsonify(authenticated=True)
        token = login_csrf.issue(request.cookies.get(csrf_cookie))
        return set_login_token(jsonify(authenticated=False, csrf=token), token)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        same_origin()
        if request.method == 'GET':
            existing = sessions.get(request.cookies.get(cookie_name))
            if existing and existing.username:
                return redirect(url_for('index'))
            return login_page()
        existing = sessions.get(request.cookies.get(cookie_name))
        if existing and existing.username:
            # Another tab may have completed login; do not log in a second time.
            return redirect(url_for('index'))
        supplied = request.form.get('csrf', '')
        cookie = request.cookies.get(csrf_cookie, '')
        if not login_csrf.matches(supplied, cookie):
            return login_page(
                '\u0424\u043e\u0440\u043c\u0430 \u0432\u0445\u043e\u0434\u0430 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0430. \u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043f\u0430\u0440\u043e\u043b\u044c \u0435\u0449\u0451 \u0440\u0430\u0437.',
                400, request.form.get('username', '')[:128], error_code='csrf')
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if not limiter.allow(request.remote_addr or '', username[:128]):
            response = login_page('\u0421\u043b\u0438\u0448\u043a\u043e\u043c \u043c\u043d\u043e\u0433\u043e \u043f\u043e\u043f\u044b\u0442\u043e\u043a. \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u0447\u0435\u0440\u0435\u0437 \u043c\u0438\u043d\u0443\u0442\u0443.', 429, error_code='rate_limited')
            response.headers['Retry-After'] = '60'
            return response
        try:
            identity = authenticator.authenticate(username, password)
        except LoginDenied:
            return login_page('\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u043b\u043e\u0433\u0438\u043d, \u043f\u0430\u0440\u043e\u043b\u044c \u0438\u043b\u0438 \u043d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430 \u043a \u043f\u0430\u043d\u0435\u043b\u0438.', 401, username[:128], error_code='login_denied')
        except LoginUnavailable:
            return login_page('\u0421\u043b\u0443\u0436\u0431\u0430 \u0432\u0445\u043e\u0434\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430. \u041e\u0431\u0440\u0430\u0442\u0438\u0442\u0435\u0441\u044c \u043a \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443.', 503, username[:128], error_code='login_unavailable')
        finally:
            # Clearing the reference is not a guarantee of zeroization in Python.
            password = None
        old = request.cookies.get(cookie_name)
        if old:
            clear_runtime(old)
            sessions.remove(old)
        browser = sessions.create(username=identity.username, user_dn=identity.dn)
        response = redirect(url_for('index'))
        response.set_cookie(cookie_name, browser.id, secure=True, httponly=True,
                            samesite='Strict', path='/')
        response.delete_cookie(csrf_cookie, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    @app.post('/api/panel/logout')
    def logout():
        # CSRF/API gate from app.py has already run.
        owner = g.browser.id
        clear_runtime(owner)
        sessions.remove(owner)
        response = jsonify(ok=True, login_url='/login')
        response.delete_cookie(cookie_name, path='/', secure=True, httponly=True, samesite='Strict')
        return response

    app.extensions['redis_farm']['login_limiter'] = limiter
    return protect
