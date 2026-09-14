"""Real local TLS with an ephemeral CA; no external endpoint or stored credentials."""

import http.server
import json
import ssl
import subprocess

LOCAL = "127.0.0.1"


def openssl(root, *args):
    return subprocess.run(
        ["openssl", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def certificates(root):
    ca_conf = root / "ca.cnf"
    ca_conf.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n[dn]\nCN=Lohra Synthetic CA\n[ca]\nbasicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
    )
    openssl(
        root,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-config",
        "ca.cnf",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
    )
    contexts = {}
    for host in ("first.test", "second.test"):
        config = root / (host + ".cnf")
        config.write_text(
            "[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN="
            + host
            + "\n[server]\nsubjectAltName=DNS:"
            + host
            + "\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"
        )
        openssl(
            root,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-config",
            config.name,
            "-keyout",
            host + ".key",
            "-out",
            host + ".csr",
        )
        openssl(
            root,
            "x509",
            "-req",
            "-in",
            host + ".csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            config.name,
            "-extensions",
            "server",
            "-out",
            host + ".pem",
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(root / (host + ".pem")), str(root / (host + ".key")))
        contexts[host] = ctx
    return contexts


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, contexts):
        self.snis, self.requests, self.active = [], [], set()
        super().__init__((LOCAL, 0), Handler)

        def sni(sock, name, initial):
            sock.logical_sni = name
            self.snis.append(name)
            sock.context = contexts.get(name, contexts["first.test"])

        contexts["first.test"].set_servername_callback(sni)
        self.socket = contexts["first.test"].wrap_socket(self.socket, server_side=True)

    def handle_error(self, request, client_address):
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.server.active.add(id(self.connection))

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.active.discard(id(self.connection))

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(
            dict(
                path=self.path,
                host=self.headers.get("Host"),
                sni=getattr(self.connection, "logical_sni", None),
                tls=self.connection.version(),
            )
        )
        location = None
        if self.path == "/relative":
            location = "/final"
        elif self.path == "/absolute":
            location = "https://second.test:" + str(self.server.server_port) + "/final"
        body = json.dumps(self.server.requests[-1]).encode()
        self.send_response(302 if location else 200)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
