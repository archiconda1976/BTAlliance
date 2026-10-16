"""Host forwarding uses separate LAN/loopback sockets and obeys HA windows."""

from pathlib import Path
import runpy
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.btalliance.share_proxy import encode_forwarded_packet, decode_forwarded_packet


def test_host_proxy_advertises_forwards_and_acknowledges_only_during_window(monkeypatch, capsys):
    original = list(sys.path)
    try:
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tools' / 'btalliance_share_proxy.py'))
    finally:
        sys.path[:] = original
    globals_ = module['run'].__globals__
    key = bytes(range(32))
    phone = ('192.168.1.190', 58025)
    control_peer = ('192.168.1.206', 50000)
    payload = b'msg_send_data-0-private-export-not-for-logs'
    incoming = [
        (payload, phone),
        (encode_forwarded_packet(b'msg_client_online-0-HA', '0.0.0.0', key), control_peer),
        (payload, phone),
        (encode_forwarded_packet(b'msg_ok', phone[0], key), control_peer),
        (encode_forwarded_packet(b'msg_client_offline-0-', '0.0.0.0', key), control_peer),
        (payload, phone),
    ]
    lan, loopback = Mock(), Mock()
    lan.recvfrom.side_effect = lambda _: incoming.pop(0)

    def select(*_):
        if not incoming:
            raise KeyboardInterrupt
        return [lan], [], []

    monkeypatch.setitem(globals_, 'socket', SimpleNamespace(socket=Mock(side_effect=[lan, loopback]),
        AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
        SOL_SOCKET=socket.SOL_SOCKET, SO_BROADCAST=socket.SO_BROADCAST))
    monkeypatch.setitem(globals_, 'select', SimpleNamespace(select=select))
    monkeypatch.setitem(globals_, 'signal', SimpleNamespace(signal=Mock(), SIGINT=2, SIGTERM=15))
    monkeypatch.setitem(globals_, 'read_key', lambda _: key)
    with pytest.raises(KeyboardInterrupt):
        module['run'](SimpleNamespace(bind='192.168.1.206', network='192.168.1.0/24',
            key_file='unused', status_file=None, duration=0))
    lan.bind.assert_called_once_with(('192.168.1.206', 58024))
    loopback.bind.assert_called_once_with(('127.0.0.1', 0))
    wrapped, target = loopback.sendto.call_args.args
    loopback.sendto.assert_called_once()
    assert target == ('127.0.0.1', 58026)
    sender, _, data = decode_forwarded_packet(wrapped, key)
    assert sender == phone[0] and data == payload
    lan.sendto.assert_any_call(b'msg_ok', phone)
    lan.sendto.assert_any_call(b'msg_client_online-0-HA', ('192.168.1.255', 58025))
    lan.close.assert_called_once()
    loopback.close.assert_called_once()
    assert 'private-export' not in capsys.readouterr().out
