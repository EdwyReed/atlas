"""Read-only SSH metadata transport; never invoke the model installer."""
import json
import math
import time
from pathlib import Path
import re
import subprocess
from .contracts import Fault
from .lora_install import descriptor
from .windows_ssh import command as ssh_command
from .civitai_readonly_windows import validate, MAXIMUM


def fetch(policy, url):
    try:
        validate(url)
    except (ValueError, TypeError):
        raise Fault('SOURCE_URL', 'only credential-free civitai.red metadata GET is allowed') from None
    cfg = descriptor(policy)
    script = Path(__file__).with_name('civitai_readonly_windows.py').read_text()
    script += '\nmain(' + repr(url) + ')\n'
    cmd = ssh_command(cfg)
    try:
        proc = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=55)
        if proc.returncode or len(proc.stdout) > MAXIMUM * 6 + 1024:
            raise ValueError()
        value = json.loads(proc.stdout)
        if value == {'ok': True, 'value': value.get('value')} and isinstance(value.get('value'), dict):
            return value['value']
        code = value.get('code')
        delay = value.get('retry_after', 60)
        retry_shape = (code == 'SOURCE_HTTP_429' and set(value) == {'ok', 'code', 'retry_after'}
                       and type(delay) in (int, float) and math.isfinite(delay) and delay >= 0)
        if ((set(value) == {'ok', 'code'} or retry_shape) and value['ok'] is False and isinstance(code, str)
                and re.fullmatch(r'SOURCE_(?:HTTP_[1-5][0-9]{2}|URL|LIMIT|SHAPE|REDACTED|TRANSPORT)', code)):
            exc = Fault(code, 'read-only Windows metadata request rejected; no redirect or fallback')
            if code == 'SOURCE_HTTP_429':
                exc.retry_at = time.time() + delay
            raise exc
    except (subprocess.SubprocessError, ValueError, TypeError, AttributeError, OSError):
        pass
    raise Fault('SOURCE_TRANSPORT', 'read-only Windows metadata transport unavailable; no private diagnostics retained')
