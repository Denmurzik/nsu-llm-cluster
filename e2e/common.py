"""Shared plumbing for the e2e driver scripts: transport, login, CSRF-aware portal session."""
import http.cookies
import ssl
import sys

import httpx

COOKIE_NAME = "nsullm_session"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class Fail(Exception):
    """An assertion about the system under test failed (exit code 1)."""


def parse_resolve(items):
    """['host:ip', ...] -> {host: ip}."""
    mapping = {}
    for item in items or []:
        host, sep, ip = item.partition(":")
        if not sep or not host or not ip:
            raise ValueError(f"--resolve ожидает host:ip, получено {item!r}")
        mapping[host.lower()] = ip
    return mapping


class ResolveTransport(httpx.BaseTransport):
    """Dials a fixed IP for selected hosts while keeping Host header and TLS SNI/verification on the name."""

    def __init__(self, mapping, inner):
        self.mapping = mapping
        self.inner = inner

    def handle_request(self, request):
        host = request.url.host
        ip = self.mapping.get(host.lower())
        if ip is None:
            return self.inner.handle_request(request)
        # A new request: httpx resolves redirects against the original, so it must keep the real host.
        dialed = httpx.Request(
            request.method,
            request.url.copy_with(host=ip),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return self.inner.handle_request(dialed)

    def close(self):
        self.inner.close()


def make_transport(resolve, cacert):
    verify = ssl.create_default_context(cafile=cacert) if cacert else True
    inner = httpx.HTTPTransport(verify=verify)
    mapping = parse_resolve(resolve)
    return ResolveTransport(mapping, inner) if mapping else inner


def add_connection_args(parser):
    parser.add_argument("--base", required=True, help="portal URL, e.g. https://localhost")
    parser.add_argument("--resolve", action="append", default=[], metavar="HOST:IP",
                        help="dial IP for HOST (SNI and Host stay HOST); repeatable")
    parser.add_argument("--cacert", help="CA bundle used to verify the portal certificate")
    parser.add_argument("--login", choices=["fake", "cookie"], default="fake",
                        help="fake: follow the fake-OIDC redirect flow; cookie: use --cookie")
    parser.add_argument("--cookie", help=f"value of the {COOKIE_NAME} cookie (with --login cookie)")


def validate_connection_args(parser, args):
    if args.login == "cookie" and not args.cookie:
        parser.error("--login cookie требует --cookie")
    try:
        parse_resolve(args.resolve)
    except ValueError as exc:
        parser.error(str(exc))


def _session_cookie_from(responses, name):
    value = None
    for response in responses:
        for header in response.headers.get_list("set-cookie"):
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(header)
            except http.cookies.CookieError:
                continue
            if name in jar and jar[name].value:
                value = jar[name].value
    return value


class Portal:
    """HTTP session against the portal: cookie auth, lazily fetched CSRF token for mutations."""

    def __init__(self, base, transport, cookie):
        self.base = base.rstrip("/")
        self.transport = transport
        self.cookie = cookie
        self.client = httpx.Client(
            base_url=self.base, transport=transport, timeout=30,
            headers={"Cookie": f"{COOKIE_NAME}={cookie}"},
        )
        self._me = None

    @classmethod
    def login_cookie(cls, base, transport, cookie):
        return cls(base, transport, cookie)

    @classmethod
    def login_fake(cls, base, transport):
        # Own jar only for the redirect chain: the Secure cookie must still be readable over plain http.
        with httpx.Client(base_url=base.rstrip("/"), transport=transport, timeout=30,
                          follow_redirects=True) as client:
            response = client.get("/auth/login", params={"return_to": "/"})
            cookie = _session_cookie_from([*response.history, response], COOKIE_NAME)
        if not cookie:
            raise Fail(f"вход не удался: сессионная cookie {COOKIE_NAME} не выдана "
                       f"(последний ответ {response.status_code} {response.url})")
        return cls(base, transport, cookie)

    @classmethod
    def from_args(cls, args):
        transport = make_transport(args.resolve, args.cacert)
        if args.login == "cookie":
            return cls.login_cookie(args.base, transport, args.cookie)
        return cls.login_fake(args.base, transport)

    def close(self):
        self.client.close()

    def me(self, refresh=False):
        if self._me is None or refresh:
            response = self.client.get("/api/me")
            if response.status_code != 200:
                raise Fail(f"GET /api/me -> {response.status_code}: сессия недействительна")
            self._me = response.json()
        return self._me

    def request(self, method, path, expect=(200, 201, 204), **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() in MUTATING:
            headers["X-CSRF-Token"] = self.me()["csrf_token"]
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code not in expect:
            raise Fail(f"{method} {path} -> {response.status_code}: {_detail(response)}")
        return response

    def json(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        return response.json() if response.content else None


def _detail(response):
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    return body.get("detail", body) if isinstance(body, dict) else body


def run_guarded(func, argv_error_types=()):
    """Maps outcomes to exit codes: 0 success, 1 assertion failed, 2 connection error."""
    try:
        return func()
    except Fail as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    except (httpx.TransportError, *argv_error_types) as exc:
        print(f"ERROR: нет соединения с порталом: {exc}", file=sys.stderr)
        return 2

