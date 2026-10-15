"""Portable first-install settings. No LDAP import, network or password persistence."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

TEXT = json.loads(Path(__file__).with_name('installer_text.json').read_text(encoding='utf-8'))
LANGUAGE = 'en'
def tr(key): return TEXT[key][LANGUAGE]
def set_language(language):
    global LANGUAGE
    if language not in ('en','ru'): raise ValueError('language must be en or ru')
    LANGUAGE = language
    return language

def choose_language(args, *, existing=None, interactive=False, ask=input):
    lang = getattr(args,'language',None)
    if lang is None and existing is not None: lang=existing.get('language','ru')
    if lang is None: lang=prompt('Language / \u042f\u0437\u044b\u043a (en/ru)', 'en', ask=ask) if interactive else 'en'
    return set_language(lang)

def prompt(label, default='', *, required=False, ask=input):
    while True:
        try: value = ask(label + (f' [{default}]' if default else '') + ': ').strip()
        except EOFError: raise ValueError(tr('interrupted')) from None
        value = value or default
        if value or not required: return value

def parse_dns(value):
    tokens=re.split(r'[,;\s]+',value.strip()) if value.strip() else []
    if len(tokens)>8: raise ValueError('Use at most eight DNS IPs.')
    result=[]
    for text in tokens:
        ip=ipaddress.ip_address(text)
        if ip.is_unspecified or ip.is_multicast: raise ValueError('DNS needs a unicast IP.')
        if str(ip) not in result: result.append(str(ip))
    return result

def validate_admin_user(value):
    if not isinstance(value,str) or not value or len(value)>128 or value!=value.strip() or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('redis_admin_user must be 1..128 printable characters, without surrounding whitespace.')
    if value=='default': raise ValueError('Use the default tab for the default user, not redis_admin_user=default.')
    return value

def choose_dns(args, *, interactive, ask=input, say=print):
    if getattr(args,'system_dns',False): return []
    if getattr(args,'dns_server',None) is not None: return parse_dns(' '.join(args.dns_server))
    if not interactive: raise ValueError('First install needs --dns-server IP or --system-dns.')
    say('\n'+tr('dns_help')+'\n')
    while True:
        answer=prompt(tr('dns'),ask=ask)
        try: return parse_dns(answer)
        except ValueError: say(tr('retry'))

def choose_host(args, *, interactive, ask=input):
    value=getattr(args,'host',None)
    if not value:
        if not interactive: raise ValueError('First install needs --host IP.')
        value=prompt(tr('host'),required=True,ask=ask)
    ip=ipaddress.ip_address(value)
    if ip.is_unspecified or ip.is_multicast: raise ValueError('Choose a specific unicast interface IP.')
    return str(ip)

def default_cli():
    for path in ('/opt/redis-farm/bin/redis-cli',shutil.which('redis-cli'),'/usr/bin/redis-cli'):
        if path and Path(path).is_file(): return str(Path(path).absolute())
    return '/usr/bin/redis-cli'

def profile_settings(args, *, interactive, ask=input, say=print):
    enabled=getattr(args,'use_acl_user',None)
    if enabled is None:
        if interactive:
            say(tr('acl_help'))
            value=prompt(tr('acl'),'yes',ask=ask).lower()
            if value not in ('yes','no','y','n'): raise ValueError('Choose yes or no.')
            enabled=value in ('yes','y')
        else: enabled=True
    elif isinstance(enabled,str): enabled=enabled=='yes'
    name=getattr(args,'admin_user',None)
    if not name: name=prompt(tr('admin'),'admin',ask=ask) if enabled and interactive else 'admin'
    return {'use_acl_user':enabled,'redis_admin_user':validate_admin_user(name)}

def confirm_no_auth(args, *, interactive, ask=input, say=print):
    say(tr('noauth_warning'))
    if getattr(args,'allow_no_auth',False): return
    if not interactive or prompt(tr('noauth_confirm'),ask=ask)!='NO_AUTH':
        raise ValueError('Unauthenticated server mode needs NO_AUTH confirmation or --allow-no-auth.')

def first_server_config(args, template, *, interactive=None, ask=input, say=print):
    if interactive is None: interactive=sys.stdin.isatty() and not getattr(args,'non_interactive',False)
    language=choose_language(args,interactive=interactive,ask=ask)
    raw=dict(template)
    host=choose_host(args,interactive=interactive,ask=ask)
    port=getattr(args,'port',None)
    if port is None: port=int(prompt(tr('port'),'8765',ask=ask)) if interactive else 8765
    if not 1024<=port<=65535: raise ValueError('HTTPS port must be 1024..65535.')
    dns=choose_dns(args,interactive=interactive,ask=ask,say=say)
    networks=getattr(args,'allow_cidr',None)
    if networks is None:
        if not interactive: raise ValueError('First install needs --allow-cidr ADMIN_NETWORK.')
        say(tr('net_help'));networks=re.split(r'[,;\s]+',prompt(tr('networks'),required=True,ask=ask))
    networks=[str(ipaddress.ip_network(n,strict=False)) for n in networks]
    networks += [host+('/128' if ':' in host else '/32'),'127.0.0.1/32','::1/128']
    cli=getattr(args,'redis_cli',None) or (prompt(tr('cli'),default_cli(),ask=ask) if interactive else default_cli())
    if not Path(cli).is_absolute(): raise ValueError('redis-cli path must be absolute.')
    profile=profile_settings(args,interactive=interactive,ask=ask,say=say)
    auth=getattr(args,'panel_auth',None) or (prompt(tr('auth'),'ldap',ask=ask) if interactive else 'ldap')
    if auth not in ('ldap','none'): raise ValueError('panel_auth must be ldap or none.')
    if auth=='none': confirm_no_auth(args,interactive=interactive,ask=ask,say=say)
    raw.update(bind_host=host,public_host=host,port=port,allowed_hosts=[host],dns_servers=dns,
               allowed_client_cidrs=list(dict.fromkeys(networks)),cli_path=cli,
               language=language,panel_auth=auth,**profile)
    raw.pop('public_origin',None);raw.pop('trusted_proxy_ips',None)
    return raw

def load_local(path):
    path=Path(path)
    if not path.exists(): return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size>65536: raise ValueError('Invalid local.json file.')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or set(value)-{'dns_servers','cli_path','language','redis_admin_user','use_acl_user'}: raise ValueError('Unknown local.json fields.')
    if 'dns_servers' in value:
        if not isinstance(value['dns_servers'],list): raise ValueError('dns_servers must be a list.')
        value['dns_servers']=parse_dns(' '.join(value['dns_servers']))
    if 'cli_path' in value and (not isinstance(value['cli_path'],str) or not Path(value['cli_path']).is_absolute()): raise ValueError('cli_path must be absolute.')
    if value.get('language','ru') not in ('en','ru'): raise ValueError('language must be en or ru.')
    validate_admin_user(value.get('redis_admin_user','admin'))
    if type(value.get('use_acl_user',True)) is not bool: raise ValueError('use_acl_user must be true or false.')
    return value

def configure_local(args, *, ask=input, say=print):
    path=Path(args.config).absolute();interactive=sys.stdin.isatty() and not args.non_interactive
    existing=load_local(path) if path.exists() else None
    language=choose_language(args,existing=existing,interactive=interactive,ask=ask)
    if existing is not None:
        # Preserve unless explicitly requested. Legacy installations stay Russian.
        data=dict(existing)
        if getattr(args,'language',None): data['language']=args.language
        if getattr(args,'redis_cli',None): data['cli_path']=args.redis_cli
        if getattr(args,'admin_user',None): data['redis_admin_user']=validate_admin_user(args.admin_user)
        if getattr(args,'use_acl_user',None) is not None: data['use_acl_user']=args.use_acl_user=='yes'
        if args.system_dns: data['dns_servers']=[]
        elif args.dns_server is not None: data['dns_servers']=parse_dns(' '.join(args.dns_server))
        if data==existing: say(tr('preserved')+': '+str(path));return
    else:
        dns=choose_dns(args,interactive=interactive,ask=ask,say=say)
        cli=args.redis_cli or (prompt(tr('cli'),default_cli(),ask=ask) if interactive else default_cli())
        if not Path(cli).is_absolute(): raise ValueError('redis-cli path must be absolute.')
        data={'dns_servers':dns,'cli_path':cli,'language':language,**profile_settings(args,interactive=interactive,ask=ask,say=say)}
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.local-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        if existing is None: os.link(temp,path)
        else: os.replace(temp,path)
    finally: Path(temp).unlink(missing_ok=True)
    say(tr('saved')+': '+str(path))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('local.json'))
    group=p.add_mutually_exclusive_group();group.add_argument('--dns-server',action='append');group.add_argument('--system-dns',action='store_true')
    p.add_argument('--redis-cli');p.add_argument('--language',choices=['en','ru'])
    p.add_argument('--admin-user');p.add_argument('--use-acl-user',choices=['yes','no'])
    p.add_argument('--non-interactive',action='store_true')
    try: configure_local(p.parse_args())
    except (ValueError,OSError) as e: print(str(e),file=sys.stderr);return 1
    return 0
if __name__=='__main__': raise SystemExit(main())
