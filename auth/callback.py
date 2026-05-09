"""OAuth2 redirect callback handler."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from config import load_config


REDIRECT_PORT = int(load_config().get("oauth", {}).get("redirect_port", 53214))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth2 redirect callback for all providers."""

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        self.server.auth_code = query.get("code", [None])[0]
        self.server.auth_error = query.get("error_description", [None])[0]

        if self.server.auth_code:
            body = b"<html><body><h2>Authenticated! You can close this tab.</h2></body></html>"
        else:
            msg = self.server.auth_error or "Unknown error"
            body = f"<html><body><h2>Error: {msg}</h2></body></html>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def wait_for_callback(port: int = REDIRECT_PORT) -> tuple[str | None, str | None]:
    """Start a one-shot HTTP server, wait for the OAuth callback, return (code, error)."""
    server = HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
    server.auth_code = None
    server.auth_error = None
    server.timeout = 120
    print(f"[voitta-desktop] Waiting for callback on port {port}...")
    server.handle_request()
    server.server_close()
    return server.auth_code, server.auth_error
