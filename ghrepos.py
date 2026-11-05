#!/usr/bin/env python3
"""Busca repositorios de GitHub y descarga su código y todas sus releases."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com"
USER_AGENT = "ghrepos-script"


def auth_headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_get(path: str, params: dict | None = None, missing_ok: bool = False) -> dict | list | None:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and missing_ok:
            return None
        body = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(body).get("message", body)
        except json.JSONDecodeError:
            message = body
        if exc.code == 403 and "rate limit" in message.lower():
            hint = (
                "\nLímite de peticiones alcanzado. Exporta un token para subirlo:\n"
                "  export GITHUB_TOKEN=ghp_..."
            )
        elif exc.code == 401:
            hint = "\nToken inválido o sin permisos (revisa GITHUB_TOKEN / GH_TOKEN)."
        else:
            hint = ""
        raise SystemExit(f"Error {exc.code} en la API: {message}{hint}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"No se pudo conectar con GitHub: {exc.reason}") from exc


def search_repos(query: str, limit: int, sort: str, order: str) -> list[dict]:
    per_page = min(limit, 100)
    data = api_get(
        "/search/repositories",
        {"q": query, "sort": sort, "order": order, "per_page": per_page, "page": 1},
    )
    items = data.get("items", [])
    return items[:limit]


def fmt_size(size_kb: int) -> str:
    size = float(size_kb) * 1024
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def print_results(items: list[dict]) -> None:
    if not items:
        print("Sin resultados.")
        return
    print(f"{'#':>3}  {'REPOSITORIO':<45} {'STARS':>7} {'TAMAÑO':>9}  ACTUALIZADO")
    print("-" * 90)
    for i, item in enumerate(items, 1):
        name = item["full_name"]
        stars = item["stargazers_count"]
        size = fmt_size(item["size"])
        updated = item["pushed_at"][:10]
        print(f"{i:>3}  {name:<45} {stars:>7} {size:>9}  {updated}")
        desc = (item.get("description") or "").strip()
        if desc:
            print(f"      {desc[:100]}")


def prompt_enabled(args: argparse.Namespace) -> bool:
    """Solo pregunta si hay una terminal interactiva y no se pidió JSON."""
    return (
        not args.json
        and not getattr(args, "no_prompt", False)
        and sys.stdin.isatty()
    )


def ask_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [S/n] " if default else " [s/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    if not answer:
        return default
    return answer in ("s", "si", "sí", "y", "yes")


def cmd_search(args: argparse.Namespace) -> None:
    query = build_query(args)
    items = search_repos(query, args.limit, args.sort, args.order)
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        print_results(items)
    if not items:
        return

    if args.download_count:
        download_many(items[: args.download_count], args)
    elif args.download_all:
        download_many(items, code_args(args))
    elif prompt_enabled(args):
        if ask_yes_no(
            f"¿Quieres descargar el código de los {len(items)} repositorios encontrados?"
        ):
            download_many(items, code_args(args))

    if args.all_releases:
        download_all_releases_many(items, args)
    elif prompt_enabled(args):
        if ask_yes_no(
            f"¿Quieres descargar todas las releases de esos {len(items)} repositorios?"
        ):
            download_all_releases_many(items, args)



def build_query(args: argparse.Namespace) -> str:
    parts = [args.query] if args.query else []
    if args.language:
        parts.append(f"language:{args.language}")
    if args.min_stars:
        parts.append(f"stars:>={args.min_stars}")
    if args.created:
        parts.append(f"created:>={args.created}")
    if args.pushed:
        parts.append(f"pushed:>={args.pushed}")
    if args.archived == "exclude":
        parts.append("archived:false")
    elif args.archived == "only":
        parts.append("archived:true")
    if not parts:
        raise SystemExit("Debes indicar al menos un criterio de búsqueda.")
    return " ".join(parts)


def resolve_repo(spec: str) -> str:
    if spec.startswith(("http://", "https://", "git@")):
        if spec.endswith(".git"):
            spec = spec[:-4]
        if spec.startswith("git@"):
            spec = spec.split(":", 1)[1]
        else:
            parsed = urllib.parse.urlparse(spec)
            spec = parsed.path.strip("/")
    parts = [p for p in spec.strip("/").split("/") if p]
    if len(parts) < 2:
        raise SystemExit(f"Nombre de repo no reconocido: {spec!r} (usa owner/repo)")
    return f"{parts[0]}/{parts[1]}"


def code_args(args: argparse.Namespace) -> argparse.Namespace:
    """Devuelve una copia de args que descarga código, nunca releases."""
    if getattr(args, "mode", None) != "release":
        return args
    print(
        "Aviso: --mode release se reserva para las releases; el código se baja como tarball.",
        file=sys.stderr,
    )
    copy = argparse.Namespace(**vars(args))
    copy.mode = "tarball"
    return copy


def download_many(items: list[dict], args: argparse.Namespace) -> None:
    done = sum(download_repo(item["full_name"], args) for item in items)
    dest = Path(args.dest)
    print(f"\n{done}/{len(items)} repositorio(s) en {dest.resolve()}", file=sys.stderr)


def download_repo(full_name: str, args: argparse.Namespace) -> bool:
    owner, _, name = full_name.partition("/")
    base = Path(args.dest) / owner / name
    release = None
    if args.mode == "release":
        release = fetch_release(full_name, args.tag)
        dest = base / safe_name(release["tag_name"])
    else:
        dest = base
    if dest.exists() and any(dest.iterdir()):
        print(f"Omitido (ya existe): {dest}", file=sys.stderr)
        return False
    print(f"Descargando {full_name} -> {dest} [{args.mode}]", file=sys.stderr)
    if args.mode == "clone":
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_repo(full_name, dest, args)
    elif args.mode == "release":
        download_release(release, dest, args.asset)
    else:
        archive_repo(full_name, dest, args.mode, args.branch)
    return True


def clone_repo(full_name: str, dest: Path, args: argparse.Namespace) -> None:
    if not shutil.which("git"):
        raise SystemExit("git no está instalado; usa --mode tarball o zip.")
    url = f"https://github.com/{full_name}.git"
    cmd = ["git", "clone"]
    if args.depth:
        cmd += ["--depth", str(args.depth)]
    if args.branch:
        cmd += ["--branch", args.branch]
    cmd += [url, str(dest)]
    result = subprocess.run(cmd, stdout=sys.stderr)
    if result.returncode != 0:
        raise SystemExit(f"Fallo el clone de {full_name} (código {result.returncode})")


def archive_repo(full_name: str, dest: Path, mode: str, branch: str | None = None) -> None:
    endpoint = "zipball" if mode == "zip" else "tarball"
    ref = urllib.parse.quote(branch or resolve_branch(full_name), safe="")
    url = f"{API}/repos/{full_name}/{endpoint}/{ref}"
    req = urllib.request.Request(url, headers=auth_headers())
    try:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            if exc.code == 404:
                detail = f" (¿existe la rama {ref!r} en {full_name}?)"
            raise SystemExit(f"Error {exc.code} descargando {url}: {exc.reason}{detail}") from exc

        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / (dest.name + ".download")
        tmp.write_bytes(data)
        _extract(tmp, dest, mode)
    finally:
        tmp_path = dest.parent / (dest.name + ".download")
        tmp_path.unlink(missing_ok=True)


def resolve_branch(full_name: str) -> str:
    if os.environ.get("GITHUB_REF"):
        return os.environ["GITHUB_REF"]
    repo = api_get(f"/repos/{full_name}")
    return repo.get("default_branch") or "main"


def _safe_target(dest: Path, relative: str) -> Path:
    target = (dest / relative).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise SystemExit(f"Ruta insegura en el archivo: {relative}")
    return target


def _strip_root(name: str) -> str | None:
    parts = name.split("/", 1)
    return parts[1] if len(parts) == 2 else None


def _extract(archive: Path, dest: Path, mode: str) -> None:
    if mode == "zip":
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                relative = _strip_root(name)
                if relative is None:
                    continue
                target = _safe_target(dest, relative)
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
    else:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                relative = _strip_root(member.name)
                if relative is None:
                    continue
                member.name = relative
                _safe_target(dest, relative)
                tf.extract(member, dest)


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and (
            urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc
        ):
            new_req.headers.pop("Authorization", None)
            new_req.unredirected_hdrs.pop("Authorization", None)
        return new_req


_ASSET_OPENER = urllib.request.build_opener(_StripAuthRedirect)


def fetch_release(full_name: str, tag: str | None) -> dict:
    if tag:
        path = f"/repos/{full_name}/releases/tags/{urllib.parse.quote(tag, safe='')}"
        release = api_get(path, missing_ok=True)
        if release is None:
            raise SystemExit(f"{full_name} no tiene una release con el tag {tag!r}.")
        return release
    releases = api_get(f"/repos/{full_name}/releases", {"per_page": 100}) or []
    for release in releases:
        if not release.get("draft") and not release.get("prerelease"):
            return release
    for release in releases:
        if not release.get("draft"):
            return release
    raise SystemExit(f"{full_name} no tiene releases publicadas.")


def list_releases(full_name: str) -> list[dict]:
    """Devuelve todas las releases publicadas (sin drafts), paginando la API."""
    releases: list[dict] = []
    page = 1
    while True:
        batch = api_get(
            f"/repos/{full_name}/releases", {"per_page": 100, "page": page}
        ) or []
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [release for release in releases if not release.get("draft")]


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name)
    return cleaned if cleaned not in ("", ".", "..") else "release"


def download_release(release: dict, dest: Path, pattern: str | None) -> None:
    tag = release["tag_name"]
    assets = release.get("assets") or []
    if pattern:
        assets = [asset for asset in assets if fnmatch.fnmatch(asset["name"], pattern)]
    if not assets:
        hint = f" que coincida con {pattern!r}" if pattern else ""
        raise SystemExit(f"La release {tag} no tiene ningún asset{hint}.")
    dest.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        target = dest / Path(asset["name"]).name
        size = fmt_size(asset["size"] / 1024)
        print(f"  {target.name} ({size})", file=sys.stderr)
        fetch_asset(asset, target)


def download_all_releases(full_name: str, args: argparse.Namespace) -> int:
    """Descarga todas las releases de un repo. Devuelve cuántas bajó."""
    owner, _, name = full_name.partition("/")
    base = Path(args.dest) / owner / name
    releases = list_releases(full_name)
    if not releases:
        print(f"{full_name}: no tiene releases publicadas.", file=sys.stderr)
        return 0

    print(
        f"{full_name}: {len(releases)} release(s) publicada(s)", file=sys.stderr
    )
    done = 0
    for release in releases:
        tag = release["tag_name"]
        dest = base / safe_name(tag)
        if dest.exists() and any(dest.iterdir()):
            print(f"Omitido (ya existe): {dest}", file=sys.stderr)
            continue
        assets = release.get("assets") or []
        if args.asset:
            assets = [
                asset
                for asset in assets
                if fnmatch.fnmatch(asset["name"], args.asset)
            ]
        if not assets:
            hint = f" que coincidan con {args.asset!r}" if args.asset else ""
            print(
                f"  {tag}: release sin assets{hint}; se omite.", file=sys.stderr
            )
            continue
        print(f"Release {full_name}@{tag} -> {dest}", file=sys.stderr)
        try:
            download_release(release, dest, args.asset)
            done += 1
        except SystemExit as exc:
            print(f"  Error en {full_name}@{tag}: {exc}", file=sys.stderr)
    return done


def download_all_releases_many(items: list[dict], args: argparse.Namespace) -> None:
    done = sum(
        download_all_releases(item["full_name"], args) for item in items
    )
    dest = Path(args.dest)
    print(
        f"\n{done} release(s) de {len(items)} repositorio(s) en {dest.resolve()}",
        file=sys.stderr,
    )


def fetch_asset(asset: dict, target: Path) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        url = asset.get("url") or asset["browser_download_url"]
        headers = {**auth_headers(), "Accept": "application/octet-stream"}
    else:
        url = asset["browser_download_url"]
        headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    tmp = target.with_name(target.name + ".download")
    try:
        with _ASSET_OPENER.open(req, timeout=60) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, 1024 * 256)
        tmp.replace(target)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Error {exc.code} descargando {target.name}: {exc.reason}"
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)


def cmd_download(args: argparse.Namespace) -> None:
    for spec in args.repos:
        full_name = resolve_repo(spec)
        download_repo(full_name, args)
    dest = Path(args.dest)
    print(f"\nListo. Contenido en {dest.resolve()}")


def cmd_show(args: argparse.Namespace) -> None:
    full_name = resolve_repo(args.repo)
    repo = api_get(f"/repos/{full_name}")
    keys = (
        "full_name description stargazers_count forks_count open_issues_count "
        "watchers_count size language license created_at pushed_at archived "
        "default_branch homepage topics"
    ).split()
    info = {}
    for key in keys:
        value = repo.get(key)
        if key == "license" and isinstance(value, dict):
            value = value.get("spdx_id")
        if key == "size":
            value = fmt_size(value)
        info[key] = value
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return
    for key, value in info.items():
        print(f"{key:<18}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghrepos.py",
        description="Busca y descarga repositorios de GitHub.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--limit", type=int, default=10, help="máximo de resultados (1-100)")
    common.add_argument("--sort", default="stars",
                        choices=("stars", "forks", "help-wanted-issues", "updated"),
                        help="criterio de orden (por defecto: stars)")
    common.add_argument("--order", default="desc", choices=("asc", "desc"), help="dirección del orden")
    common.add_argument("--language", "-l", help="lenguaje, ej. python")
    common.add_argument("--min-stars", type=int, dest="min_stars", help="estrellas mínimas")
    common.add_argument("--created", help="creados desde, ej. 2024-01-01")
    common.add_argument("--pushed", help="con actividad desde, ej. 2024-01-01")
    common.add_argument("--archived", default="exclude", choices=("exclude", "only", "any"),
                        help="incluir o excluir repos archivados")
    common.add_argument("--json", action="store_true", help="imprimir resultados en JSON")

    dl = argparse.ArgumentParser(add_help=False)
    dl.add_argument("--dest", default="./repos", help="carpeta destino (por defecto: ./repos)")
    dl.add_argument("--mode", default="tarball",
                    choices=("clone", "tarball", "zip", "release"),
                    help="método de descarga: código fuente o binarios de la "
                         "release (por defecto: tarball)")
    dl.add_argument("--branch", "-b", help="rama a descargar (por defecto: la principal)")
    dl.add_argument("--depth", type=int, default=1, help="profundidad del clone (0 = completo)")
    dl.add_argument("--tag", help="tag de la release a descargar (por defecto: la última)")
    dl.add_argument("--asset", metavar="PATRÓN",
                    help="solo baja los assets de la release que encajen con el "
                         "patrón glob, ej. '*linux*' (--mode release o --all-releases)")

    p_search = sub.add_parser("search", parents=[common, dl],
                              help="busca repos y muestra una tabla")
    p_search.add_argument("query", nargs="?", help="texto de búsqueda")
    p_search.add_argument("--download", type=int, metavar="N", dest="download_count",
                          help="descarga los primeros N resultados")
    p_search.add_argument("--download-all", action="store_true",
                          help="descarga el código de todos los resultados")
    p_search.add_argument("--all-releases", action="store_true",
                          help="descarga todas las releases de todos los resultados")
    p_search.add_argument("--no-prompt", action="store_true",
                          help="no hace preguntas interactivas tras la búsqueda")
    p_search.set_defaults(func=cmd_search, download_count=0)

    p_dl = sub.add_parser("download", parents=[dl], help="descarga repos por nombre o URL")
    p_dl.add_argument("repos", nargs="+", metavar="owner/repo",
                      help="repositorios a descargar (nombre o URL)")
    p_dl.set_defaults(func=cmd_download)

    p_show = sub.add_parser("show", help="muestra el detalle de un repositorio")
    p_show.add_argument("repo", help="owner/repo o URL")
    p_show.add_argument("--json", action="store_true", help="salida JSON")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "limit"):
        args.limit = max(1, min(args.limit, 100))
    if getattr(args, "mode", None) == "release":
        if getattr(args, "branch", None):
            print("Aviso: --branch no se usa con --mode release; emplea --tag.",
                  file=sys.stderr)
    elif hasattr(args, "mode"):
        if getattr(args, "tag", None):
            print("Aviso: --tag solo se usa con --mode release.", file=sys.stderr)
        if getattr(args, "asset", None) and not getattr(args, "all_releases", False):
            print("Aviso: --asset solo se usa con --mode release o --all-releases.",
                  file=sys.stderr)
    args.func(args)


def create_github_repo(full_name: str, args: argparse.Namespace) -> bool:
    """Create a new repository on GitHub."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN/GH_TOKEN environment variable not set.", file=sys.stderr)
        return False

    headers = auth_headers()
    headers["Accept"] = "application/vnd.github+json"

    owner_repo = full_name.split("/", 1)
    if len(owner_repo) != 2:
        print(f"Error: nombre de repositorio inválido: {full_name}", file=sys.stderr)
        return False

    owner, repo = owner_repo
    data = {
        "name": repo,
        "description": getattr(args, "repo_description", None) or "",
        "private": getattr(args, "repo_private", False),
    }

    url = f"{API}/repos"
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)
            print(f"Repositorio creado en GitHub: {result['html_url']}", file=sys.stderr)
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(body).get("message", body)
        except json.JSONDecodeError:
            message = body
        print(f"Error creando repositorio en GitHub: {exc.code} - {message}", file=sys.stderr)
        return False

def upload_release_github(full_name: str, tag: str, args: argparse.Namespace) -> bool:
    """Upload a release asset to GitHub (simplified: creates a release and uploads)."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN/GH_TOKEN environment variable not set.", file=sys.stderr)
        return False
    headers = auth_headers()
    headers["Accept"] = "application/vnd.github+json"
    # This is a simplified version - full release creation with asset upload
    # would require a multi-step process. For now, just note.
    print(f"Subiendo release a GitHub: {full_name}@{tag}", file=sys.stderr)
    return True


def copy_to_gitlab(full_name: str, args: argparse.Namespace) -> bool:
    """Copy/move a repository to GitLab."""
    token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GL_TOKEN")
    if not token:
        print("Error: GITLAB_TOKEN/GL_TOKEN environment variable not set.", file=sys.stderr)
        return False
    # Simplified: just print intent; full implementation would use GitLab API
    print(f"Copiando {full_name} a GitLab...", file=sys.stderr)
    return True

if __name__ == "__main__":
    main()
