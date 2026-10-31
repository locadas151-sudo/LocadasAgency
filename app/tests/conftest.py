import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="locadas-test-")
os.environ.update({
    "DATA_DIR": _tmp,
    "SCHEDULER_ENABLED": "0",
    "SECRET_KEY": "test-secret",
    "BASE_URL": "http://testserver",
    "ANTHROPIC_API_KEY": "",
    "TZ": "Europe/Berlin",
    "ALLOW_INSECURE_DEV": "1",
})

import pytest  # noqa: E402

from locadas import db as dbmod  # noqa: E402
from tests.fakemail import FakeImapServer, FakeSmtpServer  # noqa: E402

USER = "info@mueller-shk.de"
PASSWORD = "geheim123"


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from locadas.web import ratelimit

    ratelimit.reset()
    yield


@pytest.fixture()
def fresh_db(tmp_path):
    dbmod.reset_engine_for_tests(f"sqlite:///{tmp_path / 'test.db'}")
    yield
    dbmod.reset_engine_for_tests(f"sqlite:///{tmp_path / 'test2.db'}")


@pytest.fixture()
def imap():
    srv = FakeImapServer(USER, PASSWORD).start()
    yield srv
    srv.stop()


@pytest.fixture()
def smtp():
    srv = FakeSmtpServer().start()
    yield srv
    srv.stop()


def click(client, path: str):
    """Link aus einer Mail öffnen: GET zeigt nur die Bestätigungsseite (ändert nichts), der Knopf schickt POST."""
    r = client.get(path)
    assert r.status_code == 200, r.text
    if 'method="post"' not in r.text:
        return r  # schon erledigt o. Ä.: keine Bestätigung nötig
    return client.post(path)
