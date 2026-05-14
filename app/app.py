"""Flask app: Acquainted."""
from __future__ import annotations

import os
import sys
import threading
from datetime import date
from pathlib import Path

import markdown as md
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import config as cfg
import storage

app = Flask(__name__)
app.secret_key = "acquainted-localhost-only"  # localhost-only single-user


def _resolve_folder(loc_id: str) -> Path:
    loc = cfg.get_location(loc_id)
    if not loc:
        abort(404, description="Unknown location.")
    folder = Path(loc["path"])
    if not folder.is_dir():
        abort(404, description=f"Folder not found on disk: {folder}")
    return folder


def _safe_filename(filename: str) -> str:
    # Reject path-traversal: filenames are expected to be plain `*.md`.
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(400, description="Invalid filename.")
    if not filename.endswith(".md"):
        abort(400, description="Filename must end with .md")
    return filename


@app.route("/")
def welcome():
    return render_template("welcome.html", locations=cfg.list_locations())


@app.route("/__exit", methods=["POST"])
def exit_app():
    """Shut down the dev server so the user doesn't need Ctrl+C."""
    def _die():
        os._exit(0)
    threading.Timer(0.3, _die).start()
    return render_template("exit.html")


@app.route("/locations", methods=["POST"])
def add_location():
    name = request.form.get("name", "").strip()
    path = request.form.get("path", "").strip()
    try:
        cfg.add_location(name, path)
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("welcome"))


@app.route("/locations/<loc_id>/delete", methods=["POST"])
def delete_location(loc_id):
    cfg.delete_location(loc_id)
    return redirect(url_for("welcome"))


@app.route("/pick-folder", methods=["POST"])
def pick_folder():
    # Localhost-only single-user app: open the native Windows folder picker
    # via PowerShell + System.Windows.Forms (always available on Windows; has
    # a built-in "New Folder" button so the user can create a folder inline).
    import platform
    import subprocess

    if platform.system() != "Windows":
        return jsonify({"error": "Folder picker only supported on Windows."}), 500

    # Force UTF-8 on PS output; otherwise Console writes in the OEM codepage
    # (CP852 on Czech Windows: "Í" -> 0xD6, which decoded as CP1252 becomes "Ö",
    # breaking diacritics in chosen paths).
    ps_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.ShowNewFolderButton = $true; "
        "$f.Description = 'Choose folder'; "
        "$f.UseDescriptionForTitle = $true; "
        "[void]$f.ShowDialog(); "
        "[Console]::Out.Write($f.SelectedPath)"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-Command",
                ps_script,
            ],
            capture_output=True,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return jsonify({"error": str(e)}), 500

    raw = result.stdout or b""
    try:
        path_str = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        # Fallback: try the legacy Windows ANSI codepage.
        path_str = raw.decode("mbcs", errors="replace").strip()
    return jsonify({"path": path_str})


@app.route("/folder/<loc_id>/")
def list_view(loc_id):
    folder = _resolve_folder(loc_id)
    loc = cfg.get_location(loc_id)
    sort = request.args.get("sort", "name")
    q = (request.args.get("q") or "").strip()

    if q:
        results = storage.search_in_folder(folder, q)
        return render_template(
            "list.html",
            loc=loc,
            sort=sort,
            q=q,
            primary=results["primary"],
            body_matches=results["body_matches"],
            folder_path=str(folder),
            is_search=True,
        )

    people = storage.list_people(folder)
    if sort == "last-met":
        # People with last-met first (most recent first), then people without.
        people = sorted(
            people,
            key=lambda p: (not p["last_met"], -_ord_date(p["last_met"])),
        )
    return render_template(
        "list.html",
        loc=loc,
        people=people,
        sort=sort,
        folder_path=str(folder),
        is_search=False,
        q="",
    )


def _ord_date(d: str) -> int:
    """Convert YYYY-MM-DD to integer. Empty -> 0."""
    if not d:
        return 0
    try:
        y, m, day = d.split("-")
        return int(y) * 10000 + int(m) * 100 + int(day)
    except (ValueError, AttributeError):
        return 0


@app.route("/folder/<loc_id>/new", methods=["GET", "POST"])
def new_person(loc_id):
    folder = _resolve_folder(loc_id)
    loc = cfg.get_location(loc_id)
    existing_people = storage.list_people(folder)
    all_tags = sorted({t for p in existing_people for t in p.get("tags", [])})

    if request.method == "POST":
        header = _header_from_form(request.form)
        header = _resolve_partner_file(folder, header)
        if not header["display-name"]:
            flash("Display name is required.", "error")
            return render_template(
                "new.html", loc=loc, header=header, suggested_filename="",
                existing_people=existing_people, all_tags=all_tags,
            )
        filename = request.form.get("filename", "").strip()
        if not filename:
            filename = storage.slugify_filename(header["display-name"])
        elif not filename.endswith(".md"):
            filename += ".md"
        filename = _safe_filename(filename)
        try:
            storage.create_person(folder, filename, header)
        except FileExistsError as e:
            flash(str(e), "error")
            return render_template(
                "new.html", loc=loc, header=header,
                suggested_filename=filename[:-3],
                existing_people=existing_people, all_tags=all_tags,
            )
        return redirect(url_for("person_view", loc_id=loc_id, filename=filename))

    empty_header = {
        "display-name": "",
        "partner": [],
        "partner-file": [],
        "job": "",
        "location": "",
        "tags": [],
    }
    return render_template(
        "new.html", loc=loc, header=empty_header, suggested_filename="",
        existing_people=existing_people, all_tags=all_tags,
    )


@app.route("/folder/<loc_id>/<filename>", methods=["GET", "POST"])
def person_view(loc_id, filename):
    folder = _resolve_folder(loc_id)
    loc = cfg.get_location(loc_id)
    filename = _safe_filename(filename)

    if not (folder / filename).exists():
        abort(404, description=f"{filename} not found in {folder}")

    if request.method == "POST":
        header = _header_from_form(request.form)
        header = _resolve_partner_file(folder, header)
        manual_note = request.form.get("note", "")
        note_date = request.form.get("note-date", "").strip()
        note_type = request.form.get("note-type", "")
        result = storage.save_person(
            folder, filename, header, manual_note, note_date, note_type
        )
        if result["bullets"]:
            label = result["entry_date"]
            if result["entry_type"]:
                label += f", {result['entry_type']}"
            flash(
                f"Saved. Logged {len(result['bullets'])} entr"
                + ("y" if len(result["bullets"]) == 1 else "ies")
                + f" under {label}.",
                "success",
            )
        else:
            flash("No changes detected.", "info")
        return redirect(url_for("person_view", loc_id=loc_id, filename=filename))

    person = storage.load_person(folder, filename)
    preamble, raw_sections = storage.split_sections(person["body"])
    preamble_html = md.markdown(preamble) if preamble.strip() else ""
    sections = []
    for i, s in enumerate(raw_sections):
        sections.append({
            "index": i,
            "heading": s["heading"],
            "date": s["date"],
            "type": s["type"],
            "content": s["content"],
            "content_html": md.markdown(s["content"]) if s["content"].strip() else "",
        })
    all_people = storage.list_people(folder)
    existing_people = [p for p in all_people if p["filename"] != filename]
    all_tags = sorted({t for p in all_people for t in p.get("tags", [])})
    edit_mode = request.args.get("edit") == "1"

    # Resolve each partner to {name, filename_or_none}. Prefer the explicit
    # parallel partner-file ref; if missing, fall back to case-insensitive
    # display-name match against other people in this folder.
    partner_names = list(person["header"].get("partner") or [])
    partner_files = list(person["header"].get("partner-file") or [])
    n = max(len(partner_names), len(partner_files))
    partner_names = (partner_names + [""] * n)[:n]
    partner_files = (partner_files + [""] * n)[:n]

    partner_links = []
    for nm, pf in zip(partner_names, partner_files):
        if not nm and not pf:
            continue
        link = None
        if pf and (folder / pf).exists():
            link = pf
        elif nm:
            needle = nm.strip().lower()
            for p in all_people:
                if p["filename"] == filename:
                    continue
                if p["display_name"].strip().lower() == needle:
                    link = p["filename"]
                    break
        partner_links.append({"name": nm, "filename": link})

    return render_template(
        "person.html",
        loc=loc,
        filename=filename,
        header=person["header"],
        body=person["body"],
        preamble_html=preamble_html,
        sections=sections,
        existing_people=existing_people,
        all_tags=all_tags,
        today=date.today().isoformat(),
        edit_mode=edit_mode,
        partner_links=partner_links,
    )


@app.route("/folder/<loc_id>/<filename>/edit-section", methods=["POST"])
def edit_section(loc_id, filename):
    folder = _resolve_folder(loc_id)
    filename = _safe_filename(filename)
    if not (folder / filename).exists():
        abort(404, description=f"{filename} not found in {folder}")

    try:
        section_index = int(request.form.get("section-index", "-1"))
    except ValueError:
        abort(400, description="Invalid section index.")

    action = request.form.get("action", "save")
    new_date = request.form.get("section-date", "").strip()
    new_type = request.form.get("section-type", "").strip()
    new_content = request.form.get("section-content", "") if action != "delete" else ""

    try:
        storage.update_section(
            folder, filename, section_index, new_date, new_type, new_content
        )
    except IndexError:
        flash("Section not found (it may have been edited in another tab).", "error")
        return redirect(url_for("person_view", loc_id=loc_id, filename=filename))

    if action == "delete":
        flash("Note deleted.", "info")
    else:
        flash("Note updated.", "success")
    return redirect(url_for("person_view", loc_id=loc_id, filename=filename))


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated form value, preserving empty positional slots
    when the user typed an explicit empty entry (e.g. ", Bob"). Trailing
    blanks at the very end are dropped."""
    if not raw:
        return []
    items = [s.strip() for s in raw.split(",")]
    while items and items[-1] == "":
        items.pop()
    return items


def _header_from_form(form) -> dict:
    return {
        "display-name": form.get("display-name", "").strip(),
        "partner": _split_csv(form.get("partner", "")),
        "partner-file": _split_csv(form.get("partner-file", "")),
        "job": form.get("job", "").strip(),
        "location": form.get("location", "").strip(),
        "tags": [t.strip() for t in form.get("tags", "").split(",") if t.strip()],
    }


def _resolve_partner_file(folder: Path, header: dict) -> dict:
    """Sanity-check each `partner-file` entry and sync the matching `partner`
    name to its current display-name. Drops file refs that don't exist or look
    suspicious; the typed partner string in that slot is preserved (so a
    typed-but-unlinked partner stays visible). Mutates and returns header.
    """
    import frontmatter

    names = list(header.get("partner") or [])
    files = list(header.get("partner-file") or [])
    n = max(len(names), len(files))
    names = (names + [""] * n)[:n]
    files = (files + [""] * n)[:n]

    for i, pf in enumerate(files):
        if not pf:
            continue
        if "/" in pf or "\\" in pf or ".." in pf or not pf.endswith(".md"):
            files[i] = ""
            continue
        candidate = folder / pf
        if not candidate.exists():
            files[i] = ""
            continue
        try:
            post = frontmatter.load(str(candidate))
            current_name = (post.metadata or {}).get("display-name")
            if current_name:
                names[i] = str(current_name)
        except Exception:
            pass

    # Drop slots that ended up entirely empty.
    cleaned_names: list[str] = []
    cleaned_files: list[str] = []
    for nm, pf in zip(names, files):
        if nm or pf:
            cleaned_names.append(nm)
            cleaned_files.append(pf)
    header["partner"] = cleaned_names
    header["partner-file"] = cleaned_files
    return header


@app.template_filter("tags_inline")
def tags_inline(tags):
    if not tags:
        return ""
    return ", ".join(tags)


if __name__ == "__main__":
    # Allow `python app/app.py` from project root or `python app.py` from app/.
    sys.path.insert(0, str(Path(__file__).parent))
    app.run(host="127.0.0.1", port=5000, debug=False)
