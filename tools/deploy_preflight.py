"""Validate deployment prerequisites without publishing, printing secrets or creating accounts."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def settings(path: Path):
    result = {}
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        key, value = raw.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


def validate(values):
    required = Path(__file__).resolve().parents[1] / 'deploy/settings.env.example'
    names = settings(required)
    errors = [f'{name}: required' for name in names if not values.get(name)]
    if errors:
        return errors
    domain = values['PUBLIC_DOMAIN']
    if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?', domain) or '.' not in domain:
        errors.append('PUBLIC_DOMAIN: use a real DNS hostname')
    if domain.endswith(('.invalid', '.example', '.test', '.localhost')) or any(domain == name or domain.endswith('.' + name) for name in ('example.com', 'example.org', 'example.net')):
        errors.append('PUBLIC_DOMAIN: reserved test hostname is not a public deployment')
    try:
        ipaddress.ip_address(domain)
        errors.append('PUBLIC_DOMAIN: a DNS hostname is required')
    except ValueError:
        pass
    for name in ('OIDC_ISSUER', 'OIDC_JWKS_URL'):
        parsed = urlsplit(values[name])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.fragment:
            errors.append(name + ': HTTPS URL required')
    for name in ('CADDY_IMAGE', 'OAUTH2_PROXY_IMAGE'):
        if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', values[name]):
            errors.append(name + ': pin a verified image digest')
    try:
        mapping = json.loads(values['OIDC_GROUP_ROLES'])
        groups = {group.strip() for group in values['OIDC_ALLOWED_GROUPS'].split(',') if group.strip()}
        if not isinstance(mapping, dict) or not mapping or set(mapping) != groups or any(v not in {'agent','lead','admin'} for v in mapping.values()):
            errors.append('OIDC_GROUP_ROLES: match allowed groups with explicit operator roles')
        acrs = json.loads(values['OIDC_MFA_ACRS'])
        if not isinstance(acrs, list) or not acrs or any(not isinstance(v, str) or v.casefold() in {'0', '1', 'none', 'password'} for v in acrs):
            errors.append('OIDC_MFA_ACRS: explicit MFA assurance values required')
    except (ValueError, TypeError):
        errors.append('OIDC mapping/assurance: invalid JSON')
    for name in ('OIDC_CLIENT_SECRET_FILE', 'OIDC_COOKIE_SECRET_FILE'):
        path = Path(values[name])
        if not path.is_absolute() or not path.is_file():
            errors.append(name + ': absolute path to an existing local secret file required')
        elif name == 'OIDC_COOKIE_SECRET_FILE' and path.stat().st_size != 32:
            errors.append(name + ': exactly 32 raw bytes required')
    handout = Path(values['HANDOUT_DIR'])
    if not all((handout / name).is_file() for name in ('rules.json', 'taxonomy.json')):
        errors.append('HANDOUT_DIR: both organizer dictionaries required locally')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('settings', type=Path)
    args = parser.parse_args()
    try:
        errors = validate(settings(args.settings))
    except (ValueError, OSError):
        errors = ['Cannot parse/read the settings file']
    for error in errors:
        print(error)
    if not errors:
        print('Local prerequisites valid. DNS, issuer MFA policy, public certificate and end-to-end SSO still need live verification.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
