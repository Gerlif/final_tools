# Frame.io V4 — de endpoints værktøjet bruger

Kilde: <https://next.developer.frame.io/platform>. Tilføj `.md` til enhver
dokumentationsside for at få ren markdown.

## Autentificering

Adobe IMS server-to-server, `client_credentials`:

```
POST https://ims-na1.adobelogin.com/ims/token/v3
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=…&client_secret=…&scope=openid,AdobeID,frame.s2s.all
```

Access tokens lever ca. 1 time. Legacy developer tokens virker kun på konti, der
endnu ikke administreres via Adobe Admin Console, og kræver headeren
`x-frameio-legacy-token-auth: true`. Legacy-API'et lukker efter 2026-12-01.

## Endpoints

| Formål | Kald | Rate limit |
|--------|------|------------|
| Find konto | `GET /v4/accounts` | 100/min |
| Find projekt (og `root_folder_id`) | `GET /v4/accounts/{account_id}/projects` | 100/min |
| Gå ned gennem mapper | `GET /v4/accounts/{account_id}/folders/{folder_id}/children?type=folder` | 100/min |
| Opret fil-placeholder | `POST /v4/accounts/{account_id}/folders/{folder_id}/files/local_upload` | 5/sek |
| Upload chunk | `PUT <presigned S3 url>` | — |
| Bekræft upload | `GET /v4/accounts/{account_id}/files/{file_id}/status` | 5/sek |
| Ny version af eksisterende fil | `POST /v4/accounts/{account_id}/folders/{folder_id}/version_stacks` | 10/min |
| Læg fil i eksisterende stak | `PATCH /v4/accounts/{account_id}/files/{file_id}/move` | 100/min |
| Ryd op efter fejlet upload | `DELETE /v4/accounts/{account_id}/files/{file_id}` | 100/min |

## Upload-flowet

```jsonc
// 1) POST .../files/local_upload
{ "data": { "name": "spot.mp4", "file_size": 50645990 } }

// svar
{ "data": {
    "id": "…", "media_type": "video/mp4", "status": "created",
    "upload_urls": [ { "size": 16881997, "url": "https://…s3…/part_1" }, … ] } }
```

Hver chunk PUT'es i rækkefølge med præcis den `size`, Frame.io har afsat, og med
headerne:

* `Content-Type: <media_type fra svaret>` — skal matche, ellers afvises den
* `x-amz-acl: private`

En presigned URL udløber; fejler en chunk med `403`, skal hele filen oprettes
igen frem for at genbruge URL'en. Derfor sletter værktøjet placeholderen og
prøver forfra ved næste scanning.

## Version stacks

Frame.io kan ikke uploade direkte ind i en stak. Rækkefølgen er:

1. Upload den nye fil i samme mappe.
2. Findes der en **fil** med samme navn: `POST .../version_stacks` med
   `file_ids: [gammel, ny]` (ældst først).
3. Findes der allerede en **version stack**: `PATCH .../files/{ny}/move` med
   `parent_id` sat til stakkens id.
