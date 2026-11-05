# IceCatch

Icecast Stream Recorder – *Catch the stream. Keep the moment.* Web-käyttöliittymällä
varustettu nettiradiotallennin. Ei Python-riippuvuuksia: Python 3.9+, ffmpeg ja SQLite.
Tuotantokone on Raspberry Pi (ssh-alias `pi`), mutta ohjelma ei saa riippua Pi:stä.

Kielet: koodi, kommentit, lokit ja tiedostonimet englanniksi. Käyttöliittymä, käyttäjälle
näkyvät virheilmoitukset, asennusskriptien tulosteet ja dokumentaatio suomeksi.

Testit: `python3 -m unittest discover -s tests -t .`

## Asennus Pi:lle

Kun `icecatch/`-, `static/`-, `deploy/`- tai `scripts/`-kansion muutos on valmis ja testit
menevät läpi, käynnistä aina `pi-deploy`-agentti. Se testaa, asentaa ja tarkistaa tuloksen.
Älä asenna keskeneräistä muutosta. Palvelu saa käyttää pelkkää HTTP:tä, koska se näkyy
toistaiseksi vain sisäverkkoon.
