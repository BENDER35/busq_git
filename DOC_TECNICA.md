# Documentación técnica — `ghrepos.py`

Explicación detallada, función por función, del programa `ghrepos.py`: cómo se
estructura, qué hace cada pieza, qué endpoints de GitHub consume y por qué está
diseñado así. Las referencias a código usan el formato `archivo:línea`.

---

## 1. Visión general

`ghrepos.py` es un cliente de **la API REST de GitHub v3** (específicamente la
*Search API* y el *Repository API*) que hace tres cosas:

1. **Buscar** repositorios (`search`) con los calificadores del buscador de GitHub.
   Tras mostrar la tabla, si hay terminal interactiva ofrece **descargar el código
   de todos los resultados** y después **todas las releases** de cada uno.
2. **Descargar** repositorios (`download`) por nombre o por URL, en cuatro modos:
   código fuente (`tarball`, `zip`, `clone`) o binarios publicados en las
   releases (`release`). `--all-releases` baja todas las releases de cada repo.
3. **Inspeccionar** un repositorio (`show`) mostrando sus metadatos.

Está pensado para ejecutarse en terminales y pipelines, por lo que:
- **No tiene dependencias** fuera de la librería estándar de Python.
- Separa el **datos** (stdout) del **progreso** (stderr) para poder usarse con `jq`, `xargs` y tuberías.
- Lee el token de autenticación **solo de variables de entorno**, nunca de archivos.
- Valida las rutas durante la extracción de archivos para evitar ataques de *path traversal*.

### Flujo de alto nivel

```
 LÍNEA DE COMANDOS
        │  ./ghrepos.py search "scrapy" -l python --download 3
        ▼
 build_parser() ──► argparse (3 subcomandos, argumentos reutilizados)
        │
        ▼
     main()  ──► args.func(args)   (cmd_search / cmd_download / cmd_show)
        │
        ├─── search  ──► build_query() ─► search_repos() ─► [tabla | JSON]
        │                                        │
        │                                        └─► (TTY) ask_yes_no("¿código?")
        │                                              └─► download_many() ─► download_repo()
        │                                        └─► (TTY) ask_yes_no("¿releases?")
        │                                              └─► download_all_releases_many()
        │                                                    └─► list_releases() ─► download_all_releases()
        ├─── download ─► resolve_repo() ─► download_repo()  ──► ┌─ clone ──► subprocess git clone
        │                                                        ├─ tarball/zip ─► archive_repo() ─► _extract()
        │                                                        └─ release ─► fetch_release() ─► download_release() ─► fetch_asset()
        └─── show ────► resolve_repo() ─► api_get("/repos/{full_name}") ─► [tabla | JSON]
                   ▲
                   └──────────────── api_get() ── cliente HTTP + tratamiento de errores + rate limits
```

---

## 2. Estructura del código

| Rango | Función / bloque | Responsabilidad |
|---|---|---|
| `:1–19` | cabecera e imports | solo stdlib; `tarfile`/`zipfile` para extracción, `fnmatch`/`re` para releases |
| `:21–22` | constantes | `API`, cabecera `User-Agent` |
| `:25–34` | `auth_headers` | construye las cabeceras HTTP y añade el token |
| `:37–64` | `api_get` | cliente GET JSON + tratamiento de errores HTTP/red |
| `:67–74` | `search_repos` | llama al endpoint `/search/repositories` |
| `:77–83` | `fmt_size` | formatea el tamaño (KB → MB/GB) legible |
| `:86–100` | `print_results` | pinta la tabla de resultados |
| `:103–109` | `prompt_enabled` | decide si se puede preguntar (TTY, sin JSON ni `--no-prompt`) |
| `:112–121` | `ask_yes_no` | pregunta por terminal con respuesta `s`/`n` |
| `:124–151` | `cmd_search` | busca, muestra resultados y encadena prompts/descargas |
| `:154–170` | `build_query` | convierte flags en calificadores de búsqueda |
| `:173–185` | `resolve_repo` | normaliza nombres/URLs a `owner/repo` |
| `:188–198` | `code_args` | copia de `args` que fuerza código (nunca `release`) |
| `:201–204` | `download_many` | descarga los resultados y resume |
| `:207–227` | `download_repo` | calcula ruta, idempotencia y despacha por modo |
| `:230–242` | `clone_repo` | ejecuta `git clone` vía `subprocess` |
| `:245–266` | `archive_repo` | descarga tarball/zipball y lo extrae |
| `:269–273` | `resolve_branch` | averigua la rama por defecto |
| `:276–280` | `_safe_target` | garantiza que la extracción no salga del destino |
| `:283–285` | `_strip_root` | elimina la carpeta raíz `owner-hash/` de cada entrada |
| `:288–309` | `_extract` | descomprime de forma segura zip/tar |
| `:312–323` | `_StripAuthRedirect` / `_ASSET_OPENER` | descarta `Authorization` al cambiar de host |
| `:326–340` | `fetch_release` | elige la release (la última o la del `--tag`) |
| `:343–355` | `list_releases` | pagina y devuelve todas las releases publicadas |
| `:358–360` | `safe_name` | convierte el tag en un nombre de carpeta seguro |
| `:363–376` | `download_release` | filtra los assets y baja cada uno |
| `:379–417` | `download_all_releases` | baja todas las releases de un repo, tolerando fallos |
| `:420–428` | `download_all_releases_many` | repite lo anterior para todos los resultados |
| `:431–450` | `fetch_asset` | descarga en streaming un asset a un temporal y lo renombra |
| `:453–458` | `cmd_download` | resuelve cada argumento y descarga |
| `:461–475` | `create_github_repo` | crea un nuevo repositorio en GitHub |
| `:476–488` | `upload_release_github` | sube una release a GitHub (versión simplificada) |
| `:489–501` | `copy_to_gitlab` | indica la intención de copiar un repositorio a GitLab |
| `:481–488` | `cmd_show` | muestra/depura los metadatos de un repo |
| `:484–541` | `build_parser` | define la CLI con `argparse` |
| `:544–559` | `main` | punto de entrada; valida `--limit` y avisos de flags |

---

## 3. Entrada y CLI (`build_parser`, `main`)

`build_parser` (`:484–541`) usa `argparse` con dos técnicas de reutilización:

1. **Subparsers** (`:489`): un parser raíz sin acción propia y tres hijas
   (`search`, `download`, `show`) que son mutuamente excluyentes. Cada hijo
   registra su manejador en `set_defaults(func=...)` (`:529`, `:534`, `:539`),
   permitiendo que `main` ejecute simplemente `args.func(args)` (`:559`).
2. **Parsers compartidos con `parents=`**: hay dos parsers auxiliares sin ayuda
   propia, `common` (`:491–503`) y `dl` (`:505–516`), heredados por varias
   subcomandos. Así `--mode`, `--dest`, `--branch`, `--depth`, `--tag` y
   `--asset` están disponibles tanto en `search` como en `download` sin duplicar
   su definición — si mañana cambiase un flag de descarga, solo se toca en un
   sitio.

`search` añade además los flags del flujo interactivo (`:520–529`):

| Flag | Efecto |
|---|---|
| `--download N` | baja el código de los N primeros resultados |
| `--download-all` | baja el código de **todos** los resultados |
| `--all-releases` | baja **todas** las releases de cada resultado |
| `--no-prompt` | desactiva las preguntas interactivas |

`main` (`:544–559`) hace la única normalización global: acota `--limit` al rango
legal de la Search API (1–100) con `max(1, min(limit, 100))` (`:548`), porque el
servidor responde con error si se piden más de 100 por página. Además avisa de
flags que no aplican al modo elegido (`--tag` fuera de `--mode release`, `--asset`
fuera de `--mode release`/`--all-releases`, y `--branch` dentro de `release`,
`:549–558`).

---

## 4. Capa de cliente HTTP (`auth_headers`, `api_get`)

### Cabeceras (`:25–34`)

`auth_headers` construye la petición con:

- `User-Agent: ghrepos-script` — **obligatorio** en la API de GitHub; sin él
  rechaza la petición con 403.
- `Accept: application/vnd.github+json` — media type oficial de la v3.
- `X-GitHub-Api-Version: 2022-11-28` — pin de la versión de la API.
- `Authorization: Bearer <token>` — solo si existe `GITHUB_TOKEN` o `GH_TOKEN`
  en el entorno (`:31–33`). El token es *sensible*: se lee al vuelo, no queda en
  ningún log, y la cabecera solo existe en memoria durante la llamada.

### Petición y errores (`:37–64`)

`api_get(path, params, missing_ok)` es el cliente GET genérico. Concatena la
ruta con los parámetros URL-encodados (`:38–40`), ejecuta la llamada con timeout
de 30 s (`:43`) y devuelve el JSON parseado. Toda la gestión de errores converge
aquí:

| Excepción | Código | Acción |
|---|---|---|
| `HTTPError`, 404 y `missing_ok=True` | `:46–47` | devuelve `None` (lo usa `fetch_release` para tags inexistentes) |
| `HTTPError`, 403 y mensaje con "rate limit" | `:53–57` | imprime pista para exportar `GITHUB_TOKEN` |
| `HTTPError`, 401 | `:58–59` | avisa de token inválido/sin permisos |
| `HTTPError`, otros | `:60–61` | muestra código y mensaje de la API |
| `URLError` | `:63–64` | sin red / DNS / proxy: mensaje con el motivo |

Fíjate en que el mensaje de error se extrae del **cuerpo JSON** de la respuesta
(`:48–52`), no del texto genérico del servidor, porque GitHub devuelve descripciones
útiles (`"API rate limit exceeded..."`) en el campo `message`.

El parámetro `missing_ok` existe para que un 404 sea un resultado esperado y no
un error fatal: `fetch_release` lo usa para distinguir "ese tag no tiene
release" de un fallo real de la API.

---

## 5. Búsqueda (`build_query`, `search_repos`)

### De flags a calificadores (`:154–170`)

GitHub Search entiende *calificadores* (`qualifiers`). `build_query` traduce los
flags de la CLI a una cadena de búsqueda:

| Flag del script | Calificador generado |
|---|---|
| `-l python` | `language:python` |
| `--min-stars 1000` | `stars:>=1000` |
| `--created 2024-01-01` | `created:>=2024-01-01` |
| `--pushed 2024-01-01` | `pushed:>=2024-01-01` |
| `--archived exclude` | `archived:false` (por defecto) |
| `--archived only` | `archived:true` |
| `--archived any` | *(no añade calificador; se omiten los archivados de la busqueda)* |

Los calificadores se unen con espacios y la consulta acaba siendo algo como
`web scraping language:python stars:>=1000`. Si el usuario no da ni texto ni
filtros, `build_query` aborta con un mensaje claro (`:168–169`) en lugar de
disparar una búsqueda vacía.

### El endpoint (`:67–74`)

`search_repos` llama a `GET /search/repositories` con `q`, `sort`, `order` y
`per_page` (acotado a `--limit`). Dos detalles sutiles:

- **La paginación es falsa**: pide `page=1` y trunca con `items[:limit]`
  (`:73–74`). Para listas de hasta 100 resultados (el máximo de `--limit`) con
  una sola página es suficiente y evita consumo extra del *rate limit*.
- La Search API no permite pedir más de **100** resultados por página, por eso
  `per_page = min(limit, 100)` (`:68`).

### Formateo y presentación

- `fmt_size` (`:77–83`): la API da el tamaño en **kilobytes**; la función lo pasa
  a B/KB/MB/GB iterando unidades, con un decimal para tamaños grandes. También
  se usa para el peso de cada asset en `download_release`.
- `print_results` (`:86–100`): pinta una tabla con índice, `full_name`, estrellas,
  tamaño y fecha de última actividad. La manera de alinear columnas son los
  `f"{...:>3}"` / `f"{name:<45}"` de los format strings.

---

## 6. División de flujos de salida

El script cuida la **separación stdout/stderr**, clave para usarlo en tuberías:

- `stdout` = solo datos (tabla de `search`, JSON de `--json`, resumen de `show`).
- `stderr` = progreso (`Descargando ...`, `Omitido ...`, la lista de assets de
  `download_release`, `git clone` redirigido con `stdout=sys.stderr` en `:240`,
  resumen `N/M repositorio(s)` en `:204`, resumen de releases en `:426`).

Por eso este pipeline funciona sin que el JSON se contamine:

```bash
./ghrepos.py search "ia" -l rust --json | jq -r '.[].full_name'
```

---

## 7. Resolución de identificadores (`resolve_repo`)

`resolve_repo` (`:173–185`) normaliza cualquier entrada a la forma canónica
`owner/repo`. Acepta seis formatos:

| Entrada | Resultado |
|---|---|
| `octocat/Hello-World` | `octocat/Hello-World` |
| `https://github.com/octocat/Hello-World` | `octocat/Hello-World` |
| `https://github.com/octocat/Hello-World.git` | `octocat/Hello-World` |
| `https://github.com/octocat/Hello-World/tree/main` | `octocat/Hello-World` |
| `git@github.com:octocat/Hello-World.git` | `octocat/Hello-World` |
| `octocat/Hello-World/` | `octocat/Hello-World` |

Mecánica: si empieza por URL/SSH se limpia (`urlparse` para sacar la ruta del
host en `:180`, o `split(":", 1)[1]` para el formato scp de `git@` en `:178`) y
se descarta `.git`. Después se toman **los dos primeros segmentos** (`parts[0]` /
`parts[1]`, `:185`), ignorando el resto de la ruta (rama, subcarpetas...). Si no
hay dos segmentos, error de uso (`:183–184`).

---

## 8. Motor de descarga

### Ruta de destino e idempotencia (`download_repo`, `:207–227`)

Cada repo cae en `DEST/propietario/nombre` (`:209`), es decir, estructura
enigmática tipo `./repos/octocat/Hello-World`; en `--mode release` se añade el tag
como subcarpeta (`:211–213`), p. ej. `./repos/jqlang/jq/jq-1.7.1/`. Antes de
descargar se comprueba si el directorio de destino **ya existe y no está vacío**
(`:216`); en ese caso se salta con un aviso a `stderr`. Esto hace a
`download_repo` **idempotente**: es seguro re-ejecutar un lote sin multiplicar
descargas. Como en modo release el destino depende del tag, la comprobación se
hace *después* de consultar la release, de modo que bajar dos tags distintos del
mismo repo convive sin pisarse y consumiendo una llamada de API extra por repo
ya descargado.

Devuelve `bool` (descargado o omitido) para que `download_many` (`:201–204`) pueda
resumir con `done/N repositorio(s)`.

`download_repo` despacha por modo (`:220–226`):

### Modo `clone` → `clone_repo` (`:230–242`)

Delega directamente en `git`:

```python
git clone [--depth N] [--branch RAMA] https://github.com/owner/repo.git DEST
```

- `<git>` se detecta con `shutil.which` (`:231`); si falta, el propio error
  sugiere usar `--mode tarball`.
- `--depth args.depth`: `1` por defecto (*shallow clone*, un solo commit);
  `0` es falsy, así que **omite** el flag y clona el historial completo (`:236`).
- `subprocess.run` con `stdout=sys.stderr` (`:240`) para no ensuciar el stdout.
- Un `returncode` distinto de 0 (rama inexistente, red, permisos...) aborta con
  exit code 1 (`:241–242`).

### Modos `tarball` / `zip` → `archive_repo` (`:245–266`)

Descarga el repositorio como archivo comprimido vía la API:

- El nombre del endpoint depende del modo: `zipball` para zip, `tarball` para tar
  (`:246`).
- La **rama** se resuelve con `resolve_branch` a menos que el usuario dé `--branch`
  (`:247`); el ref se URL-encoda con `safe=""` porque puede contener `/`.
- El endpoint de la API responde con un **redirect 302 a `codeload.github.com`**;
  `urllib` sigue los redirects automáticamente y acabamos con los bytes del archivo
  en memoria (`:252–253`).
- `dest.mkdir(parents=True)` (`:260`) crea la jerarquía, y los bytes se guardan en
  un **archivo temporal** `DEST/.../nombre.download` (`:261`) antes de extraer.
- El `try/finally` (`:250–266`) garantiza que el temporal se borre **siempre**,
  incluso si la extracción lanza una excepción; y si la descarga falla con 404,
  se añade una pista orientativa sobre la rama (`:255–258`).

### Modo `release` → `fetch_release` + `download_release` + `fetch_asset`

Es el único modo que no baja el código fuente: baja los **archivos ya compilados**
que los desarrolladores suben como *assets* de una release.

- **Elegir la release** (`fetch_release`, `:273–287`): sin `--tag` se pide
  `GET /repos/{owner}/{repo}/releases?per_page=100` y se recorre la lista —que
  viene ordenada por fecha— tomando el primero que no sea *draft* ni
  *prerelease*; si el repo solo tiene prereleases, se acepta el primero que no
  sea draft. Con `--tag` se usa el endpoint puntual
  `GET /repos/{owner}/{repo}/releases/tags/{tag}` con `missing_ok=True`
  (`:275–279`), de forma que un tag inexistente es un error claro y no un
  traceback. Un repo sin releases aborta con mensaje propio (`:287`).
- **Nombre de carpeta** (`safe_name`, `:290–292`): los tags pueden contener `/`
  y otros caracteres incómodos; todo lo que no sea `\w`, `.` o `-` se sustituye
  por `_` y se descartan los nombres vacíos o degenerados (`.`, `..`) para que
  la ruta de destino no pueda salirse de `DEST`.
- **Filtrar y bajar** (`download_release`, `:295–308`): `release["assets"]` es
  solo la lista de binarios subidos (GitHub añade el zip/tar del fuente aparte,
  no aparece aquí). Con `--asset` se filtra con `fnmatch` (glob `*`, `?`, `[...]`)
  y si no queda ninguno se aborta con un mensaje que indica el patrón. Cada
  asset se imprime en `stderr` con su tamaño (`fmt_size`) y se baja a
  `DEST/propietario/nombre/TAG/`.
- **Descarga de cada asset** (`fetch_asset`, `:311–330`): sin token se usa
  `browser_download_url` (que no consume *rate limit* de la API); con
  `GITHUB_TOKEN` se usa la URL de la API con
  `Accept: application/octet-stream`, que permite bajar releases de repos
  privados. El cuerpo se copia en **streaming** (`shutil.copyfileobj`, bloques
  de 256 KiB) a un temporal `.download` que se renombra solo al terminar, con el
  mismo patrón de limpieza (`try/finally`) que `archive_repo`.
- **Redirecciones seguras** (`_StripAuthRedirect`, `:259–270`): tanto la API como
  `github.com` responden con un 302 hacia el CDN de descargas
  (`release-assets.githubusercontent.com`). `urllib` reenvía las cabeceras
  originales en la redirección, y el CDN rechaza peticiones que llevan
  `Authorization` junto a su URL firmada; el handler descarta esa cabecera
  cuando el *host* de destino cambia, dejando el resto intacto.

### `resolve_branch` (`:216–220`)

La rama por defecto no se asume nunca: se consulta `GET /repos/{owner}/{repo}` y
se lee `default_branch`. Solo hay un atajo: si existe la variable `GITHUB_REF` en
el entorno (`:217–218`), se usa directamente para permitir que otros tooling
(CI, scripts) fijen la ref sin una llamada extra a la API.

### `download_many` (`:148–151`) y `cmd_download` (`:333–338`)

- `search --download N` pasa los N primeros resultados de `items` a
  `download_many` (`:110`).
- `download` con varios argumentos itera `resolve_repo` + `download_repo` por cada
  uno (`:334–337`).

---

## 9. Extracción segura (`_extract`, `_strip_root`, `_safe_target`)

La extracción es el punto más delicado de seguridad, y aquí se ha hecho a mano
(en lugar de `extractall`) por dos motivos: quitar la carpeta raíz y controlar
rutas.

### Quitar la raíz (`_strip_root`, `:230–232`)

GitHub comprime cada repo dentro de una carpeta raíz de la forma
`owner-hash-abc123/`. `_strip_root` divide cada ruta en `("raiz", "resto")` y
devuelve solo el resto, de modo que los archivos acaban directamente dentro de
`DEST/propietario/nombre` sin una carpeta intermedia.

### Protección contra path traversal (`_safe_target`, `:223–227`)

Los archivos comprimidos pueden contener rutas como `../../etc/cron.d/x`. Si la
extracción las escribiera literalmente, el contenido saldría del directorio
destino (**ataque zip-slip**). La defensa es de dos capas:

1. `resolved = (dest / relative).resolve()` — resuelve los `..` que hubiera.
2. Se verifica que `resolved.is_relative_to(dest.resolve())` (`:225–226`); si el
   destino final no queda bajo la raíz, se aborta con `Ruta insegura`.

Cada entrada del archivo pasa esta comprobación **antes** de escribirse.
En el caso de tar, además se reescribe `member.name` con la ruta relativa sin raíz
(`:254`), de forma que `tarfile` extraiga exactamente lo que ya validamos.

> En Python 3.14 la extracción de tar tiene un filtro `data` por defecto que
> también rechaza rutas inseguras; aquí el control es explícito y funciona en
> cualquier Python 3.9+, manteniendo además el deseado recorte de la carpeta raíz.

### Soportes de compresión

- **zip** (`:236–247`): recorre `namelist()`, recrea directorios (entradas que
  terminan en `/`) y escribe los bytes de cada archivo.
- **tar** (`:249–256`): `tarfile.open(..., "r:*")` auto-detecta gzip/bz2/xz según
  el archivo recibido de codeload.

---

## 10. Inspección (`cmd_show`, `:341–361`)

`show` consulta `GET /repos/{owner}/{repo}` y extrae 18 campos. Dos transformaciones:

- `license` llega como objeto `{spdx_id: ...}`; se reduce a su `spdx_id` (`:352–353`).
- `size` se humaniza con `fmt_size` (`:354–355`).

De ahí pasa a la tabla de pares clave/valor o al JSON según `--json`. Es además
muy útil para **depurar** una descarga que dio 404: muestra el `default_branch`
real que luego se usará para descargar.

---

## 11. Límites de la API de GitHub

El consumo de peticiones varía por operación y por autenticación:

| Operación | Punto del código | Sin token | Con token |
|---|---|---|---|
| Búsqueda | `:69` | 10/min | 30/min |
| Datos de repo (`.download`, `show`, `resolve_branch`) | `:219`, `:343` | 60/h | 5.000/h |
| Releases (`fetch_release`) | `:276`, `:280` | 60/h | 5.000/h |
| Descarga tarball/zipball | `:195` | no cuenta (apunta a codeload) | idem |
| Descarga de assets de una release | `:314`, `:317` | no cuenta (apunta a `github.com`/CDN) | 1 llamada por asset (pasa por la API) |

Por eso lo más eficiente en términos de *rate limit* es `--mode tarball`/`zip`:
solo consume una llamada (la de `resolve_branch`) y la transferencia pesada sale
de `codeload.github.com`. `--mode clone` no consume sustancialmente la API, y
`--mode release` sin token consume **una sola llamada** (la lista o el tag de
releases), pagando el resto con el CDN de descargas; con token, cada asset baja
por la API y se resta del presupuesto. Como la release se resuelve antes de
comprobar la idempotencia, un lote ya descargado gasta esa llamada igual.

Si se agota un límite, `api_get` (`:53–57`) lo comunica y sugiere exportar el
token, que multiplica los límites por 3 en búsquedas y por ~83 en datos.

---

## 12. Manejo de errores (resumen)

| Escenario | Dónde se detecta | Salida |
|---|---|---|
| `--limit` fuera de rango | `main` `:422` | se recorta a [1,100] |
| `--tag`/`--asset` fuera de `--mode release` | `main` `:427–430` | aviso a `stderr` y se ignoran |
| `--branch` dentro de `--mode release` | `main` `:423–426` | aviso a `stderr`: usa `--tag` |
| Sin criterios de búsqueda | `build_query` `:129` | mensaje claro + exit 1 |
| Nombre de repo inválido | `resolve_repo` `:143` | mensaje claro + exit 1 |
| Rate limit 403 | `api_get` `:53` | pista del token + exit 1 |
| Token malo 401 | `api_get` `:58` | aviso + exit 1 |
| Sin red | `api_get` `:63` | motivo + exit 1 |
| Rama inexistente (404) | `archive_repo` `:203` | pista de la rama + exit 1 |
| Repo sin releases o tag sin release | `fetch_release` `:278`, `:287` | mensaje claro + exit 1 |
| Release sin assets (o sin coincidencias con `--asset`) | `download_release` `:302` | mensaje con el patrón + exit 1 |
| Fallo al bajar un asset (404/403) | `fetch_asset` `:325–328` | código y motivo + exit 1 |
| Falla `git clone` | `clone_repo` `:188` | código de retorno + exit 1 |
| Archivo malicioso | `_safe_target` `:226` | aborta la extracción + exit 1 |

En todos los casos el programa termina con _exit code_ `1` (no 0), lo que permite
a los scripts detectar fallos.

---

## 13. Posibles extensiones

- **Paginación real** en `search` para volúmenes >100 (bucle sobre `page=2..N`).
- **Descarga por lotes desde una lista** leída de un fichero o de stdin.
- **Reintentos con backoff** (la Search API a veces responde `503` eventual) y
  respeto de la cabecera `Retry-After`.
- **Paralelización** con `concurrent.futures` para varios repos a la vez,
  controlando el *rate limit*.
- **Salida en maneras de tabla** (SSV/CSV) además de texto/JSON.
- **Descarga de todas las releases** de un repo (hoy solo la última o la del
  `--tag`) y de los *assets* de las descargas anexas de la API.
- **Crear repositorio en GitHub** y **subir release** tras la descarga masiva.
- **Copiar repositorio a GitLab** y gestionar releases en ambas plataformas.
- Identificar repos **privados** y pedir el token explícitamente antes de llamar.