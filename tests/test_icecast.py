import unittest

from stream_arkisto.icecast import normalize_base, parse_status_html, parse_status_json

HTML_24 = """<html><body><div class="main">
<div class="roundbox"><div class="mounthead"><h3 class="mount">Mount Point /oulu</h3>
<div class="right"><ul class="mountlist"><li><a class="play" href="/oulu.m3u">M3U</a></li></ul></div></div>
<div class="mountcont"><table class="yellowkeys"><tbody>
<tr><td>Stream Name:</td><td class="streamstats">Oulun rauhanyhdistys</td></tr>
<tr><td>Stream Description:</td><td class="streamstats">Seurat &amp; tilaisuudet</td></tr>
<tr><td>Content Type:</td><td class="streamstats">audio/mpeg</td></tr>
<tr><td>Stream started:</td><td class="streamstats">Mon, 02 Nov 2026 18:00:01 +0200</td></tr>
<tr><td>Bitrate:</td><td class="streamstats">64</td></tr>
<tr><td>Listeners (current):</td><td class="streamstats">3</td></tr>
<tr><td>Listeners (peak):</td><td class="streamstats">12</td></tr>
<tr><td>Genre:</td><td class="streamstats">Hengellinen</td></tr>
<tr><td>Stream URL:</td><td class="streamstats"><a href="http://srk.fi">http://srk.fi</a></td></tr>
<tr><td>Currently playing:</td><td class="streamstats">Laulu 123</td></tr>
</tbody></table></div></div>
<div class="roundbox"><div class="mounthead"><h3 class="mount">Mount Point /hiljainen</h3></div>
<div class="mountcont"><table class="yellowkeys"><tbody>
<tr><td>Stream Name:</td><td class="streamstats">Testi</td></tr>
<tr><td>Content Type:</td><td class="streamstats">application/ogg</td></tr>
<tr><td>Listeners (current):</td><td class="streamstats">0</td></tr>
</tbody></table></div></div></div></body></html>"""

HTML_25 = """<html><body><main>
<section class="box"><h3 class="box_title">Mount Point <code>/radio</code></h3>
<table class="table-keys"><tbody>
<tr><td>Stream Name:</td><td>Radio X</td></tr>
<tr><td>Content Type:</td><td>audio/aac</td></tr>
<tr><td>Bitrate:</td><td>128 kbit/s</td></tr>
<tr><td>Listeners (current):</td><td>7</td></tr>
</tbody></table></section></main></body></html>"""

JSON_ONE = """{"icestats":{"admin":"x","host":"localhost","server_id":"Icecast 2.4.4",
"source":{"listenurl":"http://localhost:8000/oulu","server_name":"Oulun ry",
"server_type":"audio/mpeg","bitrate":64,"listeners":2,"listener_peak":5,
"genre":"various","title":"","dummy":null}}}"""

JSON_MANY_BROKEN = """{"icestats":{"source":[
{"listenurl":"http://10.0.0.5:8000/a","server_name":"A","audio_info":"channels=2;samplerate=44100;bitrate=96","title": -},
{"listenurl":"http://10.0.0.5:8000/b.ogg","server_name":"B","ice-bitrate":"48","artist":"X","title":"Y"}]}}"""


class T(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_base("https://h:8443/status.xsl"), "https://h:8443")
        self.assertEqual(normalize_base("h:8090/"), "http://h:8090")
        self.assertEqual(normalize_base("https://h/ice/status-json.xsl"), "https://h/ice")

    def test_html_24(self):
        m = parse_status_html(HTML_24, "https://h:8443")
        self.assertEqual([x.mount for x in m], ["/oulu", "/hiljainen"])
        o = m[0]
        self.assertEqual(o.url, "https://h:8443/oulu")
        self.assertEqual(o.name, "Oulun rauhanyhdistys")
        self.assertEqual(o.description, "Seurat & tilaisuudet")
        self.assertEqual((o.bitrate, o.listeners, o.listener_peak), (64, 3, 12))
        self.assertEqual(o.title, "Laulu 123")
        self.assertEqual(m[1].listeners, 0)

    def test_html_25(self):
        m = parse_status_html(HTML_25, "https://h")
        self.assertEqual(len(m), 1)
        self.assertEqual((m[0].mount, m[0].name, m[0].bitrate), ("/radio", "Radio X", 128))

    def test_json_single_uses_public_base(self):
        m = parse_status_json(JSON_ONE, "https://pub:8443")
        self.assertEqual(m[0].url, "https://pub:8443/oulu")
        self.assertEqual(m[0].listeners, 2)

    def test_json_list_and_broken(self):
        m = parse_status_json(JSON_MANY_BROKEN, "http://h")
        self.assertEqual([x.mount for x in m], ["/a", "/b.ogg"])
        self.assertEqual(m[1].bitrate, 48)
        self.assertEqual(m[1].title, "X - Y")


if __name__ == "__main__":
    unittest.main()
