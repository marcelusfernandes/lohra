"""Real pip auth paths with synthetic homes; no network, install or real keyring."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ci.wheel_gate import clean_environment


_PROBE = r'''
import json
import socket
import sys
from types import SimpleNamespace

from pip._internal.commands import create_command
from pip._internal.network import auth
from pip._vendor.requests import Request, Response

def no_network(*args, **kwargs):
    raise AssertionError("auth probe attempted network access")

socket.socket.connect = no_network
command = create_command(sys.argv[1])
options, _ = command.parse_args([])
with command._build_session(options) as session:
    urls = ("https://pypi.org/simple/pip/", "https://files.pythonhosted.org/fake.whl")
    prepared = [session.prepare_request(Request("GET", url)) for url in urls]
    report = {"authorization": ["Authorization" in req.headers for req in prepared],
              "keyring_provider": options.keyring_provider, "no_input": bool(options.no_input),
              "index_url": options.index_url, "client_cert": session.cert,
              "keyring_calls": [], "prompts": [], "sends": []}
    if sys.argv[2] == "401":
        class FakeKeyring:
            has_keyring = True

            def get_auth_info(self, url, username):
                report["keyring_calls"].append(url)
                return None

        # Observe the real pip decision to consult keyring, without importing
        # or launching any actual provider/keychain.
        auth.get_keyring_provider = lambda provider: FakeKeyring()

        def prompt(netloc):
            report["prompts"].append(netloc)
            return None, None, False

        def send(request, **kwargs):
            report["sends"].append(request.url)
            return Response()

        session.auth._prompt_for_password = prompt
        response = Response()
        response.status_code = 401
        response.url = urls[0]
        response.request = prepared[0]
        response._content = b""
        response.raw = SimpleNamespace(release_conn=lambda: None)
        response.connection = SimpleNamespace(send=send)
        report["same_response"] = session.auth.handle_401(response) is response
    print(json.dumps(report))
'''


def _environment(tmp_path, *, netrc):
    home, codex = tmp_path / "home", tmp_path / "codex"
    home.mkdir()
    codex.mkdir()
    if netrc:
        (home / ".netrc").write_text("default login fictional-user password fictional-password\n")
        (home / ".netrc").chmod(0o600)
    return {"HOME": str(home), "CODEX_HOME": str(codex),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath}


def _probe(tmp_path, env, command, mode="prepare"):
    result = subprocess.run([sys.executable, "-I", "-c", _PROBE, command, mode],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("command", ["wheel", "install"])
@pytest.mark.parametrize("caller_netrc", [False, True], ids=["absent", "already-disabled"])
def test_pip_request_ignores_home_netrc_without_moving_homes(tmp_path, command, caller_netrc):
    inherited = _environment(tmp_path, netrc=True)
    if caller_netrc:
        inherited["NETRC"] = os.devnull
    inherited.update(PIP_INDEX_URL="https://fictional-user:fictional-password@example.invalid",
                     PIP_CLIENT_CERT="/fictional/certificate.pem", PIP_KEYRING_PROVIDER="import")
    env = clean_environment(inherited)
    assert (env["HOME"], env["CODEX_HOME"]) == (inherited["HOME"], inherited["CODEX_HOME"])
    report = _probe(tmp_path, env, command)
    assert report["authorization"] == [False, False], report
    assert report["index_url"] == "https://pypi.org/simple"
    assert report["client_cert"] is None


@pytest.mark.parametrize("command", ["wheel", "install"])
def test_pip_401_never_consults_keyring_or_prompts(tmp_path, command):
    inherited = _environment(tmp_path, netrc=False)
    inherited["PIP_KEYRING_PROVIDER"] = "import"
    report = _probe(tmp_path, clean_environment(inherited), command, "401")
    assert report["keyring_calls"] == [], report
    assert report["prompts"] == [] and report["sends"] == [], report
    assert report["same_response"] is True
    assert report["keyring_provider"] == "disabled" and report["no_input"] is True


def test_auth_probe_detects_synthetic_netrc_when_isolation_is_absent(tmp_path):
    env = _environment(tmp_path, netrc=True)
    env["PIP_CONFIG_FILE"] = os.devnull
    report = _probe(tmp_path, env, "install")
    assert report["authorization"] == [True, True]
