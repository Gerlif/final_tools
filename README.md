# frameio-export-watcher

Overvåger eksportmapper på produktionsserveren og uploader færdigskrevne filer
til den tilsvarende mappe på Frame.io. Kører som en Docker-container på en
Synology NAS.

```
/volume1/FinalKlip/AktiveProjekter/2026/Beierholm/Kundecase #0711/Projektfiler/Eksport
                                   └── år ──┘ └─ kunde ─┘ └──── sag ─────┘

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

### 2. Konfigurér

```bash
cp config.example.yaml config.yaml
# tilret watch.export_template, frameio.project_template og frameio.folder_template
```

### 3. Byg og kør på NAS'en

```bash
export FRAMEIO_CLIENT_ID=...
export FRAMEIO_CLIENT_SECRET=...
docker compose up -d --build
docker compose logs -f
```

`PUID`/`PGID` i `docker-compose.yml` skal matche ejeren af sharet, ellers kan
containeren ikke læse filerne.

### 4. Tjek at mappingen rammer rigtigt

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
