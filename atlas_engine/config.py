"""Explicit standalone configuration. Relative paths belong to the config file."""
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class Connection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    _token_reference: str | None = PrivateAttr(default=None)
    id: str = Field(min_length=1)
    backend_generation: str = Field(min_length=1)
    account_scope: str = Field(min_length=1)
    url: str
    token_file: Path | None = None
    enable_writes: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid')
    root: Path
    clients_file: Path
    host: str = '127.0.0.1'
    port: int = Field(default=12088, ge=1, le=65535)
    container_endpoint: str | None = None
    vocabulary: Path | None = None
    connections: list[Connection] = Field(default_factory=list)
    default_connection_id: str | None = None
    compatibility_routing: bool = False
    migrate_connection_binding: bool = False

    @model_validator(mode='after')
    def connection_ids(self):
        ids = [c.id for c in self.connections]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate connection ID')
        if self.default_connection_id is not None and (not self.compatibility_routing or self.default_connection_id not in ids):
            raise ValueError('default_connection_id requires compatibility_routing and a configured ID')
        return self
    reference_url: str | None = None
    reference_token_file: Path | None = None
    reference_mirror: Path | None = None
    reference_delta: Path | None = None
    reference_collections: list[str] | None = None
    projection_dir: Path | None = None
    policy_file: Path | None = None
    civitai_url: str = 'https://civitai.red'
    media_config_file: Path | None = None

    @classmethod
    def load(cls, path, **overrides):
        path = Path(path).absolute()
        config = cls.model_validate(json.loads(path.read_text()) | {k: v for k, v in overrides.items() if v is not None})
        if config.container_endpoint is not None:
            u = urlsplit(config.container_endpoint)
            if (config.host != '0.0.0.0' or u.scheme != 'http' or
                    u.hostname != '127.0.0.1' or not u.port or
                    config.container_endpoint != f'http://127.0.0.1:{u.port}'):
                raise ValueError('container_endpoint requires 0.0.0.0 bind and exact http://127.0.0.1:PORT endpoint')
        elif not ipaddress.ip_address(config.host).is_loopback:
            raise ValueError('this release requires a loopback IP bind or explicit container_endpoint')
        for name in ('root', 'clients_file', 'vocabulary', 'reference_token_file', 'media_config_file',
                     'reference_mirror', 'reference_delta', 'projection_dir', 'policy_file'):
            value = getattr(config, name)
            if value is not None:
                setattr(config, name, (path.parent / value).resolve())
        for connection in config.connections:
            validate_url(connection.url)
            if connection.token_file is not None:
                connection._token_reference = str(connection.token_file)
                connection.token_file = (path.parent / connection.token_file).resolve()
        if config.reference_url:
            validate_url(config.reference_url)
        if config.reference_mirror and config.reference_url:
            raise ValueError('choose reference mirror or URL')
        if config.reference_delta and not config.reference_mirror:
            raise ValueError('reference_delta requires reference_mirror')
        if config.reference_collections is not None and not config.reference_url:
            raise ValueError('typed reference collections require reference_url')
        validate_url(config.civitai_url)
        return config


def bind_connections(root, connections, migrate=False):
    """Append-only binding manifest, written while the daemon owns service.lock.

    Legacy files stay untouched. One atomic fsynced v2 file also IS the migration
    receipt, so interruption never leaves a receipt that disagrees with binding.
    """
    bindings = {c.id: {k: str(getattr(c, k)) if getattr(c, k) is not None else None
                      for k in ('id', 'url', 'backend_generation', 'account_scope', 'token_file')}
                for c in connections}
    for c in connections:
        if c._token_reference is not None:
            bindings[c.id]['token_file'] = c._token_reference
    path = root / 'connection-bindings.json'
    if path.exists():
        old = json.loads(path.read_text())
        if (not isinstance(old, dict) or old.get('format') != 'atlas-connection-bindings-v2'
                or not isinstance(old.get('connections'), dict)
                or any(not isinstance(v, dict) for v in old['connections'].values())):
            raise ValueError('unsupported connection binding manifest format')
        old = dict(old)
        revised = dict(old['connections'])
        relocations = []
        for c in connections:
            prior = revised.get(c.id)
            # Unpublished absolute-reference v2 candidates can opt into logical
            # references only at their original credential location. No changed
            # URL/account/generation or different secret path is accepted.
            if (migrate and prior and c._token_reference is not None
                    and prior.get('token_file') == str(c.token_file)
                    and prior.get('token_file') != bindings[c.id]['token_file']
                    and {k:v for k,v in prior.items() if k != 'token_file'} ==
                        {k:v for k,v in bindings[c.id].items() if k != 'token_file'}):
                revised[c.id] = bindings[c.id]
                relocations.append({'connection_id':c.id,'old_reference':prior['token_file'],
                                    'logical_reference':bindings[c.id]['token_file']})
        old['connections'] = revised
        for key, value in old['connections'].items():
            if bindings.get(key) != value:
                raise ValueError('connection binding changed or removed; startup refused')
        if bindings == old['connections'] and not relocations:
            return old
        if not migrate:
            raise ValueError('connection binding changed by addition; explicit migrate_connection_binding required')
        receipt = {**old, 'connections': bindings,
                   'additions': old.get('additions', []) + [sorted(set(bindings)-set(old['connections']))]}
        if relocations:
            receipt['token_reference_migrations'] = old.get('token_reference_migrations', []) + relocations
    else:
        legacy_path = root / 'connection-binding.json'
        legacy = json.loads(legacy_path.read_text()) if legacy_path.exists() else None
        if legacy_path.exists() and not migrate:
            raise ValueError('legacy binding requires explicit migrate_connection_binding receipt')
        if legacy and any(bindings.get(legacy['id'], {}).get(k) != v for k, v in legacy.items()):
            raise ValueError('legacy connection binding changed; startup refused')
        receipt = {'format': 'atlas-connection-bindings-v2', 'connections': bindings,
                   'legacy_connection_id': legacy['id'] if legacy else None,
                   'legacy_binding': legacy, 'migration': 'explicit-preserve-v1' if legacy_path.exists() else 'new-root',
                   'additions': []}
    # Embedded corpus owners do not take service.lock. Refuse publication while
    # they own this root too; no binding/receipt may change behind their back.
    import fcntl
    corpus = root / 'corpus'
    corpus.mkdir(exist_ok=True)
    with (corpus / 'owner.lock').open('a') as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('active corpus owner; connection migration refused')
        temp = root / 'connection-bindings.new'
        try:
            with temp.open('w') as f:
                json.dump(receipt, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            temp.replace(path)
            fd = os.open(root, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)
    return receipt


def bind_connection(root, connection):
    """Legacy helper retained for embedded compatibility fixtures only."""
    binding = {k: getattr(connection, k) for k in ('id', 'url', 'backend_generation', 'account_scope')} if connection else None
    path = root / 'connection-binding.json'
    if path.exists():
        if json.loads(path.read_text()) != binding:
            raise ValueError('connection binding changed; use a new state root or reviewed scoped migration')
        return
    temp = root / 'connection-binding.new'
    with temp.open('w') as f:
        json.dump(binding, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)
    fd = os.open(root, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_url(value):
    u = urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('upstream URL must be HTTP(S), without credentials, query or fragment')


def secret(path):
    if path is None:
        return ''
    if path.stat().st_mode & 0o077:
        raise ValueError('secret file must be mode 0600')
    value = path.read_text().strip()
    if not value:
        raise ValueError('empty secret file')
    return value


def clients(path):
    if path.stat().st_mode & 0o077:
        raise ValueError('clients file must be mode 0600')
    values = json.loads(path.read_text())
    if not isinstance(values, list) or not values:
        raise ValueError('at least one client required')
    for c in values:
        if (set(c) != {'token', 'principal', 'source'} or
                not all(isinstance(c[k], str) and c[k] for k in c) or
                len(c['token']) < 32 or not c['token'].isascii() or
                any(ch.isspace() for ch in c['token'])):
            raise ValueError('invalid client identity/token')
    if len({c['token'] for c in values}) != len(values):
        raise ValueError('duplicate credential')
    return values
