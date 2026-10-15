#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON=${PYTHON:-python3}
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
"$PYTHON" -m redis_farm.install_wizard "$@"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python -c 'from redis_farm.install_wizard import load_local; from redis_farm.i18n import Translator; c=load_local("local.json"); print(Translator(c.get("language","ru"))("Installation complete. Run: ./start.sh"))'
