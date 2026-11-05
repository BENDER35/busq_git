# ghrepos.py

Script de línea de comandos para **buscar, inspeccionar y descargar repositorios de GitHub** desde la terminal. Escrito en Python 3 con la librería estándar: no requiere `pip install`, ni `requests`, ni ningún otro requisito más allá de Python 3.9+ (y `git` solo si se usa `--mode clone`).

---

## Características

- Búsqueda en el buscador de GitHub con calificadores (`language:`, `stars:`, `created:`, `pushed:`, `archived:`).
- Resultados en tabla legible o en JSON parseable con `jq`.
- Descarga de uno o varios repos en un solo comando.
- Cuatro métodos de descarga: `tarball` (por defecto, sin dependencias), `zip`,
  `clone` (hist completo o superficial) y `release` (los binarios publicados por
  los desarrolladores en las releases del repo).
- **Flujo interactivo tras buscar**: pregunta primero
  `¿Quieres descargar el código de los N repositorios encontrados?` y después
  `¿Quieres descargar todas las releases de esos N repositorios?`.
- Descarga del código de **todos** los resultados (`--download-all`) y de
  **todas las releases** de cada repo (`--all-releases`), no solo la última.
- Selección de rama, profundidad de clonado y de la release (tag) a descargar.
- Idempotencia: los repos y las releases ya descargados se omiten, no se re-descargan.
- Autenticación opcional con token para subir los límites de peticiones.
- Sin dependencias externas.

---

## Requisitos

| Requisito | Versión | Cuándo hace falta |
|---|---|---|
| Python | 3.9 o superior | siempre |
| `git` | cualquier versión | solo con `--mode clone` |
| Conexión a `api.github.com` | HTTPS | siempre |
| Token de GitHub | opcional | para superar los límites de peticiones |

Comprobación rápida:

```bash
python3 --version
git --version
```

---

## Instalación

```bash
git clone <este-repositorio>
cd sc_busq_git
chmod +x ghrepos.py
./ghrepos.py --help
```

También se puede invocar como `python3 ghrepos.py ...`.

---

## Uso

El script tiene tres subcomandos: `search`, `download` y `show`.

### 1. `search` — buscar y listar

```bash
./ghrepos.py search "web scraping" -l python --min-stars 1000 --limit 10
```

```
  #  REPOSITORIO                                     STARS    TAMAÑO  ACTUALIZADO
------------------------------------------------------------------------------------------
  1  D4Vinci/Scrapling                               86163   11.3 MB  2026-10-07
      An adaptive Web Scraping framework that handles everything from a single request...
  2  scrapy/scrapy                                   64636   32.3 MB  2026-10-07
      Scrapy, a fast high-level web crawling / scraping framework for Python.
```

Buscar y **descargar directamente los 3 primeros resultados**:

```bash
./ghrepos.py search "buscador peliculas" -l python --download 3 --dest ./repos
```

Salida JSON (el progreso va a `stderr`, así que el pipe queda limpio):

```bash
./ghrepos.py search "framework ia" -l rust --limit 5 --json | jq -r '.[].full_name'
```

#### Flujo interactivo: bajar el código y luego todas las releases

Cuando `search` se ejecuta desde una terminal (stdin interactivo), no se pide
`--json` y no se usa `--no-prompt`, después de mostrar la tabla pregunta:

```text
¿Quieres descargar el código de los 5 repositorios encontrados? [s/N]
¿Quieres descargar todas las releases de esos 5 repositorios? [s/N]
```

- La primera pregunta descarga el **código fuente** de todos los resultados con
  el `--mode` elegido (`tarball` por defecto). Si se había puesto
  `--mode release`, para el código se usa `tarball` automáticamente. **Si se usa
  `--download-all`, el código se descarga automáticamente sin preguntar primero.**
- La segunda descarga **todas las releases publicadas** (sin drafts) de cada
  repo, cada una en su carpeta `DEST/propietario/nombre/TAG/`.
- Los repos y releases que ya existan se omiten, así que se puede repetir sin
  miedo a duplicar descargas.

Para lanzar el mismo flujo sin preguntas (por ejemplo en un script o en CI):

```bash
# Código de todos los resultados + todas sus releases
./ghrepos.py search "buscador peliculas" -l python --limit 5 \
    --download-all --all-releases

# Solo todas las releases de los resultados
./ghrepos.py search "jq" -l c --limit 3 --all-releases

# Todas las releases, pero solo los assets de Linux
./ghrepos.py search "jq" -l c --limit 3 --all-releases --asset '*linux*'

# Desactivar las preguntas y no descargar nada
./ghrepos.py search "rust cli" -l rust --no-prompt
```

### 2. `download` — descargar por nombre o URL

```bash
# Nombre simple
./ghrepos.py download octocat/Hello-World

# Varias URLs a la vez, modo zip
./ghrepos.py download https://github.com/octocat/Hello-World \
                      https://github.com/octocat/Spoon-Knife --mode zip

# Clone superficial de una rama concreta
./ghrepos.py download octocat/Hello-World --mode clone --branch master --depth 1

# Historial completo
./ghrepos.py download octocat/Hello-World --mode clone --depth 0

# Binarios de la última release (los archivos ya compilados que suben los devs)
./ghrepos.py download jqlang/jq --mode release

# Releases concretas y solo el binario de Linux
./ghrepos.py download jqlang/jq --mode release --tag jq-1.7.1 --asset "*linux-amd64*"
```

Con `--mode release` se baja el **último release publicado** (sin drafts ni
prereleases); con `--tag` se elige uno concreto. Los archivos se guardan en
`DEST/propietario/nombre/TAG/`, de modo que conviven varias releases del mismo
repo sin pisarse.

Para bajar **todas las releases** de golpe usa `--all-releases` en `search`:

```bash
./ghrepos.py search "jq" -l c --limit 3 --all-releases
```

`--all-releases` recorre todas las páginas de releases (100 por página), omite
los drafts, incluye las prereleases y se salta sin abortar los repos que no
tienen releases o las releases sin assets.

Formatos aceptados: `owner/repo`, `https://github.com/owner/repo`, `.../owner/repo.git`,
`.../owner/repo/tree/main`, `git@github.com:owner/repo.git`.

### 3. `show` — detalle de un repositorio

```bash
./ghrepos.py show octocat/Hello-World
./ghrepos.py show octocat/Hello-World --json | jq .license,.default_branch
```

---

## Opciones

### Comunes de búsqueda (`search`)

| Opción | Descripción | Por defecto |
|---|---|---|
| `QUERY` | texto libre de búsqueda (opcional si hay filtros) | — |
| `--limit N` | máximo de resultados (1–100) | `10` |
| `--sort` | `stars`, `forks`, `help-wanted-issues`, `updated` | `stars` |
| `--order` | `asc` o `desc` | `desc` |
| `-l, --language LANG` | filtrar por lenguaje | — |
| `--min-stars N` | estrellas mínimas | — |
| `--created FECHA` | creados desde (`YYYY-MM-DD`) | — |
| `--pushed FECHA` | con actividad desde (`YYYY-MM-DD`) | — |
| `--archived` | `exclude`, `only` o `any` | `exclude` |
| `--json` | salida JSON en lugar de tabla | activo |
| `--download N` | descargar los primeros N resultados | desactivado |
| `--download-all` | descargar el código de **todos** los resultados | desactivado |
| `--all-releases` | descargar **todas** las releases de todos los resultados | desactivado |
| `--no-prompt` | no hacer preguntas interactivas tras la búsqueda | desactivado |

### De descarga (compartidas por `search` y `download`)

| Opción | Descripción | Por defecto |
|---|---|---|
| `--dest DIR` | carpeta destino | `./repos` |
| `--mode` | `tarball`, `zip`, `clone` o `release` | `tarball` |
| `-b, --branch RAMA` | rama a descargar | rama principal |
| `--depth N` | profundidad del clone (`0` = completo) | `1` |
| `--tag TAG` | release a descargar (solo `--mode release`) | última release |
| `--asset PATRÓN` | solo los assets que encajen con el glob, ej. `'*linux*'` (`--mode release` o `--all-releases`) | todos |

Los repos se guardan en `DEST/propietario/nombre`, por ejemplo
`./repos/scrapy/scrapy`. En `--mode release` y en `--all-releases` la ruta añade
el tag: `./repos/jqlang/jq/jq-1.7.1/jq-linux-amd64`.

---

## Autenticación y límites de peticiones

Sin token, la API de GitHub permite **10 búsquedas por minuto** y **60 peticiones
de datos por hora**. Con un token esos límites suben a 30/min y 5.000/h.

```bash
export GITHUB_TOKEN=ghp_...     # o GH_TOKEN
./ghrepos.py search "compilers" -l rust --limit 20
```

El token solo se lee de la variable de entorno: **nunca se escribe en disco ni en
los logs**. Si se alcanza el límite, el script lo indica y sugiere exportarlo.

---

## Ejemplos avanzados

```bash
# Top 5 repos de Rust muy estrellas y activos en el último año
./ghrepos.py search -l rust --min-stars 20000 --pushed 2025-10-01 \
    --sort stars --limit 5

# Incluir también repos archivados
./ghrepos.py search "machine learning" -l python --archived any

# Descarga masiva y luego listado de lo descargado
./ghrepos.py search "awesome" -l go --limit 5 --download 5 --dest ~/coding/awesomes
find ~/coding/awesomes -maxdepth 2 -mindepth 2

# Código + todas las releases de los 3 resultados más actualizados
./ghrepos.py search "jq" -l c --sort updated --limit 3 \
    --download-all --all-releases --dest ~/backups/jq

# Pipeline: descargar los 3 más actualizados como zip
./ghrepos.py search "cli" -l rust --sort updated --limit 3 --json \
  | jq -r '.[].full_name' \
  | xargs -I{} ./ghrepos.py download {} --mode zip --dest ./clis
```

---

## Solución de problemas

| Síntoma | Causa | Solución |
|---|---|---|
| `Error 403 ... rate limit` | límite de peticiones agotado | `export GITHUB_TOKEN=...` |
| `Error 401 en la API` | token inválido o caducado | revisar `GITHUB_TOKEN` / `GH_TOKEN` |
| `Error 404 ... tarball/r ...` | la rama no existe | comprobar con `show` el `default_branch` |
| `no tiene releases publicadas` | el repo no tiene releases | usar otro modo de descarga |
| `release sin assets ... se omite` | `--all-releases` encontró una release sin binarios o sin coincidencias con `--asset` | normal: sigue con las demás releases |
| `La release ... no tiene ningún asset` | el tag no subió binarios o el glob no encaja | revisar `--tag` / `--asset` |
| `git no está instalado` | falta `git` | instalarlo o usar `--mode tarball` |
| `Omitido (ya existe)` | el repo ya está descargado | borrar la carpeta para forzar la descarga |
| `No se pudo conectar con GitHub` | sin red / proxy / DNS | revisar conexión o variables `HTTPS_PROXY` |

Códigos de salida: `0` cuando todo va bien, `1` ante cualquier error (la API, la
red, la extracción o un `git clone` fallido).

---

## Integración con GitHub y GitHub Enterprise

El script incluye funcionalidades para crear repositorios y subir releases:

- **Crear repositorio en GitHub**: usa `--create-github-repo` después de descargar
  el código. Requiere `GITHUB_TOKEN` con alcance `repo`. El nombre del repo se
  deriva del repositorio buscado.

- **Subir release a GitHub**: usa `upload_release_github` para subir assets a una
  release existente. Para crear releases completas, se recomienda usar la API
  de GitHub directamente.

- **Copiar a GitLab**: usa `copy_to_gitlab` para indicar la intención de mover
  un repositorio a GitLab. Requiere `GITLAB_TOKEN` con permisos adecuados.

Para usar estas funciones:

```bash
# Después de buscar y descargar, crear repo en GitHub
./ghrepos.py search "mi-proyecto" --download-all --all-releases \
    --create-github-repo --repo-description "Mi proyecto descriptivo"

# Copiar a GitLab (después de tener el código localmente)
./ghrepos.py download mi/proyecto --mode tarball
# Luego manual o usar la API de GitLab
```

---

## Documentación

La explicación detallada del funcionamiento interno (flujo completo, capa HTTP,
construcción de consultas, descarga, extracción y seguridad) está en
**[DOC_TECNICA.md](DOC_TECNICA.md)**.
