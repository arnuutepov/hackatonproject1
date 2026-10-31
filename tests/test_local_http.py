from urllib.request import Request
import pytest
from support_copilot.local_http import open_local


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://127.0.0.1/', 'http://example.com/',
                                  'http://localhost/', 'http://user:pass@127.0.0.1/'])
def test_test_transport_never_resolves_external_urls(url):
    with pytest.raises(ValueError):
        open_local(Request(url))


def test_local_transport_does_not_follow_redirect_to_external_host():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from urllib.error import HTTPError
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', 'http://outside.example.invalid/')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Redirect)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as error:
            open_local(Request(f'http://127.0.0.1:{server.server_port}/'))
        assert error.value.code == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_review_import_rejects_xml_entities(tmp_path):
    from zipfile import ZipFile
    from support_copilot.review import read_xlsx
    path = tmp_path / 'malicious.xlsx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<!DOCTYPE workbook [<!ENTITY content "not a worksheet">]><workbook>&content;</workbook>')
    with pytest.raises(ValueError, match='Unsafe XML'):
        read_xlsx(path, 'Review', ['ticket_id'])
