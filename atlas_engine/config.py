"""Explicit standalone configuration. Relative paths belong to the config file."""
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field


class Connection(BaseModel):
    model_config = ConfigDict(extra='forbid')
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
    connections: list[Connection] = Field(default_factory=list, max_length=1)
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


def bind_connection(root, connection):
    """Single legacy backend per state root until scoped native migration exists."""
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
