"""Bounded local test transport: no DNS, ambient proxy or redirect to an external host."""
import ipaddress
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_local(request: Request, timeout=10):
    parsed = urlsplit(request.full_url)
    try:
        allowed = ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        allowed = False
    if parsed.scheme != 'http' or not allowed or parsed.username or parsed.password:
        raise ValueError('Only literal loopback HTTP is allowed by the local test transport')
    return build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)
