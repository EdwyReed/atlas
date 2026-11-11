"""Owned launch of an operator-supplied mask worker, without donor modifications."""
import ctypes
import os
from pathlib import Path
import runpy
import signal
import sys


def main():
    parent = int(sys.argv[1])
    if not sys.platform.startswith('linux'):
        raise RuntimeError('owned mask worker requires Linux parent-death binding')
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'prctl')
    if os.getppid() != parent:
        return
    sys.path.insert(0, str(Path(sys.argv[2])))
    runpy.run_module('atlas_mask.worker', run_name='__main__')


if __name__ == '__main__':
    main()
