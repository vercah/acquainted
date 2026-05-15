"""Read/write per-person markdown files with frontmatter + dated timeline body."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path

import frontmatter
from markupsafe import Markup, escape

# Fixed schema fields that appear in the header form (in display order).
# `last-met` is managed by the app, never user-editable, never auto-logged.
TRACKED_FIELDS = ["display-name", "partner", "job", "location", "tags"]
HEADER_ORDER = TRACKED_FIELDS + ["partner-file", "last-met"]

DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:,\s*(.+?))?\s*$", re.MULTILINE)


# ---------- helpers ----------


def _normalize_str(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _normalize_tags(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        # Allow comma-separated input from the form.
        return [t.strip() for t in v.split(",") if t.strip()]
    if isinstance(v, (list, tuple)):
        return [str(t).strip() for t in v if str(t).strip()]
    return []


def _normalize_partner_list(v) -> list[str]:
    """Read partner / partner-file as a list, accepting either the legacy
    scalar form (``partner: Sam``) or the new list form. Preserves empty
    entries because partner and partner-file are stored in parallel — a
    name may be present without a linked file, so the slot still occupies
    a position in the list."""
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v]
    return []


def _emit_partner_value(items: list[str]):
    """Choose the YAML shape: omit if empty, scalar string if exactly one
    non-empty entry, list otherwise. Keeps existing single-partner files
    unchanged on read+write (no migration)."""
    cleaned = [s for s in items if s]
    if not cleaned:
        return None
    if len(cleaned) == 1 and len(items) == 1:
        return cleaned[0]
    return items


def slugify_filename(display_name: str) -> str:
    """Suggest 'surname-given.md' from a display name. Strips diacritics + parens."""
    name = re.sub(r"\([^)]*\)", "", display_name).strip()
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    parts = [re.sub(r"[^a-zA-Z0-9]+", "", p).lower() for p in ascii_only.split()]
    parts = [p for p in parts if p]
    if not parts:
        return "person.md"
    if len(parts) == 1:
        return f"{parts[0]}.md"
    surname = parts[-1]
    given = "-".join(parts[:-1])
    return f"{surname}-{given}.md"


def _surname_sort_key(display_name: str) -> tuple[str, str]:
    """Sort key (surname, given) — last whitespace-separated word is the surname.

    Same convention as slugify_filename. Parens are stripped, diacritics folded so
    'Müller' sorts as 'muller'.
    """
    name = re.sub(r"\([^)]*\)", "", display_name).strip()
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    parts = ascii_only.split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[-1], " ".join(parts[:-1]))


def _last_met_str(meta: dict) -> str:
    v = meta.get("last-met")
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


# ---------- listing ----------


def _extract_snippets(
    body: str, query: str, max_count: int = 3, context: int = 60
) -> list[Markup]:
    """Return up to `max_count` HTML-safe snippets with <mark> around matches."""
    q_lower = query.lower()
    body_lower = body.lower()
    snippets: list[Markup] = []
    start = 0
    while len(snippets) < max_count:
        idx = body_lower.find(q_lower, start)
        if idx == -1:
            break
        s = max(0, idx - context)
        e = min(len(body), idx + len(query) + context)
        snippet_text = " ".join(body[s:e].split())
        sl = snippet_text.lower()
        midx = sl.find(q_lower)
        if midx == -1:
            html = escape(snippet_text)
        else:
            before = escape(snippet_text[:midx])
            match = escape(snippet_text[midx : midx + len(query)])
            after = escape(snippet_text[midx + len(query) :])
            html = before + Markup("<mark>") + match + Markup("</mark>") + after
        prefix = Markup("…") if s > 0 else Markup("")
        suffix = Markup("…") if e < len(body) else Markup("")
        snippets.append(prefix + html + suffix)
        start = idx + len(q_lower)
    return snippets


def search_in_folder(folder: Path, query: str) -> dict:
    """Full-text search across all .md files in `folder`.

    Returns {"primary": [...], "body_matches": [...]} where:
      - primary: people whose display-name or tags contain query (no snippets)
      - body_matches: people who match in body only, with `snippets` attached
    Both lists are sorted by surname.
    """
    q = query.strip().lower()
    if not q:
        return {"primary": [], "body_matches": []}

    primary = []
    body_matches = []

    for path in sorted(folder.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:
            continue
        meta = post.metadata or {}
        display_name = meta.get("display-name")
        if not display_name:
            continue

        person = {
            "filename": path.name,
            "display_name": str(display_name),
            "last_met": _last_met_str(meta),
            "tags": _normalize_tags(meta.get("tags")),
        }

        haystacks = [
            person["display_name"].lower(),
            _normalize_str(meta.get("job")).lower(),
            _normalize_str(meta.get("location")).lower(),
        ]
        haystacks.extend(t.lower() for t in person["tags"])
        haystacks.extend(p.lower() for p in _normalize_partner_list(meta.get("partner")) if p)

        if any(q in h for h in haystacks):
            primary.append(person)
            continue

        body = post.content or ""
        if q in body.lower():
            person["snippets"] = _extract_snippets(body, query)
            body_matches.append(person)

    primary.sort(key=lambda r: _surname_sort_key(r["display_name"]))
    body_matches.sort(key=lambda r: _surname_sort_key(r["display_name"]))
    return {"primary": primary, "body_matches": body_matches}


def list_people(folder: Path) -> list[dict]:
    """Scan folder for .md files with display-name in frontmatter."""
    results = []
    for p in sorted(folder.glob("*.md")):
        try:
            post = frontmatter.load(str(p))
        except Exception:
            continue
        meta = post.metadata or {}
        display_name = meta.get("display-name")
        if not display_name:
            continue
        results.append(
            {
                "filename": p.name,
                "display_name": str(display_name),
                "last_met": _last_met_str(meta),
                "tags": _normalize_tags(meta.get("tags")),
            }
        )
    results.sort(key=lambda r: _surname_sort_key(r["display_name"]))
    return results


# ---------- load ----------


def load_person(folder: Path, filename: str) -> dict:
    file_path = folder / filename
    post = frontmatter.load(str(file_path))
    meta = dict(post.metadata or {})
    partners = _normalize_partner_list(meta.get("partner"))
    partner_files = _normalize_partner_list(meta.get("partner-file"))
    # Pad to equal length (parallel arrays).
    n = max(len(partners), len(partner_files))
    partners = (partners + [""] * n)[:n]
    partner_files = (partner_files + [""] * n)[:n]
    header = {
        "display-name": _normalize_str(meta.get("display-name")),
        "partner": partners,
        "partner-file": partner_files,
        "job": _normalize_str(meta.get("job")),
        "location": _normalize_str(meta.get("location")),
        "tags": _normalize_tags(meta.get("tags")),
        "last-met": _last_met_str(meta),
    }
    extras = {k: v for k, v in meta.items() if k not in HEADER_ORDER}
    return {"header": header, "body": post.content or "", "extras": extras}


# ---------- create ----------


def create_person(folder: Path, filename: str, initial_header: dict) -> Path:
    file_path = folder / filename
    if file_path.exists():
        raise FileExistsError(f"{filename} already exists")

    meta = _build_meta(initial_header, last_met="", extras={})
    post = frontmatter.Post("", **meta)
    file_path.write_text(frontmatter.dumps(post, sort_keys=False), encoding="utf-8")
    _apply_partner_backlink(
        folder, filename,
        old_partner_files=[],
        new_partner_files=_normalize_partner_list(initial_header.get("partner-file")),
        my_display_name=_normalize_str(initial_header.get("display-name")),
    )
    return file_path


def _is_safe_partner_filename(pf: str) -> bool:
    if not pf:
        return False
    if "/" in pf or "\\" in pf or ".." in pf:
        return False
    return pf.endswith(".md")


def _rewrite_partner_lists(
    file_path: Path,
    new_partners: list[str],
    new_partner_files: list[str],
) -> None:
    """Rewrite just the partner / partner-file fields of the file at file_path,
    preserving body content and all other frontmatter keys."""
    post = frontmatter.load(str(file_path))
    meta = dict(post.metadata or {})
    other_header = {
        "display-name": _normalize_str(meta.get("display-name")),
        "partner": new_partners,
        "partner-file": new_partner_files,
        "job": _normalize_str(meta.get("job")),
        "location": _normalize_str(meta.get("location")),
        "tags": _normalize_tags(meta.get("tags")),
    }
    last_met = _last_met_str(meta)
    extras = {k: v for k, v in meta.items() if k not in HEADER_ORDER}
    rebuilt = _build_meta(other_header, last_met=last_met, extras=extras)
    new_post = frontmatter.Post(post.content or "", **rebuilt)
    file_path.write_text(
        frontmatter.dumps(new_post, sort_keys=False), encoding="utf-8"
    )


def _backlink_add(
    folder: Path, other_filename: str, my_filename: str, my_display_name: str
) -> None:
    """Ensure the file at `other_filename` lists `my_filename` (+ my display name)
    in its partner-file / partner lists. Safe to call repeatedly."""
    if not _is_safe_partner_filename(other_filename):
        return
    if other_filename == my_filename or not my_display_name:
        return
    other_path = folder / other_filename
    if not other_path.exists():
        return
    try:
        post = frontmatter.load(str(other_path))
    except Exception:
        return
    meta = dict(post.metadata or {})
    names = _normalize_partner_list(meta.get("partner"))
    files = _normalize_partner_list(meta.get("partner-file"))
    n = max(len(names), len(files))
    names = (names + [""] * n)[:n]
    files = (files + [""] * n)[:n]

    if my_filename in files:
        # Already linked — just refresh the cached display-name in case it changed.
        changed = False
        for i, f in enumerate(files):
            if f == my_filename and names[i] != my_display_name:
                names[i] = my_display_name
                changed = True
        if changed:
            _rewrite_partner_lists(other_path, names, files)
        return

    names.append(my_display_name)
    files.append(my_filename)
    _rewrite_partner_lists(other_path, names, files)


def _backlink_remove(folder: Path, other_filename: str, my_filename: str) -> None:
    """Remove `my_filename` and its paired display name from the file at
    `other_filename`. Cascade — no confirmation here, the caller is responsible
    for confirming with the user."""
    if not _is_safe_partner_filename(other_filename):
        return
    other_path = folder / other_filename
    if not other_path.exists():
        return
    try:
        post = frontmatter.load(str(other_path))
    except Exception:
        return
    meta = dict(post.metadata or {})
    names = _normalize_partner_list(meta.get("partner"))
    files = _normalize_partner_list(meta.get("partner-file"))
    n = max(len(names), len(files))
    names = (names + [""] * n)[:n]
    files = (files + [""] * n)[:n]
    if my_filename not in files:
        return
    new_names: list[str] = []
    new_files: list[str] = []
    for nm, f in zip(names, files):
        if f == my_filename:
            continue
        new_names.append(nm)
        new_files.append(f)
    _rewrite_partner_lists(other_path, new_names, new_files)


def _apply_partner_backlink(
    folder: Path,
    filename: str,
    old_partner_files: list[str],
    new_partner_files: list[str],
    my_display_name: str,
) -> None:
    """Sync the relationship on the other side.

    - For files newly added to our partner-file list: append us to theirs.
    - For files removed from our partner-file list: drop us from theirs (cascade).
    - For files that stayed: refresh the cached display-name on their side.

    All updates are silent — no note bullets. The caller is responsible for any
    user-facing confirmation before removals.
    """
    old_set = {f for f in old_partner_files if f}
    new_set = {f for f in new_partner_files if f}
    added = new_set - old_set
    removed = old_set - new_set
    stayed = new_set & old_set

    for pf in sorted(removed):
        _backlink_remove(folder, pf, filename)
    for pf in sorted(added):
        _backlink_add(folder, pf, filename, my_display_name)
    # Refresh display-name on the other side for surviving links — cheap and
    # keeps caches honest when the user renames themselves.
    for pf in sorted(stayed):
        _backlink_add(folder, pf, filename, my_display_name)


# ---------- save ----------


def _format_change(field: str, old, new) -> str | None:
    if field == "tags":
        old_set = set(_normalize_tags(old))
        new_set = set(_normalize_tags(new))
        if old_set == new_set:
            return None
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        parts = []
        if added:
            parts.append("+" + ", +".join(added))
        if removed:
            parts.append("-" + ", -".join(removed))
        return f"tags: {' '.join(parts)}"

    if field == "partner":
        old_set = {s for s in _normalize_partner_list(old) if s}
        new_set = {s for s in _normalize_partner_list(new) if s}
        if old_set == new_set:
            return None
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        parts = []
        if added:
            parts.append("+" + ", +".join(added))
        if removed:
            parts.append("-" + ", -".join(removed))
        return f"partner: {' '.join(parts)}"

    old_s = _normalize_str(old)
    new_s = _normalize_str(new)
    if old_s == new_s:
        return None
    return f"{field}: {old_s or '(none)'} → {new_s or '(none)'}"


def _bullets_from_note(note: str) -> list[str]:
    """Return formatted markdown bullet lines, preserving leading indentation.

    Leading whitespace is preserved (so nested bullets work in markdown rendering
    when indented by 4 spaces / one tab). Each returned line already includes
    the `- ` prefix.
    """
    bullets = []
    for raw_line in note.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        leading = line[: len(line) - len(line.lstrip())]
        stripped = line.lstrip()
        if stripped.startswith("- "):
            content = stripped[2:]
        elif stripped.startswith("-") and len(stripped) > 1:
            content = stripped[1:].lstrip()
        else:
            content = stripped
        bullets.append(f"{leading}- {content}")
    return bullets


def _insert_into_date(
    body: str, bullets: list[str], entry_date: str, entry_type: str = ""
) -> str:
    """Insert bullets under `## entry_date[, entry_type]`.

    `bullets` are pre-formatted markdown bullet lines (already include `- ` prefix
    and any leading indentation for nesting). If a section with the exact same
    heading (date + type) already exists, bullets are appended within it;
    otherwise a new section is created. Section order is reverse-chronological by
    date — within the same date, the new section is placed above existing
    same-date sections.
    """
    if not bullets:
        return body
    heading = f"## {entry_date}, {entry_type}" if entry_type else f"## {entry_date}"
    bullet_lines = list(bullets)

    lines = body.split("\n") if body else [""]

    # Exact-heading match (date + type) — append within.
    for i, line in enumerate(lines):
        if line.strip() == heading:
            end_idx = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    end_idx = j
                    break
            insert_at = i + 1
            for j in range(i + 1, end_idx):
                if lines[j].strip():
                    insert_at = j + 1
            return "\n".join(lines[:insert_at] + bullet_lines + lines[insert_at:])

    # No exact match. Insert before the first heading whose date is <= entry_date
    # (puts new section above existing same-date sections and all older ones).
    new_section = [heading] + bullet_lines
    insert_idx = None
    for i, line in enumerate(lines):
        m = DATE_HEADING_RE.match(line)
        if m and m.group(1) <= entry_date:
            insert_idx = i
            break

    if insert_idx is None:
        # entry_date is older than (or equal to) every section — append at end.
        if not body.strip():
            return "\n".join(new_section) + "\n"
        body_lines = body.rstrip("\n").split("\n")
        return "\n".join(body_lines + ["", *new_section]) + "\n"

    pre = lines[:insert_idx]
    post = lines[insert_idx:]
    while pre and not pre[-1].strip():
        pre.pop()

    parts = []
    if pre:
        parts.extend(pre)
        parts.append("")
    parts.extend(new_section)
    parts.append("")
    parts.extend(post)
    while parts and not parts[0].strip():
        parts.pop(0)
    return "\n".join(parts).rstrip() + "\n"


def split_sections(body: str) -> tuple[str, list[dict]]:
    """Split body into (preamble, sections).

    Each section is {heading, date, type, content} where content is the raw
    markdown between this heading and the next (no leading/trailing blanks).
    Preamble is any text before the first ## date heading.
    """
    preamble_lines: list[str] = []
    sections: list[dict] = []
    current: dict | None = None
    for line in (body or "").split("\n"):
        m = DATE_HEADING_RE.match(line)
        if m:
            if current is not None:
                sections.append(current)
            current = {
                "heading": line.strip(),
                "date": m.group(1),
                "type": (m.group(2) or "").strip(),
                "content_lines": [],
            }
        elif current is not None:
            current["content_lines"].append(line)
        else:
            preamble_lines.append(line)
    if current is not None:
        sections.append(current)

    for s in sections:
        lines = s.pop("content_lines")
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        s["content"] = "\n".join(lines)

    while preamble_lines and not preamble_lines[-1].strip():
        preamble_lines.pop()
    while preamble_lines and not preamble_lines[0].strip():
        preamble_lines.pop(0)
    return ("\n".join(preamble_lines), sections)


def _assemble_body(preamble: str, sections: list[dict]) -> str:
    parts: list[str] = []
    if preamble.strip():
        parts.append(preamble.rstrip())
    for s in sections:
        if parts:
            parts.append("")
        parts.append(s["heading"])
        if s["content"].strip():
            parts.append(s["content"].rstrip())
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def update_section(
    folder: Path,
    filename: str,
    section_index: int,
    new_date: str,
    new_type: str,
    new_content: str,
) -> None:
    """Replace section at `section_index` with new heading + content. If
    `new_content` is empty/whitespace, the section is deleted entirely. If
    the date changes, the section is moved to maintain reverse-chronological
    order. Updates `last-met` to the max date across remaining sections.
    """
    file_path = folder / filename
    post = frontmatter.load(str(file_path))
    body = post.content or ""
    preamble, sections = split_sections(body)
    if section_index < 0 or section_index >= len(sections):
        raise IndexError(f"section {section_index} out of range")

    new_content_clean = new_content.rstrip()
    if not new_content_clean.strip():
        sections.pop(section_index)
    else:
        try:
            date.fromisoformat(new_date)
        except (ValueError, TypeError):
            new_date = sections[section_index]["date"]
        new_type = " ".join((new_type or "").split()).lstrip(",").strip()
        new_heading = f"## {new_date}, {new_type}" if new_type else f"## {new_date}"

        old = sections[section_index]
        updated = {
            "heading": new_heading,
            "date": new_date,
            "type": new_type,
            "content": new_content_clean,
        }
        if new_date == old["date"]:
            sections[section_index] = updated
        else:
            sections.pop(section_index)
            insert_at = len(sections)
            for i, s in enumerate(sections):
                if s["date"] <= new_date:
                    insert_at = i
                    break
            sections.insert(insert_at, updated)

    new_body = _assemble_body(preamble, sections)

    meta = dict(post.metadata or {})
    new_last_met = max((s["date"] for s in sections), default="")
    if new_last_met:
        meta["last-met"] = new_last_met
    elif "last-met" in meta:
        del meta["last-met"]

    new_post = frontmatter.Post(new_body, **meta)
    file_path.write_text(
        frontmatter.dumps(new_post, sort_keys=False), encoding="utf-8"
    )


def _build_meta(header: dict, last_met: str, extras: dict) -> dict:
    """Assemble frontmatter dict in fixed display order."""
    meta = {}
    display_name = _normalize_str(header.get("display-name"))
    if display_name:
        meta["display-name"] = display_name
    partners = _normalize_partner_list(header.get("partner"))
    partner_files = _normalize_partner_list(header.get("partner-file"))
    partner_val = _emit_partner_value(partners)
    if partner_val is not None:
        meta["partner"] = partner_val
    for field in ["job", "location"]:
        v = _normalize_str(header.get(field))
        if v:
            meta[field] = v
    tags = _normalize_tags(header.get("tags"))
    if tags:
        meta["tags"] = tags
    partner_file_val = _emit_partner_value(partner_files)
    if partner_file_val is not None:
        meta["partner-file"] = partner_file_val
    if last_met:
        meta["last-met"] = last_met
    # Preserve any unknown frontmatter keys at the end.
    for k, v in extras.items():
        if k not in meta:
            meta[k] = v
    return meta


def save_person(
    folder: Path,
    filename: str,
    new_header: dict,
    manual_note: str,
    entry_date: str = "",
    entry_type: str = "",
) -> dict:
    """Save header + manual note. Auto-logs header diffs into the entry section.

    `entry_date` is a YYYY-MM-DD string; if empty/invalid, today is used.
    `entry_type` is an optional free-text label appended to the date heading
    (e.g. 'call', 'lunch'). Newlines/extra whitespace are collapsed.
    `last-met` is set to max(existing last-met, entry_date) when bullets are added.

    Returns a dict: {bullets, last_met, entry_date, entry_type}.
    """
    if entry_date:
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            entry_date = ""
    if not entry_date:
        entry_date = date.today().isoformat()

    entry_type = " ".join((entry_type or "").split()).lstrip(",").strip()

    existing = load_person(folder, filename)
    old_header = existing["header"]
    body = existing["body"]
    extras = existing["extras"]

    change_bullets = []
    for field in TRACKED_FIELDS:
        change = _format_change(field, old_header.get(field), new_header.get(field))
        if change:
            change_bullets.append(f"- {change}")

    note_bullets = _bullets_from_note(manual_note or "")
    all_bullets = change_bullets + note_bullets

    new_body = (
        _insert_into_date(body, all_bullets, entry_date, entry_type)
        if all_bullets else body
    )

    if all_bullets:
        existing_last = old_header.get("last-met") or ""
        last_met = max(existing_last, entry_date) if existing_last else entry_date
    else:
        last_met = old_header.get("last-met") or ""

    meta = _build_meta(new_header, last_met=last_met, extras=extras)
    post = frontmatter.Post(new_body, **meta)
    (folder / filename).write_text(
        frontmatter.dumps(post, sort_keys=False), encoding="utf-8"
    )

    _apply_partner_backlink(
        folder, filename,
        old_partner_files=_normalize_partner_list(old_header.get("partner-file")),
        new_partner_files=_normalize_partner_list(new_header.get("partner-file")),
        my_display_name=_normalize_str(new_header.get("display-name")),
    )

    return {
        "bullets": all_bullets,
        "last_met": last_met,
        "entry_date": entry_date,
        "entry_type": entry_type,
    }
