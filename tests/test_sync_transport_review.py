"""Independent protocol fault controls; local child processes only, no Docker/Kube."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import os
import shlex
import shutil
import sys
import threading
import time

import pytest

from podgrove.errors import PodgroveError
from podgrove.sync_transport import RECEIVER, STDERR_BYTES, TarStream

NONCE = "0123456789abcdef" * 2
PREAMBLE = f"""
import hashlib,os,sys,time
nonce={NONCE!r}
def emit(value):
 sys.stdout.buffer.write(value);sys.stdout.buffer.flush()
def exact(size):
 data=bytearray()
 while len(data)<size:
  chunk=sys.stdin.buffer.read(size-len(data))
  if not chunk: sys.exit(0)
  data.extend(chunk)
 return bytes(data)
emit(('READY '+nonce+'\\n').encode())
"""
READ_FRAME = """
header=exact(72)
magic,frame_nonce,sequence,length=header.decode().split()
assert magic=='PGS1' and frame_nonce==nonce
payload=exact(int(length))
"""


@contextmanager
def stream(script, timeout=1):
    cancelled = threading.Event()
    connection = TarStream([sys.executable, "-u", "-c", script], os.environ.copy(), cancelled, NONCE, timeout)
    try:
        yield connection, cancelled
    finally:
        connection.close()
        assert connection.process.poll() is not None
        assert not connection._reader.is_alive(), "Owned stderr reader outlived transport close"


def test_fragmented_acknowledgements_and_binary_frame_boundaries_across_sequence_ten(tmp_path):
    observed = tmp_path / "hashes"
    script = PREAMBLE + "\nwhile True:\n" + "\n".join(" " + line for line in READ_FRAME.strip().splitlines()) + f"""
 with open({str(observed)!r},'a') as record: record.write(sequence+' '+hashlib.sha256(payload).hexdigest()+'\\n')
 for value in ('ACK '+nonce+' '+sequence+'\\n').encode():
  emit(bytes([value]));time.sleep(.0002)
"""
    payloads = [(b"\x00\nPGS1 fake\nACK fake\n" + bytes(range(256))) * index for index in range(1, 13)]
    with stream(script, timeout=2) as (connection, _):
        connection.ready()
        for payload in payloads:
            connection.transfer(io.BytesIO(payload))
            connection.check()
        assert connection.sequence == 13
    assert observed.read_text().splitlines() == [f"{index:016d} {hashlib.sha256(payload).hexdigest()}"
                                               for index, payload in enumerate(payloads, 1)]


@pytest.mark.parametrize("ack", [
    b"ACK wrong\n", b"ACK " + NONCE.encode() + b" 0000000000000000\n",
    b"ACK " + NONCE.encode() + b" 0000000000000002\n", b"x" * 8192,
    (b"ACK " + NONCE.encode() + b" 0000000000000001\n") * 2,
])
def test_wrong_stale_future_oversize_or_duplicate_ack_poisons_without_retry(ack):
    script = PREAMBLE + READ_FRAME + f"emit({ack!r});time.sleep(60)\n"
    with stream(script) as (connection, _):
        connection.ready()
        with pytest.raises(PodgroveError, match="acknowledgement"):
            connection.transfer(io.BytesIO(b"one-batch"))
        assert connection.sequence == 1 and connection.process.poll() is not None
        with pytest.raises(PodgroveError):
            connection.transfer(io.BytesIO(b"must-not-retry"))


def test_early_ack_before_large_frame_is_fully_sent_is_not_commit():
    script = PREAMBLE + "header=exact(72);emit(('ACK '+nonce+' 0000000000000001\\n').encode());time.sleep(60)\n"
    with stream(script) as (connection, _):
        connection.ready()
        with pytest.raises(PodgroveError, match="complete archive"):
            connection.transfer(io.BytesIO(b"x" * (4 * 1024 * 1024)))
        assert connection.sequence == 1


def test_zero_exit_after_apply_without_ack_remains_uncertain_and_is_never_retried(tmp_path):
    applied = tmp_path / "applied"
    script = PREAMBLE + READ_FRAME + f"open({str(applied)!r},'wb').write(payload)\nsys.exit(0)\n"
    with stream(script) as (connection, _):
        connection.ready()
        with pytest.raises(PodgroveError):
            connection.transfer(io.BytesIO(b"committed-but-unacknowledged"))
        assert applied.read_bytes() == b"committed-but-unacknowledged"
        assert connection.sequence == 1
        with pytest.raises(PodgroveError):
            connection.transfer(io.BytesIO(b"unsafe-retry"))
        assert applied.read_bytes() == b"committed-but-unacknowledged"


def test_stderr_flood_is_drained_without_deadlock_and_keeps_only_bounded_tail():
    script = PREAMBLE + READ_FRAME + """
sys.stderr.buffer.write(b'x'*(2*1024*1024)+b' final-stderr-marker');sys.stderr.buffer.flush()
emit(('ACK '+nonce+' '+sequence+'\\n').encode());time.sleep(60)
"""
    with stream(script, timeout=4) as (connection, _):
        connection.ready()
        connection.transfer(io.BytesIO(b"small"))
        deadline = time.monotonic() + 1
        while b"final-stderr-marker" not in connection._stderr and time.monotonic() < deadline:
            time.sleep(.01)
        with connection._stderr_lock:
            assert len(connection._stderr) <= STDERR_BYTES
            assert bytes(connection._stderr).endswith(b"final-stderr-marker")


def test_cancellation_while_waiting_for_commit_kills_child_and_joins_owned_reader():
    script = PREAMBLE + READ_FRAME + "time.sleep(60)\n"
    with stream(script, timeout=30) as (connection, cancelled):
        connection.ready()
        timer = threading.Timer(.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(PodgroveError, match="cancelled"):
                connection.transfer(io.BytesIO(b"awaiting-ack"))
        finally:
            timer.join()
        assert time.monotonic() - started < 2
        assert connection.process.poll() is not None


def test_ack_deadline_is_absolute_even_while_stderr_continues_arriving():
    script = PREAMBLE + READ_FRAME + "\nwhile True:\n sys.stderr.write('still alive\\n');sys.stderr.flush();time.sleep(.01)\n"
    with stream(script, timeout=.15) as (connection, _):
        connection.ready()
        started = time.monotonic()
        with pytest.raises(PodgroveError, match="timed out"):
            connection.transfer(io.BytesIO(b"awaiting-ack"))
        assert time.monotonic() - started < 2


def test_idle_unsolicited_ack_is_rejected_without_sending_another_frame(tmp_path):
    signal_file = tmp_path / "send-stray"
    script = PREAMBLE + f"""
while not os.path.exists({str(signal_file)!r}): time.sleep(.01)
emit(('ACK '+nonce+' 0000000000000001\\n').encode());time.sleep(60)
"""
    with stream(script) as (connection, _):
        connection.ready()
        signal_file.write_text("go")
        deadline = time.monotonic() + 2
        while True:
            try:
                connection.check()
            except PodgroveError as exc:
                assert "unexpected idle acknowledgement" in str(exc)
                break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert connection.sequence == 1
        with pytest.raises(PodgroveError):
            connection.transfer(io.BytesIO(b"never-sent"))


def test_ready_handshake_must_match_nonce_before_any_frame_is_sent():
    script = "import sys,time;sys.stdout.write('READY wrong\\n');sys.stdout.flush();time.sleep(60)"
    with stream(script) as (connection, _):
        with pytest.raises(PodgroveError, match="invalid acknowledgement"):
            connection.ready()
        assert connection.sequence == 1 and connection.process.poll() is not None


def test_actual_receiver_shell_preserves_twelve_frame_boundaries_and_applies_before_ack(tmp_path):
    # GNU tools provide Linux-compatible stat/dd on macOS; the separate Docker
    # integration exercises the same exported receiver with Alpine BusyBox.
    binaries = {name: shutil.which("g"+name) or (shutil.which(name) if sys.platform != "darwin" else None)
                for name in ("stat", "head", "dd")}
    if not all(binaries.values()):
        pytest.skip("Linux tools or GNU coreutils are required for the receiver shell test")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, executable in binaries.items():
        (tools / name).symlink_to(executable)
    observed = tmp_path / "applied-bytes"
    apply = "cat >> " + shlex.quote(str(observed))
    env = {**os.environ, "PATH": str(tools)+os.pathsep+os.environ["PATH"]}
    receiver = RECEIVER.replace("/tmp/podgrove-frame.XXXXXX", str(tmp_path / "frame.XXXXXX"))
    connection = TarStream(["sh", "-c", receiver, "podgrove-receiver-test", NONCE, apply], env,
                           threading.Event(), NONCE, timeout=5)
    expected = b""
    try:
        connection.ready()
        for sequence in range(1, 13):
            payload = bytes(range(256)) * sequence + b"\x00\nPGS1 ignored\nACK ignored\n"
            expected += payload
            connection.transfer(io.BytesIO(payload))
            assert observed.read_bytes() == expected  # The ACK implies apply has completed.
    finally:
        connection.close()
    assert connection.process.poll() is not None
