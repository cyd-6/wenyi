"""Map owned files and structured path fields without changing book text or IDs."""

from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ..paths import safe_filename


def file_mapping(names: list[str], package_id: str) -> dict[str, str]:
    prefixes, used = {}, set()
    result = {}
    for name in sorted(names):
        parts = PurePosixPath(name).parts
        previous = []
        mapped = []
        for part in parts:
            previous.append(part)
            original = "/".join(previous)
            if original in prefixes:
                mapped = prefixes[original].split("/")
                continue
            candidate = safe_filename(part)
            target = "/".join([*mapped, candidate])
            if candidate != part or target.casefold() in used:
                extension = PurePosixPath(candidate).suffix
                stem = candidate[: -len(extension)] if extension else candidate
                candidate = (
                    f"{stem[:85]}-{hashlib.sha256(original.encode()).hexdigest()[:12]}{extension}"
                )
            mapped.append(candidate)
            target = "/".join(mapped)
            used.add(target.casefold())
            prefixes[original] = target
        target = "/".join(mapped)
        if parts[0] == "exports":
            target = target.replace("exports/", f"exports/import_{package_id[:12]}/", 1)
        result[name] = target
    return result


def remap_document(
    value,
    *,
    old_pid: str,
    new_pid: str,
    source_root: str,
    files: dict[str, str],
    runs: dict[str, str],
    exports: dict[int, int],
    jobs: dict[int, int],
):
    def path_value(text: str) -> str:
        normalized = text.replace("\\", "/")
        if normalized.rstrip("/") in {old_pid, source_root.replace("\\", "/").rstrip("/")}:
            return new_pid
        relative = normalized
        for prefix in (source_root.replace("\\", "/").rstrip("/") + "/", old_pid + "/"):
            if normalized.startswith(prefix):
                relative = normalized[len(prefix) :]
                break
        return f"{new_pid}/{files[relative]}" if relative in files else text

    def visit(item, key=""):
        if isinstance(item, dict):
            # Frozen config definitions are historical records, not registry references.
            return {
                name: visit(child, key if key == "outputs" else name)
                for name, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if key == "project_id" and item == old_pid:
            return new_pid
        if key in {"run_id", "arq_job_id", "job_id"} and isinstance(item, str):
            return runs.get(item, item)
        if key == "export_id" and isinstance(item, int):
            return exports.get(item, item)
        if key == "job_id" and isinstance(item, int):
            return jobs.get(item, item)
        if key in {
            "source_path",
            "path",
            "output_path",
            "out_path",
            "outputs",
            "run_dir",
        } and isinstance(item, str):
            return path_value(item)
        return item

    return visit(value)


def rewrite_resource_links(path: Path, old_name: str, files: dict[str, str]) -> None:
    """Repair generated HTML/CSS links only when a resource filename was normalized."""
    if path.suffix.lower() not in {".html", ".htm", ".xhtml", ".css"}:
        return
    original = path.read_text(encoding="utf-8")

    def url(value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return value
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(old_name), unquote(parsed.path))
        )
        if resolved not in files:
            return value
        updated = posixpath.relpath(files[resolved], posixpath.dirname(files[old_name]))
        return urlunsplit(("", "", quote(updated, safe="/"), parsed.query, parsed.fragment))

    def css(text: str) -> str:
        return re.sub(
            r'url\(\s*([\'"]?)(.*?)\1\s*\)', lambda match: f'url("{url(match[2])}")', text
        )

    if path.suffix.lower() == ".css":
        updated = css(original)
    else:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(original, "html.parser")
        changed = False
        for tag in soup.find_all(True):
            for attribute in ("src", "href", "poster"):
                if isinstance(tag.get(attribute), str):
                    before = tag[attribute]
                    tag[attribute] = url(before)
                    changed |= before != tag[attribute]
            if isinstance(tag.get("style"), str):
                before = tag["style"]
                tag["style"] = css(before)
                changed |= before != tag["style"]
        updated = str(soup) if changed else original
    if updated != original:
        path.write_text(updated, encoding="utf-8")
