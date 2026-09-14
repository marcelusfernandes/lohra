"""Host policy is checked before DNS/connection at every manually followed hop."""

import httpx
import pytest

from lohra.web.egress import EgressDenied
from lohra.web.fetch import fetch_url
from lohra.web.safety import WebError
from lohra.workflow.sandbox import WorkflowPolicy, sandbox_dispatch


def _network(routes, *, internal_host=None):
    resolved, connected = [], []
    def resolver(host, port):
        resolved.append(host)
        ip = "127.0.0.1" if host == internal_host else "93.184.216.34"
        return [(2, 1, 6, "", (ip, 0))]
    def handler(request):
        connected.append(str(request.url))
        location = routes.get(str(request.url))
        return (httpx.Response(302, headers={"location": location}) if location
                else httpx.Response(200, text="arrived"))
    return resolver, httpx.MockTransport(handler), resolved, connected


@pytest.mark.parametrize("allowed_hosts", [(), ("other.test",)])
def test_initial_host_denied_before_dns_or_request(allowed_hosts):
    resolver, transport, resolved, connected = _network({})
    with httpx.Client(transport=transport) as client, pytest.raises(EgressDenied) as error:
        fetch_url("https://api.test/", client=client, resolver=resolver, allowed_hosts=allowed_hosts)
    assert error.value.reason == "egress_not_allowed"
    assert resolved == connected == []


@pytest.mark.parametrize("follow_redirects", [False, True])
def test_later_hop_is_denied_even_if_injected_client_would_follow_redirects(follow_redirects):
    routes = {"https://api.test/start": "/next", "https://api.test/next": "https://other.test/ok",
              "https://other.test/ok": "//outside.test/private-CANARY?secret=CANARY"}
    resolver, transport, resolved, connected = _network(routes)
    with httpx.Client(transport=transport, follow_redirects=follow_redirects) as client:
        with pytest.raises(EgressDenied) as error:
            fetch_url("https://api.test/start", client=client, resolver=resolver,
                      allowed_hosts=("api.test", "other.test"))
    assert error.value.reason == "egress_redirect_not_allowed"
    assert "outside.test" in str(error.value) and "redirect hop 3" in str(error.value)
    assert "CANARY" not in str(error.value)
    assert resolved == ["api.test", "api.test", "other.test"]
    assert connected == list(routes)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_all_redirect_codes_allow_explicit_hosts(status):
    resolved, connected = [], []
    def resolver(host, port):
        resolved.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]
    def handler(request):
        connected.append(request.url.host)
        return (httpx.Response(status, headers={"location": "https://sub.api.test/final"})
                if request.url.host == "api.test" else httpx.Response(200, text="arrived"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_url("https://API.TEST/start", client=client, resolver=resolver,
                         allowed_hosts=("API.TEST", "SUB.API.TEST")) == "arrived"
    assert resolved == connected == ["api.test", "sub.api.test"]


@pytest.mark.parametrize("url,hosts,allowed", [
    ("https://API.TEST/", ("api.test",), True),
    ("https://api.test:8443/", ("API.TEST",), True),
    ("https://sub.api.test/", ("api.test",), False),
    ("https://sub.api.test/", ("sub.api.test",), True),
    ("https://sub.api.test/", ("*.api.test",), False),
    ("https://api.test.attacker.test/", ("api.test",), False),
    ("https://api.test@attacker.test/", ("api.test",), False),
    ("https://api.test./", ("api.test",), False),
    ("https://[bad/", ("api.test",), False),
    ("/no-host", ("",), False),
])
def test_initial_wrapper_and_fetcher_use_identical_host_matching(tmp_path, url, hosts, allowed):
    # Exercise both entry points, rather than asserting the helper against itself.
    reached = []
    wrapper = sandbox_dispatch(lambda *args: reached.append(args), working_root=tmp_path,
                               policy=WorkflowPolicy(egress_allow=hosts), tainted=False)
    wrapper("web_fetch", {"url": url})
    assert bool(reached) is allowed
    resolver, transport, resolved, connected = _network({})
    with httpx.Client(transport=transport) as client:
        if allowed:
            assert fetch_url(url, client=client, resolver=resolver, allowed_hosts=hosts) == "arrived"
            assert len(resolved) == len(connected) == 1
        else:
            with pytest.raises(EgressDenied):
                fetch_url(url, client=client, resolver=resolver, allowed_hosts=hosts)
            assert resolved == connected == []


@pytest.mark.parametrize("explicit_none", [False, True])
def test_omitted_and_none_host_policy_preserve_unsandboxed_redirects(explicit_none):
    resolver, transport, resolved, connected = _network({
        "https://api.test/": "https://otherwise-denied.test/",
    })
    kwargs = {"allowed_hosts": None} if explicit_none else {}
    with httpx.Client(transport=transport) as client:
        assert fetch_url("https://api.test/", client=client, resolver=resolver, **kwargs) == "arrived"
    assert resolved == ["api.test", "otherwise-denied.test"] and len(connected) == 2


@pytest.mark.parametrize("initial", [False, True])
def test_allowlisted_internal_address_still_fails_ssrf(initial):
    url = "https://internal.test/" if initial else "https://api.test/"
    resolver, transport, resolved, connected = _network({"https://api.test/": "https://internal.test/"},
                                                       internal_host="internal.test")
    with httpx.Client(transport=transport) as client, pytest.raises(WebError) as error:
        fetch_url(url, client=client, resolver=resolver, allowed_hosts=("api.test", "internal.test"))
    assert not isinstance(error.value, EgressDenied)
    assert "non-public" in str(error.value)
    assert resolved == (["internal.test"] if initial else ["api.test", "internal.test"])
    assert connected == ([] if initial else ["https://api.test/"])
