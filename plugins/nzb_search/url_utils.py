from socket import gethostbyname
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


def authenticated_download_url(url, base, key):
    """Return a reachable Prowlarr URL and add its key only to owned links."""
    base = (base or "http://prowlarr:9696").rstrip("/")
    key = (key or "").strip()
    url = urljoin(base + "/", url)
    parsed, base_parsed = urlparse(url), urlparse(base)
    hostname = (parsed.hostname or "").lower()
    base_hostname = (base_parsed.hostname or "").lower()
    same_host = hostname == base_hostname and parsed.port == base_parsed.port
    loopback_alias = hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == base_parsed.port
    if key and parsed.netloc and (same_host or loopback_alias):
        # Prowlarr often advertises its container-local URL. Rewrite only that
        # loopback alias to the configured service address so the bot container
        # can fetch the NZB through the shared Docker network.
        service_netloc = base_parsed.netloc
        if base_hostname and "." not in base_hostname:
            try:
                address = gethostbyname(base_hostname)
                if "." in address:
                    service_netloc = f"{address}:{base_parsed.port}" if base_parsed.port else address
            except OSError:
                pass
        parsed = parsed._replace(scheme=base_parsed.scheme, netloc=service_netloc)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("apikey", key)
        return urlunparse(parsed._replace(query=urlencode(query)))
    return url


def nzb_mirror_command(url, destination="gd", name=None):
    """Build a WZML command (its parser splits on spaces, not shell syntax)."""
    parts = ["/nzbmirror", str(url).strip()]
    if name:
        # WZML joins all tokens between flags into the name; shell quotes would
        # be retained literally and make the NZB URL fail URL validation.
        clean_name = " ".join(str(name).split())[:180]
        parts.extend(["-n", clean_name])
    parts.extend(["-up", str(destination).strip()])
    return " ".join(parts)
