# frameio-export-watcher

Overvåger eksportmapper på produktionsserveren og uploader færdigskrevne filer
til den tilsvarende mappe på Frame.io. Kører som en Docker-container på en
Synology NAS.

```
/volume1/AktiveProjekter/2026/Beierholm/Kundecase #0711/Projektfiler/Eksport
                         └år┘ └─kunde─┘ └─────sag─────┘

Frame.io:  projekt "2026"  →  mappe "Beierholm"  →  mappe "Kundecase #0711"
```

Findes der ikke et tilsvarende projekt eller en tilsvarende sagsmappe på
Frame.io, uploades der ingenting. Værktøjet opretter **aldrig** projekt-,
kunde- eller sagsmapper — den struktur ejes af jeres bot.

Undermapper *inde i* eksportmappen spejles derimod automatisk, og oprettes hvis
de mangler:

```
Eksport/Hero/Fil.mp4   →   2026/Beierholm/Kundecase #0711/Hero/Fil.mp4
```

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
5. **Undermapper** — ligger filen i en undermappe under `Eksport`, oprettes den
   samme mappe på Frame.io under sagsmappen, og filen lægges deri.
6. **Versioner** — findes filnavnet allerede i målmappen, lægges den nye fil
   oven på som ny version (version stack) i stedet for at ligge ved siden af.
7. **Hukommelse** — hver fil skrives i en SQLite-database (sti, størrelse,
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

curl -L https://github.com/Gerlif/final_tools/archive/refs/heads/claude/synology-frameio-uploader-yu9pw3.tar.gz \
    | tar xz --strip-components=1
```

DSM leveres uden `git`, så ovenstående henter koden direkte. Vil I hellere have
git — se afsnittet [Opdatering](#opdatering).

### 3. Tjek stien og adgangen

Tjek at stien i `volumes:` i `docker-compose.yml` passer til jeres share:

```yaml
- /volume1/AktiveProjekter:/data/AktiveProjekter:ro
```

Det er kun **venstre** side af kolonet, der er stien på NAS'en — højre side
(`/data/AktiveProjekter`) er stien inde i containeren, og den er den, som
`watch.root` i `config.yaml` peger på. Lad den stå.

Containeren kører som standard som **root** (`user: "0:0"`), fordi
Synology-shares typisk slet ikke har POSIX-rettigheder:

```bash
ls -ldn /volume1/AktiveProjekter
# d---------+ 1 0 0 400 Aug 24 07:11 /volume1/AktiveProjekter
```

`d---------` betyder ingen rettigheder for nogen, og `+` betyder at adgangen
styres af DSM's ACL'er. Så findes der ikke noget almindeligt UID at køre som.
Mounten er read-only, så containeren kan under ingen omstændigheder skrive til
produktionsmappen.

Har jeres share derimod almindelige POSIX-rettigheder, kan I køre som en
ikke-privilegeret bruger: kommentér `user: "0:0"` ud, og sæt `PUID`/`PGID` til
tallene fra `ls -ldn`. Test at brugeren faktisk kan læse mappen, før I bygger om:

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

### 5. Byg imaget og sæt udgangspunktet

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
docker compose build
```

Ligger der allerede færdige eksporter i mapperne, skal de markeres som
historik, **før** overvågningen startes. Ellers bliver hele bagkataloget
uploadet ved anden scanning:

```bash
docker compose run --rm frameio-export-watcher baseline
```

Den svarer fx `marked 201 existing file(s) as already handled`. Fra nu af er det
kun filer, der bliver skrevet eller ændret herefter, der uploades. Vil I se hvad
den ville markere uden at gøre det:

```bash
docker compose run --rm -e DRY_RUN=true frameio-export-watcher baseline
```

Skifter I mening, kan bagkataloget frigives igen med
`retry --status baseline`.

### 6. Start

```bash
docker compose up -d
docker compose logs -f
```

Containeren starter automatisk igen efter en genstart af NAS'en
(`restart: unless-stopped`), og læser credentials fra `.env` hver gang.

#### Container Manager i stedet for SSH

Vil I hellere bruge DSM's GUI: **Container Manager → Projekt → Opret → Angiv sti**
og peg på `/volume1/docker/frameio-export-watcher`. Den finder selv
`docker-compose.yml`, bygger imaget og starter containeren, og derefter har I
logs, status og genstart-knap i DSM. Kør `baseline` fra trin 5 først — Container
Manager starter overvågningen med det samme.

Filerne skal stadig ligge på plads først (trin 2-4). De kan redigeres i File
Station med **Teksteditor**-pakken, hvis I ikke vil bruge SSH — men File Station
viser som udgangspunkt ikke filer, der starter med punktum. Vil I redigere
credentials i GUI'en, så kald filen `frameio.env` i stedet og ret linjen i
`docker-compose.yml` til `- frameio.env` (begge navne er gitignorerede).

Vælg **én** af de to til at eje containeren. Starter I den både med
`docker compose` og som Container Manager-projekt, ender I med to sæt
containere, der slås om den samme mappe.

### 7. Tjek at mappingen rammer rigtigt

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
| `baseline`| Markerer alt eksisterende som færdigbehandlet, uden at uploade |
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
| `watch.recursive` | Tag også filer i undermapper under `Eksport` |
| `frameio.create_subfolders` | Spejl de undermapper på Frame.io (ellers uploades fladt) |
| `watch.stability.*` | Hvornår en fil regnes som færdigskrevet |
| `frameio.project_template` | Frame.io-projektets navn, fx `{year}` |
| `frameio.folder_template` | Mappestien inde i projektet, fx `{client}/{case}` |
| `frameio.version_stack_on_duplicate` | Stak samme filnavn som ny version |
| `upload.max_concurrent_files` | Hvor mange filer der uploades samtidig |

Hemmeligheder kommer kun fra miljøvariable — aldrig fra YAML-filen:
`FRAMEIO_CLIENT_ID`, `FRAMEIO_CLIENT_SECRET`, `FRAMEIO_LEGACY_TOKEN` (alle
findes også som `…_FILE`, der peger på en fil med værdien).

## Opdatering

Har NAS'en `git`, er det bare:

```bash
cd /volume1/docker/frameio-export-watcher
git pull
docker compose up -d --build
```

Har den ikke, kan koden hentes direkte uden at installere noget:

```bash
cd /volume1/docker/frameio-export-watcher
curl -L https://github.com/Gerlif/final_tools/archive/refs/heads/claude/synology-frameio-uploader-yu9pw3.tar.gz \
    | tar xz --strip-components=1
docker compose up -d --build
```

`config.yaml` og `.env` ligger ikke i repoet og bliver derfor ikke rørt af nogen
af delene. `docker-compose.yml` bliver derimod overskrevet, så har I rettet
stien, `PUID`/`PGID` eller andet i den, skal det sættes igen bagefter.

Uploadhistorikken ligger i Docker-volumet `frameio-state`, ikke i mappen, så
den overlever både opdateringer og en helt frisk klon.

Vil I have `git` på NAS'en, installeres den via **Pakkecenter → Git Server**.
Pakken hedder "server", men det er den, der lægger `git`-kommandoen på plads,
så den kan bruges over SSH.

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
