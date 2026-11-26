"""Never let extraction diagnostics from tests touch a user's data home."""
import os
import tempfile

_home = tempfile.TemporaryDirectory(prefix='skillmind-tests-')
os.environ['SKILLMIND_HOME'] = _home.name
