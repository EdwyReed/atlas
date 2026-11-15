"""Private existing-key agent sidecar; no key copy, forwarding, or enrollment.

Run as namespace root with CHOWN, SETUID, SETGID, DAC_OVERRIDE, KILL and a
new disposable socket volume. The application remains UID 10001. Stop this
sidecar with the application; the socket volume contains no private key.
"""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', required=True, help='existing user@host known-host binding')
    args = parser.parse_args()
    if args.destination.startswith('-') or '@' not in args.destination:
        parser.error('explicit user@host required')
    socket = Path('/run/atlas-ssh-agent/socket')
    ready = socket.with_name('ready')
    # A leftover socket from a stopped container is safe to remove only if dead.
    if socket.exists():
        import socket as unix
        probe = unix.socket(unix.AF_UNIX)
        try:
            probe.connect(str(socket))
        except (ConnectionRefusedError, FileNotFoundError):
            socket.unlink(missing_ok=True)
        else:
            raise SystemExit('refusing an existing live agent')
        finally:
            probe.close()
    ready.unlink(missing_ok=True)
    env = dict(os.environ, SSH_AUTH_SOCK=str(socket))
    # ssh-agent verifies peer UIDs, not just socket permissions. Run the agent
    # as the application's UID; namespace-root only loads the existing key.
    os.chown(socket.parent, 10001, 10001)
    agent = subprocess.Popen(['ssh-agent', '-D', '-a', str(socket)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             user=10001, group=10001, extra_groups=[])
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for _ in range(100):
            if socket.exists():
                break
            if agent.poll() is not None:
                raise RuntimeError('agent exited before readiness')
            time.sleep(.05)
        else:
            raise RuntimeError('agent readiness timeout')
        # Destination constraint restricts this agent to the already trusted server.
        result = subprocess.run(['ssh-add', '-H', '/run/atlas-ssh/known_hosts',
                                 '-h', args.destination, '/run/atlas-ssh/identity'],
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10)
        if result.returncode:
            raise RuntimeError('existing identity unavailable; no credential fallback')
        ready.touch(mode=0o644)
        print('destination-constrained agent ready', flush=True)
        raise SystemExit(agent.wait())
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait()
        socket.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
