"""Tests for chooser interception over a fake CDP websocket."""
import asyncio
import json

import cdp_file_chooser as fc


class FakeBrowser:
    """A websocket stand-in: answers commands, raises the chooser event on click."""

    def __init__(self, chooser_params=None):
        self.sent = []
        self._queue = asyncio.Queue()
        self._chooser = chooser_params

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["method"] == "Input.dispatchMouseEvent" and msg["params"]["type"] == "mouseReleased" \
                and self._chooser is not None:
            # An event arrives BEFORE the reply, as it can on the real wire.
            await self._queue.put({"method": fc.CHOOSER_EVENT, "params": self._chooser})
        await self._queue.put({"id": msg["id"], "result": {}})

    async def recv(self):
        return json.dumps(await self._queue.get())

    def methods(self):
        return [m["method"] for m in self.sent]


def _run(browser, path, timeout=1.0):
    session = fc.CdpSession(browser)

    async def go():
        return await fc.attach(session, path, timeout=timeout,
                               open_chooser=lambda: fc.press(session, {"x": 10, "y": 20}))
    return asyncio.run(go())


def test_attach_arms_clicks_writes_and_disarms(tmp_path):
    # Arrange
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4")
    browser = FakeBrowser({"backendNodeId": 77, "mode": "selectSingle"})

    # Act
    outcome = _run(browser, str(cv))

    # Assert
    assert outcome == {"ok": True, "note": "", "backend_node_id": 77}
    methods = browser.methods()
    assert methods.index(fc.INTERCEPT) < methods.index("Input.dispatchMouseEvent")
    write = next(m for m in browser.sent if m["method"] == "DOM.setFileInputFiles")
    assert write["params"] == {"files": [str(cv)], "backendNodeId": 77}
    assert browser.sent[-1]["params"] == {"enabled": False}      # disarmed last


def test_directory_chooser_is_refused_and_still_disarmed(tmp_path):
    # Arrange
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"x")
    browser = FakeBrowser({"backendNodeId": 5, "mode": "openDirectory"})

    # Act
    outcome = _run(browser, str(cv))

    # Assert
    assert outcome["ok"] is False and "directory" in outcome["note"]
    assert "DOM.setFileInputFiles" not in browser.methods()
    assert browser.sent[-1]["params"] == {"enabled": False}


def test_no_chooser_times_out_cleanly(tmp_path):
    # Arrange
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"x")
    browser = FakeBrowser(chooser_params=None)

    # Act
    outcome = _run(browser, str(cv), timeout=0.05)

    # Assert
    assert outcome["ok"] is False and "no file chooser" in outcome["note"]
    assert browser.sent[-1]["params"] == {"enabled": False}


def test_missing_file_never_touches_the_browser(tmp_path):
    # Arrange
    browser = FakeBrowser({"backendNodeId": 1})

    # Act
    outcome = _run(browser, str(tmp_path / "missing.pdf"))

    # Assert
    assert outcome["ok"] is False and browser.sent == []
