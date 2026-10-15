"""Panel/installer localization. No process-global request language or raw Redis rewriting."""
from functools import lru_cache
import json
import re
from pathlib import Path

@lru_cache(maxsize=1)
def dictionaries():
    root = Path(__file__).with_name('locales')
    return (json.loads((root / 'messages.json').read_text(encoding='utf-8')),
            json.loads((root / 'errors.json').read_text(encoding='utf-8')))

class Translator:
    def __init__(self, language='en', admin_user='admin'):
        if language not in ('en', 'ru'):
            raise ValueError('language must be en or ru')
        self.language, self.admin_user = language, admin_user
        messages, errors = dictionaries()
        self.catalog = {k:v[language] for k,v in messages.items()}
        self.errors = {k:v[language] for k,v in errors.items()}

    def __call__(self, source, *args):
        if not isinstance(source, str):
            return source
        text = self.catalog.get(source, source)
        # Applied only to catalogued UI phrases, never user data, a DN or a command.
        if source in self.catalog:
            text = re.sub(r'(?<![A-Za-z0-9_])admin(?![A-Za-z0-9_])', lambda m:self.admin_user, text)
        return re.sub(r'\{(\d+)\}', lambda m:str(args[int(m[1])]) if int(m[1]) < len(args) else m[0], text)

    def error(self, text, code=''):
        if not text:
            return text
        # Raw Redis and resolver diagnostics remain exact for troubleshooting.
        if code in ('redis_error', 'parse_error', 'dns_error', 'dns_timeout', 'dns_busy', 'cli_error'):
            return text
        if text in self.catalog:
            return self(text)
        return self.errors.get(code, self(text)) if self.language == 'ru' else self(text)

    def web(self):
        return {'language': self.language, 'messages': self.catalog,
                'errors': self.errors, 'admin_user': self.admin_user}

def localize_errors(value, t):
    if isinstance(value, list):
        return [localize_errors(v,t) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, val in value.items():
        # These are raw Redis data containers, not translatable panel structures.
        if key in ('info', 'output', 'value', 'chunks', 'rows', 'configuration', 'clients'):
            result[key] = val
        elif key == 'error' and isinstance(val,str):
            result[key] = t.error(val, value.get('error_code', value.get('code','')))
        else:
            result[key] = localize_errors(val,t)
    return result
