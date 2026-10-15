"""Translate installer-owned prompts, preserving paths and external tool output."""
import json
from pathlib import Path
import re
_DATA=json.loads(Path(__file__).with_name('locales').joinpath('installer.json').read_text(encoding='utf-8'))
_PATTERNS=[]
for source,target in _DATA.items():
    if '{0}' in source:
        parts=re.split(r'(\{\d+\})',source)
        regex=''.join('(.*?)' if re.fullmatch(r'\{\d+\}',p) else re.escape(p) for p in parts)
        _PATTERNS.append((re.compile('^'+regex+'$',re.S),target))
def text(value, language):
    if not isinstance(value,str) or language!='ru':return value
    prefix='\n' if value.startswith('\n') else ''
    value=value.lstrip('\n')
    if value in _DATA:return prefix+_DATA[value]
    for pattern,target in _PATTERNS:
        match=pattern.fullmatch(value)
        if match:
            return prefix+re.sub(r'\{(\d+)\}',lambda m:match.group(int(m[1])+1),target)
    return prefix+value

def localize_callable(fn, language=None):
    def wrapped(*args,**kwargs):
        from . import install_wizard
        lang=language or install_wizard.LANGUAGE
        return fn(*(text(v,lang) for v in args),**kwargs)
    return wrapped
