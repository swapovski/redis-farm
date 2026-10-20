"""Render the actual UI with synthetic clusters. Never connects to Redis or LDAP.
Requires Jinja2, Playwright and local Chromium. All records are fictional.
"""
from pathlib import Path
import json,re,sys
from jinja2 import Environment,FileSystemLoader,select_autoescape
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from redis_farm import __version__
from redis_farm.models import Endpoint

def ep(host,port=6379,kind='redis'):
    return Endpoint(host,port,kind).to_dict()
rows=[]
for i in range(1,4):
    ip=f'192.0.2.{10+i}';host=f'redis-cache-0{i}.example.net'
    for kind,port,role in [('redis',6379,'master' if i==1 else 'replica'),('sentinel',26379,'sentinel')]:
        e=ep(ip,port,kind)
        info=dict(redis_version='7.2.0',uptime_in_seconds=864000,config_file=f'/etc/redis/instances/redis-{port}.conf',
          role=role,connected_slaves=2 if i==1 else 0,master_host='' if i==1 else '192.0.2.11',master_port=6379,
          master_link_status='' if i==1 else 'up',master_repl_offset=184026784,slave_repl_offset=184026784,
          master_sync_in_progress=0,used_memory=1342177280,used_memory_rss=1572864000,maxmemory=4294967296,
          maxmemory_policy='allkeys-lru',instantaneous_ops_per_sec=1840 if i==1 else 560,
          connected_clients=84 if i==1 else 12,blocked_clients=0,rdb_last_bgsave_status='ok',aof_enabled=1,aof_last_write_status='ok')
        rows.append(dict(endpoint=e,endpoint_options=[e],endpoint_ids=[e['id']],reachable=True,role=role,info=info,
          connection=dict(address=e['address'],connect_host=ip),error='',sample_ms=4+i,
          display=dict(host=host,host_key=ip,ip=ip,address=f'{host}:{port}',note='',mode='hostname',source='manual'),
          acl=dict(state='available',users=['admin','monitoring','replica_user','sentinel_user','app_cache'])))
summary=dict(redis_up=3,redis_total=3,sentinel_up=3,sentinel_total=3,masters=['192.0.2.11:6379'],clients=108,
  memory_all=4026531840,memory_masters=1342177280,ops_all=2960,replication_down=[],sentinel_agree=True,
  sentinel_views=['192.0.2.11:6379']*3)
main=dict(id='demo-cache',name='Application cache',group='demo-production',mode='sentinel',master_name='app-cache',
  endpoints=[r['endpoint'] for r in rows],seed=rows[0]['endpoint'],auth_mode='admin',last_summary=summary)
services=[main]
for sid,name,group,port,mode,hosts in [
 ('demo-session','User sessions','demo-production',6380,'sentinel',['192.0.2.11','192.0.2.12','192.0.2.13']),
 ('demo-queue','Background jobs','demo-production',6381,'sentinel',['192.0.2.11','192.0.2.12','192.0.2.13']),
 ('demo-shards','Analytics','demo-analytics',7000,'cluster',['198.51.100.21','198.51.100.22','198.51.100.23']),
 ('demo-stage','Integration cache','demo-staging',6379,'standalone',['203.0.113.31'])]:
    e=[ep(h,port) for h in hosts]
    if mode=='sentinel':e += [ep(h,int('2'+str(port)),'sentinel') for h in hosts]
    services.append(dict(id=sid,name=name,group=group,mode=mode,auth_mode='admin',endpoints=e,
      seed=e[0],last_summary=dict(redis_total=len(hosts),redis_up=len(hosts),sentinel_total=len(hosts) if mode=='sentinel' else 0,
        sentinel_up=len(hosts) if mode=='sentinel' else 0)))
snapshot=dict(service_id=main['id'],nodes=rows,summary=summary,endpoints=main['endpoints'],sampled_at=1784548800,events=[])
terminal=dict(id='demo-terminal',alive=True,service_id=main['id'],endpoint=rows[0]['endpoint'],initial_db=0,
  buffer='192.0.2.11:6379> INFO replication\n# Replication\nrole:master\nconnected_slaves:2\nslave0:ip=192.0.2.12,port=6379,state=online,offset=184026784,lag=0\nslave1:ip=192.0.2.13,port=6379,state=online,offset=184026784,lag=0\nmaster_repl_offset:184026784\n\n192.0.2.11:6379> ')
fixture=dict(services=services,snapshot=snapshot,terminal=terminal)
(ROOT/'docs/demo-data.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')
# Add a password-only service with independent credentials for the access dialog.
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse,parse_qs
import mimetypes,threading,time,base64,http.client,os
from redis_farm.i18n import Translator
from redis_farm.commands import snippets

default_ep=Endpoint('203.0.113.41',6379,'redis','default').to_dict()
default_service=dict(id='demo-default',name='Team cache',group='demo-team',mode='standalone',
    auth_mode='default',endpoints=[default_ep],seed=default_ep,credentials={'unlocked_endpoints':[],'service_password_set':False})
services.append(default_service)
fixture['services']=services
(ROOT/'docs').mkdir(exist_ok=True)
(ROOT/'docs/demo-data.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')

class DemoHandler(BaseHTTPRequestHandler):
    """Serve templates/static assets plus explicitly fake API replies on loopback."""
    def log_message(self,*args):pass
    def reply(self,payload,status=200,content_type='application/json'):
        data=json.dumps(payload,ensure_ascii=False).encode() if content_type=='application/json' else payload
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers();self.wfile.write(data)
    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
    def do_GET(self):
        parsed=urlparse(self.path);path=parsed.path
        if path in ('/','/login'):
            lang=parse_qs(parsed.query).get('lang',['en'])[0];t=Translator(lang,'ops-admin')
            self.server.language=lang
            env=Environment(loader=FileSystemLoader(ROOT/'redis_farm/templates'),autoescape=select_autoescape())
            html=env.get_template('login.html' if path=='/login' else 'index.html').render(
                version=__version__,server_mode=True,panel_login=True,panel_user='demo.operator',brand_logo=True,add_logo=True,
                t=t,language=lang,admin_user='ops-admin',use_acl_user=True,ui_catalog=t.web(),csrf='demo-csrf',username='',error='',
                url_for=lambda endpoint,filename=None,**kw:('/static/'+filename if filename else '/'+endpoint)+('?'+'&'.join(f'{k}={v}' for k,v in kw.items()) if kw else ''))
            return self.reply(html.encode(),content_type='text/html; charset=utf-8')
        if path.startswith('/static/'):
            item=(ROOT/'redis_farm/static'/path[len('/static/'):]).resolve()
            if not item.is_relative_to((ROOT/'redis_farm/static').resolve()) or not item.is_file():return self.reply({'error':'Not found'},404)
            return self.reply(item.read_bytes(),content_type=mimetypes.guess_type(str(item))[0] or 'application/octet-stream')
        if path=='/api/bootstrap':return self.reply(dict(version=__version__,csrf='demo-csrf',redis_unlocked=True,
            refresh_seconds=30,data_dir='/var/lib/redis-farm',panel_login=True,panel_user='demo.operator',
            language=self.server.language,admin_user='ops-admin',use_acl_user=True,widgets=[],
            discovery_defaults={'redis_start':6379,'redis_end':6478,'sentinel_start':26379,'sentinel_end':26478},
            cli={'available':True,'version':'redis-cli 7.2.0','error':'','path':'/usr/bin/redis-cli','dns':{'mode':'system','servers':[]}}))
        if path=='/api/services':return self.reply({'services':services})
        if path=='/api/terminals':return self.reply({'terminals':[]})
        if path.startswith('/api/snippets/'):
            parts=path.split('/');return self.reply({'snippets':snippets(parts[-1],main,admin_user='ops-admin')})
        if path.endswith('/topology'):
            sid=path.split('/')[-2]
            view=deepcopy(snapshot)
            if sid=='demo-default':view={'service_id':sid,'nodes':[{'endpoint':default_ep,'endpoint_ids':[default_ep['id']],'endpoint_options':[default_ep],'reachable':None,'role':'unknown','display':{'host':default_ep['host'],'ip':default_ep['host']}}]}
            return self.reply(view)
        if path.startswith('/api/jobs/'):return self.reply({'state':'done','result':self.server.jobs.get(path.split('/')[-1],{})})
        if path=='/login/state':return self.reply({'csrf':'demo-csrf'})
        if path=='/api/catalog/export':return self.reply({'services':services})
        return self.reply({'error':'Not found in synthetic demo','code':'not_found'},404)
    def do_POST(self):
        path=urlparse(self.path).path;data=self.body()
        if path in ('/api/session/touch','/api/session/unlock'):return self.reply({'redis_unlocked':True})
        if path.endswith('/snapshot'):
            sid=path.split('/')[-2];result=deepcopy(snapshot)
            if sid=='demo-default':result={'service_id':sid,'summary':dict(summary,redis_total=1,redis_up=0,sentinel_total=0,sentinel_up=0,masters=[]), 'sampled_at':1784548800,'endpoints':[default_ep], 'nodes':[{'endpoint':default_ep,'endpoint_ids':[default_ep['id']],'endpoint_options':[default_ep], 'reachable':False,'role':'unknown','info':{},'error':'Enter the default password for this service or instance.','error_code':'default_password_required','display':{'host':default_ep['host'],'ip':default_ep['host']}}]}
        elif path=='/api/details':
            topic=data.get('topic','acl')
            payload={'file_state':'external','aclfile':'/etc/redis/instances/acl-6379.conf','rows':[
                {'name':'ops-admin','enabled':True,'category':'system','rules':'~* &* +@all'},
                {'name':'metrics','enabled':True,'category':'system','rules':'-@all +ping +info +role'},
                {'name':'cache_app','enabled':True,'category':'application','rules':'~cache:* +@read +@write'}]}
            if topic=='clients':payload={'total':6,'limited':False,'rows':[{'id':str(i),'addr':f'198.51.100.{40+i}:43{i:03d}','user':'cache_app','name':f'api-worker-{i}','db':'0','idle':str(i%3),'cmd':'get','flags':'N'} for i in range(1,7)]}
            if topic=='config':payload={'values':{'maxmemory':'4294967296','maxmemory-policy':'allkeys-lru'},'errors':{}}
            result={'topic':topic,'endpoint':rows[0]['endpoint'],'sampled_at':1784548800,'payload':payload}
        else:return self.reply({'error':'Not implemented in synthetic demo','code':'not_found'},404)
        key=str(len(self.server.jobs)+1);self.server.jobs[key]=result;return self.reply({'job_id':key},202)

def render_and_verify():
    out=ROOT/'docs/images';out.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),DemoHandler);server.jobs={};server.language='en'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    tests=[];errors=[]
    def check(label,assertion):
        if not assertion:raise AssertionError(label)
        tests.append(label)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            for language in ('en','ru'):
                page=browser.new_page(viewport={'width':1600,'height':1120},device_scale_factor=1)
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('console',lambda msg: errors.append(msg.text) if msg.type=='error' else None)
                # Chromium's managed navigation policy blocks local URLs here. Render
                # the exact template/assets in memory; only API results are synthetic.
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                conn.request('GET','/?lang='+language);reply=conn.getresponse();html=reply.read().decode();conn.close()
                scripts=re.findall(r'<script[^>]*src="([^"]+)"[^>]*>',html)
                styles=re.findall(r'<link[^>]*href="([^"]+\.css[^"]*)"[^>]*>',html)
                html=re.sub(r'<script[^>]*src=[^>]*>\s*</script>|<link[^>]*>','',html)
                def inline_image(m):
                    name=urlparse(m[1]).path.rsplit('/',1)[-1];asset=ROOT/'redis_farm/static'/name
                    return 'src="data:'+mimetypes.guess_type(name)[0]+';base64,'+base64.b64encode(asset.read_bytes()).decode()+'"'
                html=re.sub(r'src="(/static/[^"]+)"',inline_image,html)
                page.set_content(html)
                for name in styles:page.add_style_tag(content=(ROOT/'redis_farm/static'/urlparse(name).path.rsplit('/',1)[-1]).read_text())
                def fake_fetch(path,options):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                    body=options.get('body');headers={'Content-Type':'application/json'} if body else {}
                    conn.request(options.get('method','GET'),path,body=body,headers=headers)
                    result=conn.getresponse();value={'status':result.status,'text':result.read().decode()};conn.close();return value
                page.expose_function('demoFetch',fake_fetch)
                page.evaluate("window.fetch=async (path,options={})=>{const r=await window.demoFetch(path,options);return new Response(r.text,{status:r.status,headers:{'Content-Type':'application/json'}});}")
                for name in ['i18n.js']+[urlparse(x).path.rsplit('/',1)[-1] for x in scripts if not x.split('?')[0].endswith('i18n.js')]:
                    page.add_script_tag(content=(ROOT/'redis_farm/static'/name).read_text())
                page.evaluate("document.dispatchEvent(new Event('DOMContentLoaded'))")
                page.wait_for_selector('#topology .host-card')
                page.wait_for_function("state.snapshot !== null && !document.getElementById('detailRefresh').disabled")
                page.wait_for_timeout(150)
                check(language+' catalog actions share a row',page.locator('#toggleAllGroups').evaluate('(e)=>e.parentElement.contains(document.getElementById("editButton"))'))
                check(language+' no JS errors',not errors)
                check(language+' three nodes',page.locator('#topology .host-card').count()==3)
                check(language+' logo original',page.locator('.brand-logo').evaluate('(e)=>e.naturalWidth===159 && e.naturalHeight===287'))
                check(language+' named user label',page.locator('#authTabs button[data-auth=admin]').inner_text()=='ops-admin')
                check(language+' groups start collapsed',page.locator('.catalog-group-toggle[aria-expanded=true]').count()==0)
                page.locator('#toggleAllGroups').click()
                check(language+' expand all',page.locator('.catalog-group-toggle[aria-expanded=true]').count()==3)
                page.locator('#toggleAllGroups').click()
                check(language+' collapse all',page.locator('.catalog-group-toggle[aria-expanded=true]').count()==0)
                page.locator('.catalog-group-toggle').first.click()
                check(language+' edit in sidebar',page.locator('.sidebar #editButton').count()==1)
                # Use the genuine console renderer, with a synthetic transcript (no process).
                page.evaluate('f=>{state.terminals.redis={...f.terminal,id:null,cursor:0};renderConsole();}',fixture)
                page.wait_for_timeout(100)
                text=page.inner_text('body')
                for banned in ('company.invalid','private.internal','employee-secret','production-customer-secret'):
                    check(language+' anonymous '+banned,banned not in text)
                if language=='en':check('English main screen has no Cyrillic',not re.search('[\u0400-\u04ff]',text))
                page.screenshot(path=str(out/f'dashboard-{language}.png'),full_page=True)
                login_page=browser.new_page(viewport={'width':1280,'height':800},device_scale_factor=1)
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                conn.request('GET','/login?lang='+language);lr=conn.getresponse();login_html=lr.read().decode();conn.close()
                login_html=re.sub(r'<script[^>]*src=[^>]*>\s*</script>|<link[^>]*>','',login_html)
                login_page.set_content(login_html)
                for css_name in ('app.css','login.css'):
                    login_page.add_style_tag(content=(ROOT/'redis_farm/static'/css_name).read_text())
                check(language+' login label',login_page.locator('#username').count()==1 and login_page.locator('#password').count()==1)
                login_page.screenshot(path=str(out/f'login-{language}.png'),full_page=True)
                login_page.close()
                page.locator('#detailTabs button[data-topic=clients]').click()
                page.wait_for_selector('#detailContent tbody tr')
                page.wait_for_function("state.topic === 'clients' && document.getElementById('detailContent').textContent.includes('api-worker-1')")
                page.locator('.details-panel').scroll_into_view_if_needed()
                page.locator('.details-panel').screenshot(path=str(out/f'clients-{language}.png'))
                page.evaluate('window.scrollTo(0,0)')
                page.locator('#addButton').click()
                page.wait_for_selector('#addDialog[open]')
                check(language+' add original',page.locator('.dialog-logo').evaluate('(e)=>e.naturalWidth===195 && e.naturalHeight===208'))
                check(language+' add initial host cleared',page.input_value('#seedHost')=='')
                page.locator('#seedHost').fill('redis-cache-01.example.net')
                page.locator('#seedGroup').fill('demo-production')
                page.screenshot(path=str(out/f'add-service-{language}.png'))
                page.locator('#closeAdd').click()
                page.locator('#authTabs button[data-auth=default]').click()
                page.wait_for_timeout(100)
                check(language+' default tab no automatic modal',not page.locator('#defaultAccessDialog').evaluate('(e)=>e.open'))
                page.locator('#toggleAllGroups').click()
                page.locator('#catalog .catalog-item').first.click()
                page.wait_for_selector('#defaultAccessDialog[open]')
                check(language+' default form source in own language','Enter the default password' not in page.locator('#defaultAccessDialog').inner_text() if language=='ru' else True)
                check(language+' access has separate kind selector',page.locator('#accessKind').input_value()=='redis')
                check(language+' password field not prefilled',page.input_value('#defaultAccessPassword')=='')
                check(language+' own password optional when changing settings',not page.locator('#defaultAccessPassword').evaluate('(e)=>e.required'))
                page.locator('#defaultAccessScope').focus();page.wait_for_timeout(100)
                page.screenshot(path=str(out/f'default-access-{language}.png'))
                page.locator('#closeDefaultAccess').click()
                page.locator('#authTabs button[data-auth=admin]').click()
                page.evaluate("showAccess('sentinel')")
                check(language+' Sentinel defaults to no auth',page.input_value('#accessAuth')=='none')
                check(language+' Sentinel has no global Redis auth option',page.locator('#accessAuth option[value=admin]').count()==0)
                page.locator('#accessAuth').select_option('user')
                page.locator('#accessUsername').fill('sentinel-operator')
                check(language+' Sentinel username visible',page.locator('#accessUsernameRow').is_visible())
                check(language+' Sentinel password empty',page.input_value('#defaultAccessPassword')=='')
                page.locator('#defaultAccessDialog .tls-settings summary').click()
                page.locator('#accessTls').check()
                page.locator('#accessCa').fill('/etc/redis-farm/ca/sentinel.pem')
                page.locator('#accessSni').fill('sentinel.example.net')
                check(language+' TLS checkbox stays compact',page.locator('#accessTls').evaluate('(e)=>e.getBoundingClientRect().width===16'))
                check(language+' expanded TLS no horizontal overflow',page.locator('#defaultAccessDialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'))
                page.screenshot(path=str(out/f'sentinel-access-{language}.png'))
                page.locator('#accessKind').select_option('redis')
                check(language+' Redis retains shared profile',page.input_value('#accessAuth')=='admin')
                check(language+' Redis TLS independent',not page.is_checked('#accessTls'))
                page.locator('#accessAuth').select_option('user')
                page.locator('#accessUsername').fill('cache-operator')
                page.screenshot(path=str(out/f'redis-access-{language}.png'))
                page.locator('#closeDefaultAccess').click()
                page.locator('#aboutButton').click()
                page.locator('[data-installation-reveal] summary').click();page.wait_for_timeout(700)
                check(language+' setup full size',page.locator('.installation-image').evaluate('(e)=>e.clientWidth===212 && e.clientHeight===180'))
                check(language+' reveal scrolled',page.locator('#aboutDialog').evaluate('(e)=>e.scrollTop>0'))
                page.screenshot(path=str(out/f'about-{language}.png'))
                page.mouse.click(10,10);check(language+' outside closes about',not page.locator('#aboutDialog').evaluate('(e)=>e.open'))
                # Leaf-grip pickup: exact anchor, source restoration, all three illustrations.
                b=page.locator('.brand-logo').bounding_box();x=b['x']+b['width']/2;y=b['y']+b['height']/2
                page.mouse.move(x,y);page.mouse.down();page.mouse.move(540,350,steps=5);page.wait_for_timeout(150)
                check(language+' held original hidden',page.locator('.brand-logo').evaluate('(e)=>getComputedStyle(e).visibility==="hidden" || getComputedStyle(e).opacity==="0"'))
                check(language+' held preview visible',page.locator('.image-hold-preview').is_visible())
                page.mouse.up();check(language+' original restored',page.locator('.brand-logo').evaluate('(e)=>getComputedStyle(e).visibility!=="hidden" && getComputedStyle(e).opacity!=="0"'))
                def grip(selector,label):
                    loc=page.locator(selector);loc.scroll_into_view_if_needed();b=loc.bounding_box()
                    x=b['x']+b['width']/2;y=b['y']+b['height']/2
                    page.mouse.move(x,y);page.wait_for_timeout(60)
                    check(language+' '+label+' hover has no preview',page.locator('.image-hold-preview').count()==0)
                    page.mouse.down();page.mouse.move(580,420,steps=6);page.wait_for_timeout(260)
                    check(language+' '+label+' follows outside source',page.locator('.image-hold-preview').is_visible())
                    anchor=loc.evaluate("e=>{const t=e.closest('[data-image-hold]');return {x:e.naturalWidth*Number(t.dataset.leafX),y:Math.max(0,e.naturalHeight*Number(t.dataset.leafY)-19),w:e.naturalWidth,h:e.naturalHeight}}")
                    geom=page.locator('.image-hold-preview').evaluate("e=>({x:parseFloat(e.style.left),y:parseFloat(e.style.top),w:parseFloat(e.style.width),h:parseFloat(e.style.height)})")
                    check(language+' '+label+' cursor at leaf base',abs(geom['x']+anchor['x']-580)<.1 and abs(geom['y']+anchor['y']-420)<.1)
                    check(language+' '+label+' natural size',geom['w']==anchor['w'] and geom['h']==anchor['h'])
                    transform=page.locator('.image-hold-preview img').evaluate('e=>e.style.transform')
                    check(language+' '+label+' swing retained','rotateZ' in transform and '0.000deg) rotateY(0.000deg) rotateZ(0.000deg)' not in transform)
                    page.mouse.up()
                    check(language+' '+label+' restored',loc.evaluate('(e)=>getComputedStyle(e).visibility!=="hidden"') and page.locator('.image-hold-preview').count()==0)
                grip('.brand-logo','logo')
                page.locator('#addButton').click();grip('.dialog-logo','add')
                page.locator('#sentinelConnectionSettings summary').first.click()
                page.locator('#seedSentinelAuth').select_option('user')
                page.locator('#seedSentinelUsername').fill('sentinel-operator')
                check(language+' add Sentinel password separate',page.locator('#seedSentinelPassword').is_visible() and page.input_value('#seedSentinelPassword')=='')
                page.locator('#sentinelConnectionSettings').screenshot(path=str(out/f'sentinel-add-{language}.png'))
                page.locator('#closeAdd').click()
                page.locator('#aboutButton').click();page.wait_for_timeout(100)
                if not page.locator('[data-installation-reveal]').evaluate('(e)=>e.open'):page.locator('[data-installation-reveal] summary').click()
                page.wait_for_timeout(600);grip('.installation-image','setup');page.locator('#closeAbout').click()
                page.set_viewport_size({'width':390,'height':844})
                page.wait_for_timeout(200)
                check(language+' mobile no page overflow',page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'))
                page.locator('#addButton').click()
                page.wait_for_selector('#addDialog[open]')
                check(language+' mobile dialog fits',page.locator('#addDialog').evaluate('(e)=>e.getBoundingClientRect().width <= window.innerWidth && e.scrollWidth <= e.clientWidth + 1'))
                page.locator('#sentinelConnectionSettings').evaluate('(e)=>e.open=true')
                page.locator('#sentinelConnectionSettings .tls-settings').first.evaluate('(e)=>e.open=true')
                check(language+' mobile Sentinel TLS fits',page.locator('#addDialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'))
                page.screenshot(path=str(out/f'add-mobile-{language}.png'))
                page.locator('#closeAdd').click()
                page.close()
            browser.close()
        if errors:raise AssertionError(errors)
        report={'checks':len(tests),'passed':tests,'errors':errors,'data':'synthetic only',
                'method':'Actual Jinja templates, original images, CSS and JS rendered in memory; fake API through exposed binding, real Chromium. No live HTTP/CSP, LDAP or Redis.'}
        (ROOT/'docs/browser-checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'checks':len(tests),'output':str(out),'errors':errors}))
    finally:server.shutdown();server.server_close()

if __name__=='__main__':render_and_verify()
