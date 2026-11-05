# IceCatch

**Icecast Stream Recorder** · *Catch the stream. Keep the moment.*

IceCatch tallentaa nettiradio- ja Icecast-lähetyksiä ja kokoaa niistä arkiston, jota
käytetään selaimella. Se sopii esimerkiksi seurakunnan, yhdistyksen tai paikallisradion
lähetysten talteenottoon. Se toimii pienelläkin koneella, kuten Raspberry Pillä.

- **Tallentaa itsestään**, kun lähetys alkaa, ja lopettaa, kun se päättyy.
- **Ajastaa** säännölliset ohjelmat: viikonpäivät, kellonaika ja säilytysaika.
- **Poistaa hiljaisuuden**, jolloin tallenteeseen jää vain puhe ja musiikki.
- **Selainkäyttöliittymä**: kuuntele, lataa, merkitse suosikiksi ja poista. Näkymä
  päivittyy itsestään. Teemaksi voi valita vaalean, tumman tai järjestelmän mukaisen.
- **Kestää katkot**: lyhyt katko striimissä tai koneen uudelleenkäynnistys ei pilaa
  tallennetta.

> **Vain kotiverkkoon.** IceCatch ei ole tarkoitettu internetiin. Siinä ei ole
> tietoturvaa eikä käyttäjienhallintaa, ja liikenne kulkee salaamattomana. Kuka tahansa
> samassa verkossa voi kuunnella, muuttaa ja poistaa tallenteita. Katso
> [Vastuuvapaus](#vastuuvapaus).

## Asennus

### Raspberry Pi, Debian tai Ubuntu

Tarvitset koneen, jossa on systemd. Asennusskripti asentaa Pythonin ja ffmpegin itse.

```bash
git clone https://github.com/laurimaatta/icecatch-icecast-catcher.git icecatch
cd icecatch
sudo ./scripts/install.sh
```

Lopuksi skripti tulostaa osoitteen, esimerkiksi `http://192.168.1.20:8080`. Avaa se
selaimessa.

- **Päivitys:** aja sama komento uudelleen. Tallenteet ja asetukset säilyvät.
- **Asennus omalta koneelta ssh:lla:** `PI_HOST=pi ./deploy.sh`. Komento kopioi ohjelman
  koneelle `pi` ja asentaa sen.

### Muut koneet (macOS, Windows, muu Linux)

Asenna Python 3.9 tai uudempi sekä ffmpeg. Käynnistä sitten ohjelma tässä kansiossa:

```bash
python3 -m icecatch
```

Avaa selaimessa `http://localhost:8080`. Tallenteet menevät `data`-kansioon.

## Käyttö

1. **Striimit:** lisää lähetyksen osoite (Icecast-mount, `.pls` tai `.m3u`) ja tarkista
   se *Testaa*-napilla. Valitse *Tallenna aina kun lähetys alkaa*, jos haluat
   automaattisen tallennuksen.
2. **Aikataulut:** lisää säännöllinen ohjelma ja valitse sille päivät, alkamisaika, kesto
   ja säilytysaika.
3. **Tallenteet:** kuuntele, lataa tai poista. ★-suosikkeja ei poisteta koskaan
   automaattisesti.
4. **Tila:** näet levytilan, käynnissä olevat tallennukset ja seuraavat ajastukset.

Tallennuksen voi aloittaa myös heti Striimit-sivun *Tallenna nyt* -napista.

## Asetukset

Asetukset ovat tiedostossa `/etc/icecatch.env`. Muutosten jälkeen käynnistä palvelu
uudelleen: `sudo systemctl restart icecatch`.

| Asetus | Oletus | Merkitys |
|---|---|---|
| `ICECATCH_DATA_DIR` | `/var/lib/icecatch` | Tallenteiden ja tietokannan paikka |
| `ICECATCH_PORT` | `8080` | Käyttöliittymän portti |
| `ICECATCH_USER`, `ICECATCH_PASS` | tyhjä | Kirjautuminen käyttöliittymään |
| `ICECATCH_FORMAT`, `ICECATCH_BITRATE` | `mp3`, `160k` | Tallenteiden muoto (`mp3` tai `opus`) |
| `ICECATCH_MIN_FREE_MB` | `1024` | Levytila, joka jätetään aina vapaaksi |

**Erillinen levy (suositus Raspberry Pille):** SD-kortti kuluu ja täyttyy. Liitä levy
esimerkiksi hakemistoon `/mnt/icecatch` ja aseta `ICECATCH_DATA_DIR=/mnt/icecatch`. Aja
sen jälkeen asennus uudelleen.

## Hyvä tietää

- **Automaattinen tallennus** alkaa, kun striimi tulee saataville. Se päättyy, kun
  striimi on ollut poissa 3 minuuttia tai kun striimin enimmäispituus (oletus 4 h)
  täyttyy.
- **Hiljaisuus:** yli 30 sekunnin hiljaiset kohdat poistetaan. Yksittäiset äännähdykset
  ja hiljainen taustahäly lasketaan hiljaisuudeksi. Jos taustahälyä jää tallenteeseen,
  nosta striimin asetuksista hiljaisuuden kynnystä.
- **Levytila:** jos tila ei riitä, tallennus jää tekemättä tai pysähtyy, ja siitä näkyy
  ilmoitus. Tallenteita ei koskaan poisteta tilan vuoksi.
- **Tietoturva:** IceCatch on tehty luotettuun kotiverkkoon. Älä avaa sitä internetiin
  äläkä julkiseen verkkoon. Valinnainen salasana (`ICECATCH_USER`, `ICECATCH_PASS`) on
  yksi yhteinen tunnus, joka kulkee verkossa salaamattomana. Se estää vain vahingossa
  tapahtuvaa käyttöä, ei tahallista.
- **Lokit:** `journalctl -u icecatch -f`
- **Poisto:** `sudo systemctl disable --now icecatch`, minkä jälkeen poista `/opt/icecatch`,
  `/etc/icecatch.env`, `/etc/systemd/system/icecatch.service*` ja halutessasi tallenteet
  (`/var/lib/icecatch`).

Vanha *Stream-arkisto*-asennus siirtyy IceCatchiksi automaattisesti, kun asennusskripti
ajetaan. Asetukset, tallenteet ja tietokanta säilyvät.

## Kehittäjälle

```bash
ICECATCH_DATA_DIR=./data ICECATCH_PORT=8090 python3 -m icecatch   # kehityspalvelin
python3 -m unittest discover -s tests -t .                        # testit
```

Ohjelma ei tarvitse Python-paketteja: pelkkä vakiokirjasto, ffmpeg ja SQLite riittävät.
Koodi on englanniksi ja käyttöliittymä suomeksi.

```
icecatch/     ohjelma: server.py (HTTP + API), recorder.py (tallennus ja ajastus),
              processing.py (hiljaisuuden poisto, koodaus), detect.py, db.py, events.py
static/       käyttöliittymä (HTML, CSS, JS)
scripts/      install.sh, prepare-pi.sh (tyhjentää Pi:n konteista, kysyy ensin)
deploy/       systemd-palvelu ja asetuspohja
deploy.sh     asennus toiselle koneelle ssh:lla
tests/        testit
```

## Lisenssi

IceCatch on täysin vapaa ohjelma, ja se on luovutettu public domainiin
([The Unlicense](LICENSE)). Saat käyttää, muuttaa, kopioida, jakaa ja myydä sitä mihin
tahansa tarkoitukseen ilman ehtoja tai mainintaa tekijästä.

IceCatch käyttää ffmpegiä, Pythonia ja SQLiteä, mutta ei sisällä niitä. Ne asennetaan
erikseen, ja niihin sovelletaan niiden omia lisenssejä.

## Vastuuvapaus

Ohjelma toimitetaan sellaisenaan (*as is*), ilman minkäänlaista takuuta. Tekijät eivät
vastaa mistään vahingoista, jotka johtuvat ohjelman käytöstä tai siitä, ettei sitä voi
käyttää. Tällaisia ovat esimerkiksi tallentamatta jääneet tai kadonneet lähetykset,
poistuneet tiedostot, täyttynyt levy ja tietoturvaongelmat.

- **Ei tietoturvaa:** ohjelmassa ei ole käyttäjienhallintaa, oikeuksia, salausta eikä
  suojausta hyökkäyksiä vastaan. Käytä sitä vain verkossa, jonka kaikkiin laitteisiin ja
  käyttäjiin luotat.
- **Tallenteet:** varmista tärkeät tallenteet itse. Ohjelma poistaa tallenteita
  aikataulun säilytysajan mukaan ja aina, kun käyttäjä niin pyytää.
- **Tekijänoikeudet ja yksityisyys:** käyttäjä vastaa siitä, että hänellä on oikeus
  tallentaa, säilyttää ja jakaa tallentamiaan lähetyksiä. Lähetyksissä voi olla
  tekijänoikeuksin suojattua materiaalia tai henkilötietoja.
