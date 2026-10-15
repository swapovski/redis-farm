import atexit
from dataclasses import replace
import hmac
import threading
import time
from flask import Flask, g, jsonify, render_template, request, make_response
from werkzeug.exceptions import HTTPException
from . import __version__
from .access import AccessPolicy
from .cli import RedisCLI
from .commands import WIDGETS, snippets
from .discovery import Discovery
from .models import Endpoint, FarmError, service_auth
from .credentials import DiscoveryCredentials, password_for, validate_password
from .runtime import Jobs, Sessions
from .settings import Settings
from .storage import Inventory
from .telemetry import Telemetry
from .terminal import Terminals

COOKIE = 'redis_farm_local_session'
SERVER_COOKIE = '__Host-redis_farm_server_session'


def create_app(settings: Settings, *, cli=None, authenticator=None):
    policy = AccessPolicy(settings)
    cookie_name = SERVER_COOKIE if settings.server_mode else COOKIE
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024, FARM_VERSION=__version__)
    login_enabled = settings.server_mode and settings.panel_auth == 'ldap'
    if login_enabled and authenticator is None:
        from .ldap_config import load_ldap_config
        from .ldap_auth import LDAPAuthenticator
        authenticator = LDAPAuthenticator(load_ldap_config(settings.ldap_connector))
    from .i18n import Translator, localize_errors
    translator = Translator(settings.language, admin_user=settings.redis_admin_user)
    app.json.ensure_ascii = False
    cli = cli or RedisCLI(settings)
    store = Inventory(settings.data_dir, settings.max_nodes, resolver=getattr(cli, 'resolver', None))
    sessions = Sessions(settings)
    jobs = Jobs()
    telemetry = Telemetry(cli, settings)
    discovery = Discovery(cli, settings)
    terminals = Terminals(cli, settings)
    stop = threading.Event()

    def clear_runtime(owner):
        sessions.lock_password(owner)
        jobs.clear_owner(owner)
        terminals.close_owner(owner)
        cli.close_owner(owner)
        telemetry.clear_owner(owner)

    def invalidate(owner, *, service_id=None, profile=None):
        # Cancel in-flight reads; other saved session credentials remain intact.
        jobs.clear_owner(owner)
        cli.close_owner(owner)
        telemetry.clear_owner(owner)
        terminals.close_scope(owner, service_id=service_id, profile=profile)

    def shutdown():
        if stop.is_set():
            return
        stop.set()
        for sid in list(sessions.items):
            clear_runtime(sid)
            sessions.remove(sid)
        jobs.shutdown()
        terminals.shutdown()
        cli.shutdown()

    app.extensions['redis_farm'] = {'cli': cli, 'inventory': store, 'sessions': sessions,
                                    'jobs': jobs, 'telemetry': telemetry, 'terminals': terminals,
                                    'shutdown': shutdown}
    atexit.register(shutdown)
    protect = None
    if login_enabled:
        from .panel_auth import install_auth
        protect = install_auth(app, settings, sessions, clear_runtime, authenticator, cookie_name)

    def janitor():
        while not stop.wait(5):
            for sid in sessions.expired():
                clear_runtime(sid)
                sessions.remove(sid)
            terminals.reap()
            jobs.reap()
            sessions.reap_discovery()
    threading.Thread(target=janitor, daemon=True, name='farm-cleanup').start()

    @app.before_request
    def access_control():
        # REMOTE_ADDR is the real TCP peer. Forwarded headers are never trusted.
        policy.check(request.host, request.remote_addr or '', request.scheme)
        if protect is not None:
            response = protect()
            if response is not None:
                return response
        if request.path.startswith('/api/'):
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                raise FarmError('Cross-site requests are not accepted.', 'origin', 403)
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                raise FarmError('Origin does not match this panel.', 'origin', 403)
            g.browser = sessions.get(request.cookies.get(cookie_name))
            if request.path != '/api/bootstrap' and g.browser is None:
                raise FarmError('Browser session expired. Reload the page.', 'session_expired', 401)
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                supplied = request.headers.get('X-RF-CSRF', '')
                if not g.browser or not hmac.compare_digest(supplied.encode('utf-8'), g.browser.csrf.encode('utf-8')):
                    raise FarmError('Reload the page to refresh the session token.', 'csrf', 403)
                if request.content_length and not request.is_json:
                    raise FarmError('JSON request body is required.', 'invalid_json', 415)

    @app.after_request
    def headers(response):
        # Translate only panel-owned error fields. Raw Redis output/data is untouched.
        if response.is_json:
            payload = response.get_json(silent=True)
            if payload is not None:
                translated = localize_errors(payload, translator)
                if translated != payload:
                    response.set_data(app.json.dumps(translated))
        response.headers['Content-Language'] = settings.language
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.errorhandler(FarmError)
    def farm_error(exc):
        return jsonify(error=str(exc), code=exc.code), exc.status

    @app.errorhandler(Exception)
    def generic_error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description, code='http_error'), exc.code
        # No request/exception trace logging: console commands may contain secrets.
        return jsonify(error=f'Internal request error ({type(exc).__name__}).', code='internal_error'), 500

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise FarmError('JSON object is required.', 'invalid_json')
        return value

    def target(data):
        service = store.get(data.get('service_id', ''))
        value = next((e for e in service['endpoints'] if e['id'] == data.get('endpoint_id')), None)
        if value is None:
            raise FarmError('Select an endpoint from the current service.', 'endpoint_required')
        return service, Endpoint.from_dict(value)

    def submit(key, fn, commit=None, credential_source=None):
        owner = g.browser.id
        generation = g.browser.generation
        def guarded(cancel):
            current = sessions.get(owner)
            if current is None or current.generation != generation or cancel.is_set():
                raise FarmError('Session credentials changed; operation was cancelled.', 'cancelled')
            credentials = credential_source(owner) if credential_source else sessions.password(owner)
            result = fn(owner, credentials, cancel)
            current = sessions.get(owner)
            if current is None or current.generation != generation or cancel.is_set():
                raise FarmError('Session was locked while the operation was running.', 'cancelled')
            return result
        def guarded_commit(result):
            # Job lock serializes commit/cancel; session lock protects generation.
            with sessions.lock:
                current = sessions.get(owner)
                if current is None or current.generation != generation:
                    raise FarmError('Session credentials changed; nothing was added.', 'cancelled')
                return commit(result)
        job = jobs.submit(owner, key, guarded, commit=guarded_commit if commit else None)
        return jsonify(job_id=job.id), 202

    @app.context_processor
    def template_assets():
        from pathlib import Path
        return {'t': translator, 'language': settings.language,
                'admin_user': settings.redis_admin_user, 'use_acl_user': settings.use_acl_user,
                'panel_login': login_enabled, 'ui_catalog': translator.web(), 'brand_logo': (Path(app.static_folder) / 'logo.png').is_file(),
                'add_logo': (Path(app.static_folder) / 'add.png').is_file()}

    @app.get('/')
    def index():
        return render_template('index.html', version=__version__, server_mode=settings.server_mode,
                               panel_user=g.browser.username if login_enabled else None)

    @app.get('/ui-messages.json')
    def ui_messages():
        return jsonify(language=settings.language, messages=translator.catalog,
                       admin_user=settings.redis_admin_user)

    @app.get('/healthz')
    def health():
        # Process liveness only: no Redis calls and no browser session allocation.
        return jsonify(status='ok', version=__version__)

    @app.get('/api/bootstrap')
    def bootstrap():
        browser = g.browser
        created = browser is None
        if created:
            old = request.cookies.get(cookie_name)
            if old:
                clear_runtime(old)
                sessions.remove(old)
            browser = sessions.create()
        response = make_response(jsonify(version=__version__, csrf=browser.csrf,
            redis_unlocked=browser.password is not None, cli=cli.status(),
            refresh_seconds=settings.refresh_seconds, data_dir=str(settings.data_dir), widgets=WIDGETS,
            discovery_defaults=discovery.defaults(), server_mode=settings.server_mode,
            panel_login=login_enabled, panel_user=browser.username,
            language=settings.language, admin_user=settings.redis_admin_user, use_acl_user=settings.use_acl_user))
        # Existing cookies need no refresh. A delayed bootstrap response must
        # not restore an old session cookie after login/logout in another tab.
        if created:
            response.set_cookie(cookie_name, browser.id, secure=settings.server_mode,
                                httponly=True, samesite='Strict', path='/')
        return response

    @app.post('/api/session/unlock')
    def unlock():
        password = body().get('password')
        validate_password(password)
        invalidate(g.browser.id, profile='admin')
        sessions.unlock(g.browser.id, password)
        return jsonify(redis_unlocked=True)

    @app.post('/api/session/lock')
    def lock():
        data = request.get_json(silent=True) or {}
        if data.get('profile') == 'admin':
            invalidate(g.browser.id, profile='admin')
            sessions.lock_password(g.browser.id, all_profiles=False)
        else:
            clear_runtime(g.browser.id)
        return jsonify(redis_unlocked=False)

    def credential_target(service, data):
        if service_auth(service) != 'default':
            raise FarmError('This service does not use password-only authentication.', 'invalid_auth')
        scope = data.get('scope', 'service')
        if scope == 'service':
            return None
        if scope != 'endpoint':
            raise FarmError('Choose service or endpoint credential scope.', 'invalid_scope')
        raw = next((e for e in service['endpoints'] if e['id'] == data.get('endpoint_id')
                    and e['kind'] == 'redis'), None)
        if raw is None:
            raise FarmError('Select a Redis endpoint in this service.', 'endpoint_required')
        ep = Endpoint.from_dict(raw)
        ids = {ep.id}
        # Share an explicit override across existing forward-DNS aliases of this port.
        # PTR labels are not identity and are not used to choose credentials.
        resolver = getattr(cli, 'resolver', None)
        if resolver is not None:
            try:
                address = resolver.resolve(ep.host).connect_host
                for other in service['endpoints']:
                    if other['kind'] == 'redis' and other['port'] == ep.port:
                        try:
                            if resolver.resolve(other['host']).connect_host == address:
                                ids.add(other['id'])
                        except FarmError:
                            pass
            except FarmError:
                pass
        return sorted(ids)

    @app.post('/api/services/<sid>/credentials')
    def service_unlock(sid):
        service, data = store.get(sid), body()
        password = data.get('password')
        validate_password(password)
        ids = credential_target(service, data)
        invalidate(g.browser.id, service_id=sid)
        sessions.set_default(g.browser.id, sid, password, ids)
        return jsonify(credentials=sessions.credential_state(g.browser.id, service))

    @app.delete('/api/services/<sid>/credentials')
    def service_lock(sid):
        service, data = store.get(sid), body()
        ids = credential_target(service, data)
        invalidate(g.browser.id, service_id=sid)
        sessions.forget_default(g.browser.id, sid, ids)
        return jsonify(credentials=sessions.credential_state(g.browser.id, service))

    @app.post('/api/session/touch')
    def touch():
        sessions.touch(g.browser.id)
        return jsonify(ok=True)

    @app.get('/api/services')
    def services_list():
        result = []
        for service in store.list():
            last = telemetry.last(g.browser.id, service['id'])
            result.append({**service, 'credentials': sessions.credential_state(g.browser.id, service), 'last_summary': last['summary'] if last else None,
                           'last_sampled_at': last['sampled_at'] if last else None})
        return jsonify(services=result)

    @app.get('/api/services/<sid>/topology')
    def service_topology(sid):
        # Metadata + forward DNS only: no redis-cli process or password needed.
        from .topology_view import catalog_topology
        return jsonify(catalog_topology(store.get(sid), getattr(cli, 'resolver', None)))

    @app.post('/api/services')
    def services_save():
        data = body()
        items = data.get('services', [])
        if not isinstance(items, list) or not 1 <= len(items) <= settings.max_services:
            raise FarmError('Select the discovered services to add.', 'invalid_services')
        from .models import clean_service
        clean = [clean_service(v, settings.max_nodes) for v in items]
        token = data.get('credential_ref')
        credentials = sessions.discovery_credentials(g.browser.id, token) if token else None
        before = {s['id'] for s in store.list()}
        saved = store.save_many(clean, preserve_labels=True)
        if credentials:
            credentials.bind(sessions, g.browser.id, saved)
            sessions.forget_discovery(g.browser.id, token)
        return jsonify(services=saved, addition=addition_info(saved, before))

    @app.patch('/api/services/<sid>')
    def services_edit(sid):
        data = body()
        old = store.get(sid)
        return jsonify(service=store.save({**old, 'name': data.get('name', old['name']),
                                          'group': data.get('group', old['group'])}, sid))

    @app.delete('/api/services/<sid>')
    def services_delete(sid):
        store.delete(sid)
        return jsonify(ok=True)

    @app.get('/api/catalog/export')
    def export():
        return jsonify(store.export())

    @app.post('/api/catalog/import')
    def import_catalog():
        return jsonify(services=store.import_data(body()))

    def addition_info(saved, before):
        return {'created': sum(s['id'] not in before for s in saved),
                'updated': sum(s['id'] in before for s in saved), 'total': len(saved)}

    @app.post('/api/discover')
    def discover():
        data = body()
        plan = discovery.plan(data)
        auto_add = data.get('auto_add', plan['mode'] == 'node')
        if not isinstance(auto_add, bool):
            raise FarmError('auto_add must be true or false.', 'invalid_spec')
        owner = g.browser.id
        secret = data.pop('default_password', None)
        port = data.pop('password_port', plan['profile'].port)
        if secret == '':
            secret = None
        if secret is not None and plan['profile'].auth != 'default':
            raise FarmError('Default password requires the default profile.', 'invalid_auth')
        if secret is not None:
            from .models import validate_port
            port = validate_port(port)
            if port not in {ep.port for ep, hint in plan['probes'] if hint != 'sentinel'}:
                raise FarmError('Password port must be a Redis port in the selected scan range.', 'invalid_port')
        credentials = DiscoveryCredentials(
            admin=sessions.password(owner) if plan['profile'].auth == 'admin' else None,
            seed=replace(plan['profile'], port=port), password=secret,
            resolver=getattr(cli, 'resolver', None))
        token = sessions.hold_discovery(owner, credentials)
        def scan(owner, pw, cancel):
            return discovery.discover(data, pw, owner, cancel,
                progress=lambda value: jobs.report(owner, cancel, value))
        def save_result(result):
            before = {s['id'] for s in store.list()}
            result['auto_added'] = auto_add
            result['saved_services'] = (store.save_many(
                [x['service'] for x in result['services']], preserve_labels=True)
                if auto_add and result['services'] else [])
            result['addition'] = addition_info(result['saved_services'], before)
            if auto_add:
                credentials.bind(sessions, owner, result['saved_services'])
                sessions.forget_discovery(owner, token)
            else:
                result['credential_ref'] = token
            return result
        return submit('discovery', scan, commit=save_result,
                      credential_source=lambda sid: sessions.discovery_credentials(sid, token))

    @app.get('/api/jobs/<jid>')
    def job_status(jid):
        return jsonify(jobs.get(g.browser.id, jid).public())

    @app.delete('/api/jobs/<jid>')
    def job_cancel(jid):
        jobs.cancel_job(g.browser.id, jid)
        return jsonify(ok=True)

    @app.post('/api/services/<sid>/snapshot')
    def snapshot(sid):
        service = store.get(sid)
        def collect(owner, pw, cancel):
            result = telemetry.snapshot(service, pw, owner, cancel)
            if not cancel.is_set():
                updated = store.merge_endpoints(sid, result['endpoints'])
                result['credentials'] = sessions.credential_state(owner, updated)
            return result
        return submit('snapshot:' + sid, collect,
                      credential_source=lambda owner: sessions.credentials(owner, sid))

    @app.post('/api/details')
    def details():
        data = body()
        service, ep = target(data)
        topic = data.get('topic')
        return submit('detail:' + service['id'] + ':' + ep.id + ':' + str(topic),
                      lambda owner, pw, cancel: telemetry.detail(ep, topic, pw, owner, cancel),
                      credential_source=lambda owner: sessions.credentials(owner, service['id']))

    @app.get('/api/snippets/<sid>/<kind>')
    def command_snippets(sid, kind):
        if kind not in ('redis', 'sentinel'):
            raise FarmError('Unsupported console kind.', 'invalid_kind')
        return jsonify(snippets=snippets(kind, store.get(sid), settings.redis_admin_user))

    @app.get('/api/terminals')
    def terminal_list():
        with terminals.lock:
            items = [t for t in terminals.items.values() if t.owner == g.browser.id]
        descriptors = []
        for terminal in items:
            data = terminal.poll(2**63)
            data.pop('chunks', None)
            data.pop('truncated', None)
            descriptors.append(data)
        return jsonify(terminals=descriptors)

    @app.post('/api/terminals')
    def terminal_open():
        data = body()
        service, ep = target(data)
        owner, generation = g.browser.id, g.browser.generation
        credentials = sessions.credentials(owner, service['id'])
        terminal = terminals.open(owner, ep, password_for(credentials, ep),
                                  data.get('db', 0), data.get('cluster_redirect', False),
                                  service_id=service['id'], profile=service_auth(service))
        current = sessions.get(owner)
        if current is None or current.generation != generation:
            try:
                terminals.close(owner, terminal.id)
            except FarmError:
                pass
            raise FarmError('Session changed while opening the console.', 'session_expired', 401)
        sessions.touch(owner)
        return jsonify(terminal=terminal.poll(0)), 201

    @app.get('/api/terminals/<tid>')
    def terminal_poll(tid):
        try:
            after = max(0, int(request.args.get('after', '0')))
        except ValueError:
            raise FarmError('Invalid console cursor.', 'invalid_cursor') from None
        return jsonify(terminals.get(g.browser.id, tid).poll(after))

    @app.post('/api/terminals/<tid>/input')
    def terminal_input(tid):
        terminals.get(g.browser.id, tid).send(body().get('text'))
        sessions.touch(g.browser.id)
        return jsonify(accepted=True)

    @app.post('/api/terminals/<tid>/clear')
    def terminal_clear(tid):
        return jsonify(cursor=terminals.get(g.browser.id, tid).clear())

    @app.delete('/api/terminals/<tid>')
    def terminal_close(tid):
        terminals.close(g.browser.id, tid)
        return jsonify(ok=True)

    return app
