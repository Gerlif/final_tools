# frameio-export-watcher

Overvåger eksportmapper på produktionsserveren og uploader færdigskrevne filer
til den tilsvarende mappe på Frame.io. Kører som en Docker-container på en
Synology NAS.

```
/volume1/AktiveProjekter/2026/Beierholm/Kundecase #0711/Projektfiler/Eksport
                         └år┘ └─kunde─┘ └─────sag─────┘

Frame.io:  projekt "2026"  →  mappe "Beierholm"  →  mappe "Kundecase #0711"
```

Findes der ikke et tilsvarende projekt eller en tilsvarende mappe på Frame.io,
uploades der ingenting. Værktøjet opretter **aldrig** mapper på Frame.io —
mappestrukturen ejes af jeres bot.

## Sådan virker det

1. **Scanner** — hvert minut (konfigurerbart) gennemgås kun de mapper, der kan
   matche stien i `watch.export_template`. Resten af arkivet røres ikke.
2. **Færdig-tjek** — en fil uploades først, når størrelse og ændringstidspunkt
   har stået stille over flere scanninger, og filen er ældre end
   `min_age_seconds`. Temp-filer (`.tmp`, `.part`, `~$…`, skjulte filer) springes
   over. Det fanger langsomme SMB-kopieringer, som inotify alene ikke gør.
3. **Mapping** — sti-felterne (`{year}`, `{client}`, `{case}`) sættes ind i
   `frameio.project_template` og `frameio.folder_template`, og der navigeres ned
   gennem Frame.io-mapperne. Resultatet caches, så API'et ikke oversvømmes.
4. **Upload** — Frame.io V4 lokal upload: filen oprettes med navn og størrelse,
   Frame.io svarer med presignede S3-URL'er, og hver chunk PUT'es med
   `x-amz-acl: private`. Til sidst pollet upload-status til bekræftelse.
5. **Versioner** — findes filnavnet allerede i målmappen, lægges den nye fil
   oven på som ny version (version stack) i stedet for at ligge ved siden af.
6. **Hukommelse** — hver fil skrives i en SQLite-database (sti, størrelse,
   mtime, status). En genstart uploader derfor aldrig noget igen. Ændres filen,
   betragtes den som en ny version og uploades igen.

Produktionsmappen monteres **read-only**. Værktøjet skriver aldrig til den.

## Kom i gang

### 1. Opret credentials

Brug Adobe IMS server-to-server (anbefalet):

1. Opret et projekt i [Adobe Developer Console](https://developer.adobe.com/console)
   med Frame.io API og OAuth **Server-to-Server**-credentials.
2. Noter `Client ID` og `Client Secret`. Scopes: `openid, AdobeID, frame.s2s.all`.
3. Sørg for at service-kontoen har adgang til de Frame.io-workspaces, der skal
   uploades til.

Har I stadig et gammelt developer token (kun muligt hvis kontoen ikke
administreres via Adobe Admin Console), kan I sætte `auth.mode: legacy` og
`FRAMEIO_LEGACY_TOKEN`. Det legacy-API lukker efter **1. december 2026**.

### 2. Hent koden ned på NAS'en

Slå SSH til under **Kontrolpanel → Terminal & SNMP → Aktivér SSH-tjeneste**, og
installer **Container Manager** (DSM 7.2+) eller **Docker** (ældre DSM) fra
Pakkecenter. Log så ind og hent koden:

```bash
ssh dinbruger@nas.lokal
sudo -i
mkdir -p /volume1/docker/frameio-export-watcher
cd /volume1/docker/frameio-export-watcher

# Har NAS'en git:
git clone -b claude/synology-frameio-uploader-yu9pw3 \
    https://github.com/Gerlif/final_tools.git .
```

Har den ikke git, så hent i stedet ZIP'en fra GitHub på din egen maskine og læg
indholdet i `/volume1/docker/frameio-export-watcher` med File Station.

### 3. Find det rigtige UID/GID

Containeren skal køre som en bruger, der kan læse produktionsmappen:

```bash
ls -ln /volume1/AktiveProjekter | head
# eller, hvis du kender brugeren der ejer filerne:
id dinbruger
```

Skriv de to tal ind som `PUID` og `PGID` i `docker-compose.yml`, og tjek samtidig
at stien i `volumes:` passer til jeres share (`/volume1/AktiveProjekter`).
Det er kun **venstre** side af kolonet, der er stien på NAS'en — højre side
(`/data/AktiveProjekter`) er stien inde i containeren, og den er den, som
`watch.root` i `config.yaml` peger på. Lad den stå.

Viser `ls -ldn` noget i stil med `d---------+`, har sharet **ingen**
POSIX-rettigheder, og adgangen styres alene af DSM's ACL'er (det er `+`'et).
Så bliver ethvert almindeligt UID afvist. To muligheder:

* **Hurtigt:** fjern kommentaren fra `#user: "0:0"` i `docker-compose.yml`, så
  containeren kører som root. Kræver ingen ombygning, og sharet er stadig
  monteret read-only.
* **Pænere:** giv en DSM-bruger læseadgang til sharet i **Kontrolpanel → Delt
  mappe → Rediger → Tilladelser**, find brugerens UID med `id brugernavn`, og
  sæt det som `PUID`. Test at det virker, før du bygger om:

  ```bash
  sudo -u '#1026' ls /volume1/AktiveProjekter    # udskift 1026 med dit UID
  ```

### 4. Læg credentials og config på plads

```bash
cp .env.example .env
vi .env               # indsæt FRAMEIO_CLIENT_ID og FRAMEIO_CLIENT_SECRET
chmod 600 .env

cp config.example.yaml config.yaml
vi config.yaml        # tilret export_template, project_template, folder_template
```

`config.yaml` **skal** eksistere som fil, før containeren startes — ellers opretter
Docker en tom *mappe* med det navn, og containeren fejler med en forvirrende
fejlbesked.

### 5. Byg og start

Find først ud af hvilken Compose I har:

```bash
docker compose version      # virker på DSM 7.2+ med Container Manager
docker-compose version      # virker med den ældre Docker-pakke
```

Den, der svarer med et versionsnummer, er den I skal bruge. Fejler den første
med `unknown shorthand flag: 'd'`, har I den ældre pakke, og **så skal alle
`docker compose`-kommandoer i denne README skrives med bindestreg**:
`docker-compose`.

```bash
docker compose up -d --build
docker compose logs -f
```

Containeren starter automatisk igen efter en genstart af NAS'en
(`restart: unless-stopped`), og læser credentials fra `.env` hver gang.

#### Container Manager i stedet for SSH

Vil I hellere bruge DSM's GUI: **Container Manager → Projekt → Opret → Angiv sti**
og peg på `/volume1/docker/frameio-export-watcher`. Den finder selv
`docker-compose.yml`, bygger imaget og starter containeren, og derefter har I
logs, status og genstart-knap i DSM.

Filerne skal stadig ligge på plads først (trin 2-4). De kan redigeres i File
Station med **Teksteditor**-pakken, hvis I ikke vil bruge SSH — men File Station
viser som udgangspunkt ikke filer, der starter med punktum. Vil I redigere
credentials i GUI'en, så kald filen `frameio.env` i stedet og ret linjen i
`docker-compose.yml` til `- frameio.env` (begge navne er gitignorerede).

Vælg **én** af de to til at eje containeren. Starter I den både med
`docker compose` og som Container Manager-projekt, ender I med to sæt
containere, der slås om den samme mappe.

### 6. Tjek at mappingen rammer rigtigt

```bash
# Credentials, konto og alle fundne eksportmapper med deres Frame.io-match
docker compose exec frameio-export-watcher python -m frameio_export_watcher doctor

# Én konkret mappe
docker compose exec frameio-export-watcher python -m frameio_export_watcher \
    resolve "/data/AktiveProjekter/2026/Beierholm/Kundecase #0711/Projektfiler/Eksport"

# Kør uden at uploade noget
docker compose exec -e DRY_RUN=true frameio-export-watcher \
    python -m frameio_export_watcher once
```

## Kommandoer

| Kommando  | Hvad den gør |
|-----------|--------------|
| `run`     | Overvåger løbende (containerens standard) |
| `once`    | Én scanning, venter på uploads, afslutter |
| `doctor`  | Tjekker login, konto og hvad hver eksportmappe matcher på Frame.io |
| `resolve` | Viser Frame.io-målet for én sti |
| `status`  | Viser hvad der er uploadet, sprunget over eller fejlet |
| `retry`   | Glemmer registrerede udfald, så filerne prøves igen |

Kom en Frame.io-mappe først til efter en fil blev sprunget over, kan den hentes
med:

```bash
docker compose exec frameio-export-watcher python -m frameio_export_watcher \
    retry --status no_match
```

## Konfiguration

Alt er dokumenteret i [`config.example.yaml`](config.example.yaml). De vigtigste:

| Nøgle | Betydning |
|-------|-----------|
| `watch.root` | Roden af produktionsmappen set inde fra containeren |
| `watch.export_template` | Stien fra roden ned til eksportmappen, med `{felter}` |
| `watch.recursive` | Tag også filer i undermapper (uploades fladt) |
| `watch.stability.*` | Hvornår en fil regnes som færdigskrevet |
| `frameio.project_template` | Frame.io-projektets navn, fx `{year}` |
| `frameio.folder_template` | Mappestien inde i projektet, fx `{client}/{case}` |
| `frameio.version_stack_on_duplicate` | Stak samme filnavn som ny version |
| `upload.max_concurrent_files` | Hvor mange filer der uploades samtidig |

Hemmeligheder kommer kun fra miljøvariable — aldrig fra YAML-filen:
`FRAMEIO_CLIENT_ID`, `FRAMEIO_CLIENT_SECRET`, `FRAMEIO_LEGACY_TOKEN` (alle
findes også som `…_FILE`, der peger på en fil med værdien).

## Drift

* **Sundhedstjek** — containeren skriver `/state/heartbeat` efter hver scanning;
  Dockers `HEALTHCHECK` markerer den som usund, hvis den står stille i 15 min.
* **Genforsøg** — en fejlet upload prøves igen med voksende ventetid (1 min → 1
  time) op til `upload.max_attempts`, hvorefter den markeres `given_up`.
* **Ufuldstændige uploads** — går en chunk galt, slettes den tomme fil på
  Frame.io igen, så der ikke ligger spøgelsesfiler.
* **Rate limits** — API-kald holdes under Frame.io's grænser (5 kald/sekund på
  de strammeste endpoints), og `429` respekteres med `Retry-After`.
* **Æ, Ø og Å** — mappenavne sammenlignes Unicode-normaliseret (NFC), så
  mac-klienters NFD-navne matcher Frame.io. Store/små bogstaver ignoreres som
  standard.

## Udvikling

```bash
pip install -e ".[dev]"
pytest
```

Testene kører mod et in-memory Frame.io (`tests/fakes.py`) — ingen netværk, ingen
credentials.
