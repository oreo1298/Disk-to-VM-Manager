"""QMP client against a stand-in for QEMU's control socket."""

import json
import socket
import threading

import pytest

from rehome.vm.qmp import QMP, QmpClosed

SHUTDOWN = {"event": "SHUTDOWN", "data": {"guest": True, "reason": "guest-shutdown"},
            "timestamp": {"seconds": 1, "microseconds": 0}}


def fake_qemu(path, then):
    """Greet, accept qmp_capabilities, then send ``then`` (list of messages) and hang up."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    go = threading.Event()

    def serve():
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        f.write(json.dumps({"QMP": {"version": {}, "capabilities": []}}).encode() + b"\n")
        f.flush()
        req = json.loads(f.readline())
        f.write(json.dumps({"return": {}, "id": req["id"]}).encode() + b"\n")
        f.flush()
        go.wait(5)
        # Like QEMU at the end: the event, then the socket closes at once.
        f.write(b"".join(json.dumps(m).encode() + b"\n" for m in then))
        f.flush()
        f.close()
        conn.close()
        srv.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return go, t


def test_event_sent_just_before_qemu_exits_is_not_lost(tmp_path):
    """The kiosk reported normal shutdowns as crashes when the socket closed with the event."""
    go, t = fake_qemu(tmp_path / "qmp.sock", [SHUTDOWN])
    q = QMP(str(tmp_path / "qmp.sock"), timeout=5).connect()
    go.set()
    t.join(5)                          # event and hang-up are both waiting now
    events = q.poll_events(timeout=1.0)
    assert [e["data"]["reason"] for e in events] == ["guest-shutdown"]
    with pytest.raises(QmpClosed):
        q.poll_events(timeout=0.1)


def test_hang_up_without_events_still_raises(tmp_path):
    go, t = fake_qemu(tmp_path / "qmp.sock", [])
    q = QMP(str(tmp_path / "qmp.sock"), timeout=5).connect()
    go.set()
    t.join(5)
    with pytest.raises(QmpClosed):
        q.poll_events(timeout=1.0)
