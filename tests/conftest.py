from __future__ import annotations

import threading
import time

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from mock_target_app.app import app as flask_app

PORT = 5099
BASE_URL = f"http://127.0.0.1:{PORT}"


class _ServerThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = make_server("127.0.0.1", PORT, flask_app)

    def run(self):
        self.server.serve_forever()

    def stop(self):
        self.server.shutdown()


@pytest.fixture(scope="session")
def mock_server():
    t = _ServerThread()
    t.start()
    time.sleep(0.3)
    yield BASE_URL
    t.stop()


@pytest.fixture()
def page(mock_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg
        browser.close()
