"""Authenticate through a protocol double; not a live ldap3 or FreeIPA test."""
import ssl
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from redis_farm.ldap_auth import LDAPAuthenticator, LoginDenied, LoginUnavailable, probe_transport
from redis_farm.ldap_config import parse_ldap_config, WIZARD_DEFAULTS
from redis_farm.panel_auth import LoginLimiter

class AuthTests(unittest.TestCase):
    def setUp(self):
        self.cfg=parse_ldap_config(dict(WIZARD_DEFAULTS,CA_CERT_FILE=None))
        self.events=[];self.created=[];self.bind_ok=True;self.member=True;self.tls_ok=True;self.bind_code=49
        self.error=None
        test=self
        class LDAPException(Exception): pass
        class Connection:
            def __init__(self,server,**kw):
                test.created.append(self);self.kw=kw;self.password=kw.get('password');self.user=kw.get('user');self.closed=True
                self.result={};self.response=[]
            def open(self): test.events.append('open');self.closed=False
            def start_tls(self):
                test.events.append('starttls')
                if test.error: raise test.error
                return test.tls_ok
            def bind(self):
                test.events.append('bind');self.result={'result':0 if test.bind_ok else test.bind_code}
                return test.bind_ok
            def search(self,base,filter,**kw):
                test.events.append(('search',base,filter,kw));self.result={'result':0}
                self.response=[{'type':'searchResEntry'}] if test.member else []
                return test.member
            def unbind(self): test.events.append('unbind');self.closed=True
        lib=ModuleType('ldap3'); lib.Connection=Connection
        lib.Server=lambda host,**kw:SimpleNamespace(host=host,**kw)
        lib.Tls=lambda **kw:SimpleNamespace(**kw)
        lib.SIMPLE='SIMPLE';lib.BASE='BASE';lib.NONE='NONE';lib.SUBTREE='SUBTREE';lib.DEREF_NEVER='NEVER'
        lib.core=SimpleNamespace(exceptions=SimpleNamespace(LDAPException=LDAPException))
        self.lib=lib;self.LDAPException=LDAPException
        dn=ModuleType('ldap3.utils.dn');dn.escape_rdn=lambda s:s.replace(',','\\,');dn.parse_dn=lambda s:[(part.split('=',1)[0],part.split('=',1)[1],',' if i<len(s.split(','))-1 else '') for i,part in enumerate(s.split(','))]
        conv=ModuleType('ldap3.utils.conv');conv.escape_filter_chars=lambda s:s.replace('(','\\28').replace(')','\\29').replace('*','\\2a')
        exceptions=ModuleType('ldap3.core.exceptions');exceptions.LDAPException=LDAPException
        self.patcher=patch.dict(sys.modules,{'ldap3':lib,'ldap3.utils.dn':dn,'ldap3.utils.conv':conv,'ldap3.core.exceptions':exceptions})
        self.patcher.start()
    def tearDown(self): self.patcher.stop()
    def auth(self): return LDAPAuthenticator(self.cfg)
    def test_starttls_before_password_bind(self):
        result=self.auth().authenticate('tester','secret')
        self.assertEqual(result.username,'tester')
        self.assertLess(self.events.index('starttls'),self.events.index('bind'))
        self.assertEqual(self.events[-1],'unbind')
        self.assertIsNone(self.created[0].password)
    def test_failed_tls_never_binds(self):
        self.tls_ok=False
        with self.assertRaises(LoginUnavailable):self.auth().authenticate('tester','secret')
        self.assertNotIn('bind',self.events)
    def test_tls_exception_never_binds(self):
        self.error=self.LDAPException('TLS failure fixture')
        with self.assertRaises(LoginUnavailable) as e:self.auth().authenticate('tester','secret')
        self.assertNotIn('secret',str(e.exception));self.assertNotIn('bind',self.events)
    def test_wrong_password(self):
        self.bind_ok=False
        with self.assertRaises(LoginDenied):self.auth().authenticate('tester','bad')
        self.assertFalse(any(isinstance(x,tuple) for x in self.events))
    def test_missing_membership(self):
        self.member=False
        with self.assertRaises(LoginDenied):self.auth().authenticate('tester','secret')
    def test_exact_group_and_no_referrals(self):
        self.auth().authenticate('tester','secret')
        search=next(e for e in self.events if isinstance(e,tuple))
        self.assertEqual(search[1],self.cfg.require_group);self.assertEqual(search[3]['search_scope'],'BASE')
        self.assertFalse(self.created[0].kw['auto_referrals']);self.assertTrue(self.created[0].kw['read_only'])
    def test_filter_escaped(self):
        self.auth().authenticate('test(*)','secret')
        query=next(e for e in self.events if isinstance(e,tuple))[2]
        self.assertIn('test\\28\\2a\\29',query)
    def test_empty_password_no_anonymous_bind(self):
        with self.assertRaises(LoginDenied):self.auth().authenticate('tester','')
        self.assertFalse(self.events)
    def test_bad_username_no_connection(self):
        for username in ('', ' tester', 'x\n', 'x'*129):
            with self.assertRaises(LoginDenied):self.auth().authenticate(username,'secret')
        self.assertFalse(self.events)
    def test_connection_error_does_not_expose_password(self):
        self.error=ssl.SSLError('secret must not appear')
        with self.assertRaises(LoginUnavailable) as e:self.auth().authenticate('tester','secret')
        self.assertEqual(str(e.exception),'')
    def test_transport_probe_never_binds(self):
        probe_transport(self.cfg)
        self.assertNotIn('bind',self.events);self.assertIsNone(self.created[0].kw.get('password'))
    def test_failed_probe_no_bind(self):
        self.tls_ok=False
        with self.assertRaises(RuntimeError):probe_transport(self.cfg)
        self.assertNotIn('bind',self.events)
    def test_ldaps_skips_starttls(self):
        self.cfg=parse_ldap_config(dict(WIZARD_DEFAULTS,URL='ldaps://ldap.test:636',CA_CERT_FILE=None))
        self.auth().authenticate('tester','secret')
        self.assertNotIn('starttls',self.events)
    def test_required_tls_settings(self):
        old=self.lib.Tls;seen=[]
        self.lib.Tls=lambda **kw:seen.append(kw) or old(**kw)
        self.auth().authenticate('tester','secret')
        self.assertEqual(seen[0]['validate'],ssl.CERT_REQUIRED)
        self.assertEqual(seen[0]['sni'],self.cfg.host)
        self.assertNotIn('valid_names',seen[0])

class LimiterTests(unittest.TestCase):
    def test_user_limit_and_expiry(self):
        now=[0];limit=LoginLimiter(clock=lambda:now[0])
        self.assertTrue(all(limit.allow('10.1.1.1','user') for _ in range(8)))
        self.assertFalse(limit.allow('10.1.1.2','USER'));now[0]=61
        self.assertTrue(limit.allow('10.1.1.1','user'))
    def test_no_plain_identity_keys(self):
        limit=LoginLimiter();limit.allow('10.1.1.1','fixture')
        self.assertTrue(all(isinstance(k,bytes) and len(k)==32 for k in limit.items))

if __name__=='__main__':unittest.main()
