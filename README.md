# Stream-arkisto

Tallentaa Icecast-nettiradiostriimejä aikataulun mukaan (tai heti napista), poistaa
pitkät hiljaiset kohdat ja tarjoaa web-käyttöliittymän, jossa tallenteita voi
kuunnella, hakea, muokata ja poistaa.

- **Ei Python-riippuvuuksia**: pelkkä Python 3 + ffmpeg + SQLite.
- Tallennus kestää striimin katkokset (ffmpeg yhdistää uudelleen, ja jos yhteys
  kaatuu, aloitetaan uusi segmentti; segmentit yhdistetään lopuksi).
- Palvelun uudelleenkäynnistys kesken tallennuksen ei katkaise sitä: tallennus
  jatkuu ja osat yhdistetään.
- Hiljaisuuden poisto on kaksivaiheinen (`silencedetect` → tarkat leikkaukset), joten
  se toimii samoin kaikilla ffmpeg-versioilla.

## Asennus Pi:lle

Omalta koneelta, tästä hakemistosta (ssh-alias `pi`):

```bash
./deploy.sh --siivoa   # ensimmäisellä kerralla: poistaa kaikki kontit ja Dockerin, sitten asentaa
./deploy.sh            # myöhemmät päivitykset
```

`--siivoa` listaa kontit ja kysyy vahvistuksen ennen poistoa.
Asennus tekee seuraavat:

- asentaa ffmpegin
- asettaa aikavyöhykkeeksi Europe/Helsinki (aikataulut ovat paikallista aikaa)
- luo `arkisto`-käyttäjän ja systemd-palvelun `stream-arkisto`
- tulostaa osoitteen, esim. `http://<pi:n-ip>:8080`

Asetukset: `/etc/stream-arkisto.env` (data-hakemisto, portti, salasana, formaatti).
Muutosten jälkeen `sudo systemctl restart stream-arkisto`.

### USB-levy tallenteille (suositus)

SD-kortti kuluu ja täyttyy. Liitä levy esim. `/mnt/arkisto` (fstab, `nofail`) ja aseta
`ARKISTO_DATA_DIR=/mnt/arkisto` env-tiedostoon. Anna oikeudet:
`sudo chown arkisto:arkisto /mnt/arkisto`.

## Käyttö

1. **Striimit**: lisää Icecast-osoite (myös `.pls`/`.m3u` käy) → *Testaa* näyttää koodekin ja aseman nimen.
   Samassa näkymässä säädetään hiljaisuuden poisto (oletus: yli 3 s hiljaisuudet −50 dB:n alapuolella lyhennetään ~1 sekuntiin).
2. **Aikataulut**: ohjelman nimi, viikonpäivät, aloitusaika, kesto ja säilytysaika
   (tyhjä = säilytetään aina). Yli puolenyön menevät ohjelmat toimivat.
3. **Tallenteet**: haku, suodatus, toisto (−30/+30 s, nopeus, kohta muistetaan), lataus,
   otsikon/kuvauksen/tagien muokkaus, ★-suojaus automaattiselta poistolta, poisto.
4. **Tila**: levytila, käynnissä olevat ja seuraavat tallennukset.

## Rakenne

```
arkisto/
  config.py      asetukset ympäristömuuttujista
  db.py          SQLite-skeema (streams, schedules, recordings)
  recorder.py    ffmpeg-tallennus, ajastin, käsittelyjono, säilytysajan siivous
  processing.py  segmenttien yhdistys, hiljaisuuden tunnistus ja poisto, koodaus
  server.py      HTTP-palvelin: JSON-API, staattiset tiedostot, äänen Range-toisto
static/          web-käyttöliittymä (HTML/CSS/JS, ei riippuvuuksia)
scripts/         install.sh, valmistele-pi.sh (ajetaan Pi:llä)
deploy/          systemd-yksikkö ja env-pohja
deploy.sh        asennus omalta koneelta
```

Tiedostot: `DATA/recordings/VVVV/KK/VVVV-KK-PP_HHMM_otsikko_id.mp3`, tietokanta `DATA/arkisto.db`.

## Paikallinen kehitys

```bash
ARKISTO_DATA_DIR=./data ARKISTO_PORT=8090 python3 -m arkisto
```

## API lyhyesti

| | |
|---|---|
| `GET/POST /api/streams`, `PUT/DELETE /api/streams/{id}` | striimit |
| `POST /api/streams/test` `{url}` | striimin testaus |
| `POST /api/streams/{id}/record` `{duration_min, title?}` | tallenna heti |
| `GET/POST /api/schedules`, `PUT/DELETE /api/schedules/{id}` | aikataulut |
| `GET /api/recordings?q=&stream_id=&status=&limit=&offset=` | tallenteet |
| `PUT/DELETE /api/recordings/{id}`, `POST …/stop`, `POST …/reprocess` | muokkaus, poisto, pysäytys |
| `GET /media/{id}` (`?download=1`) | äänitiedosto (Range-tuki) |
| `GET /api/status` | tila |
