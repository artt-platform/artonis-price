#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET


APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "artonis_price_mvp.sqlite"
DEFAULT_REMOTE = "gdrive_artonis:"
DEFAULT_SOURCES_PATH = "1. Sources"
EVENT_TEAM_DRIVE_ID = "0ACwQ4JPmZGinUk9PVA"
SOURCE_FILE_EXTENSIONS = (".xlsx", ".xls", ".csv", ".pdf", ".jpg", ".jpeg", ".png", ".docx", ".pptx")
PRICE_FILE_HINTS = ("price", "gia", "giá", "bang gia", "bảng giá", "pricelist", "bao gia")
CATALOGUE_HINTS = ("catalog", "catalogue", "cataloge", "catologue", "brochure", "pdf")

VND_TO_USD_RATE = 26000

XML_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XML_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def strip_accents(value):
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    # Vietnamese Đ/đ is a separate letter (not a diacritic) — map it to D/d
    return text.replace("Đ", "D").replace("đ", "d")


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", " ", strip_accents(value).lower()).strip()


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_date_token(token):
    token = clean_text(token)
    if re.fullmatch(r"\d{6}", token):
        return f"20{token[:2]}-{token[2:4]}-{token[4:6]}"
    if re.fullmatch(r"\d{8}", token):
        year = int(token[:4])
        if not 1900 <= year <= 2100:
            return f"{token[4:8]}-{token[2:4]}-{token[0:2]}"
        return f"{token[:4]}-{token[4:6]}-{token[6:8]}"
    return ""


def init_db(db_path=DB_PATH):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        create table if not exists exhibitions (
            id integer primary key,
            drive_path text unique,
            source_bucket text,
            code text,
            event_type text,
            date_token text,
            start_date text,
            city text,
            title text,
            artists_text text,
            organizer text,
            venue text,
            online_status text,
            artwork_count integer,
            metadata_json text,
            updated_at text
        );

        create table if not exists source_files (
            id integer primary key,
            exhibition_id integer,
            drive_path text unique,
            filename text,
            extension text,
            source_kind text,
            has_price_hint integer default 0,
            has_catalogue_hint integer default 0,
            imported_at text,
            foreign key(exhibition_id) references exhibitions(id)
        );

        create table if not exists artists (
            id integer primary key,
            name text not null unique,
            normalized_name text not null,
            exhibition_count integer default 0,
            price_count integer default 0,
            min_price real,
            max_price real,
            avg_price real,
            avg_price_per_m2 real,
            median_price_per_m2 real,
            updated_at text
        );

        create table if not exists price_observations (
            id integer primary key,
            artist_id integer,
            exhibition_id integer,
            source_file_id integer,
            artwork_title text,
            medium text,
            dimensions text,
            width_cm real,
            height_cm real,
            area_m2 real,
            price_per_m2 real,
            year text,
            price_amount real,
            currency text,
            status text,
            raw_row_json text,
            confidence real default 0.4,
            observed_at text,
            foreign key(artist_id) references artists(id),
            foreign key(exhibition_id) references exhibitions(id),
            foreign key(source_file_id) references source_files(id)
        );

        create table if not exists exhibition_artists (
            exhibition_id integer,
            artist_id integer,
            primary key (exhibition_id, artist_id),
            foreign key(exhibition_id) references exhibitions(id),
            foreign key(artist_id) references artists(id)
        );

        create table if not exists sale_results (
            id integer primary key,
            source text,
            source_url text unique,
            lot_number text,
            auction_title text,
            sale_date text,
            sale_location text,
            artist_id integer,
            artist_name_raw text,
            artwork_title text,
            medium text,
            dimensions text,
            width_cm real,
            height_cm real,
            area_m2 real,
            year text,
            estimate_low real,
            estimate_high real,
            hammer_price real,
            price_with_premium real,
            currency text,
            price_usd real,
            price_per_m2_usd real,
            status text,
            provenance text,
            raw_snapshot text,
            scraped_at text,
            foreign key(artist_id) references artists(id)
        );

        create table if not exists imports (
            id integer primary key,
            source text,
            detail text,
            status text,
            count integer,
            created_at text
        );

        create table if not exists upcoming_auctions (
            id integer primary key,
            source text not null,
            sale_page_url text unique,
            auction_title text,
            sale_date text,
            sale_location text,
            expected_lots integer,
            scraped_at text
        );

        create table if not exists crawl_runs (
            id integer primary key,
            source text not null,                 -- bonhams / aguttes / millon / ...
            target_slug text,                     -- specific catalog/artist slug if scoped
            started_at text,
            finished_at text,
            lots_scanned integer default 0,       -- raw lots returned by source
            lots_inserted integer default 0,      -- new VN-filtered lots written to DB
            sale_date_min text,                   -- earliest sale_date in this run's lots
            sale_date_max text,                   -- latest sale_date in this run's lots
            status text default 'ok',             -- 'ok' / 'error' / 'partial'
            note text                             -- error message or human note
        );
        """
    )
    # Lightweight migrations — add columns introduced after the original schema.
    existing_cols = {r[1] for r in conn.execute("pragma table_info(sale_results)")}
    if "kind" not in existing_cols:
        # painting (default 2D) | sculpture | work_on_paper | lacquer | other
        # Sculptures don't have meaningful area_m2; UI/Reports filter by this.
        conn.execute("alter table sale_results add column kind text default 'painting'")
    conn.commit()
    return conn


def db():
    return init_db()


def run_rclone(args, timeout=180):
    cmd = ["rclone"] + args
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def parse_exhibition_folder(path):
    parts = [p for p in path.strip("/").split("/") if p]
    folder = parts[-1] if parts else path
    source_bucket = parts[-2] if len(parts) >= 2 and parts[-2].lower().startswith("source") else ""
    code = folder
    match = re.match(r"^(EXH|EXT)_(\d{6})_([^_]+)_(.*?)_(.+)$", folder)
    if match:
        event_type, date_token, city, title, artists = match.groups()
        return {
            "drive_path": path.rstrip("/") + "/",
            "source_bucket": source_bucket,
            "code": code,
            "event_type": event_type,
            "date_token": date_token,
            "start_date": parse_date_token(date_token),
            "city": city,
            "title": clean_text(title.replace("_", " ")),
            "artists_text": clean_text(artists.replace("_", " ")),
        }
    match = re.match(r"^(EXH|EXT)_(\d{6})_([^_]+)_(.+)$", folder)
    if match:
        event_type, date_token, city, title = match.groups()
        return {
            "drive_path": path.rstrip("/") + "/",
            "source_bucket": source_bucket,
            "code": code,
            "event_type": event_type,
            "date_token": date_token,
            "start_date": parse_date_token(date_token),
            "city": city,
            "title": clean_text(title.replace("_", " ")),
            "artists_text": "",
        }
    generic = re.split(r"\s+[-–]\s+", folder)
    if len(generic) >= 4:
        artists = generic[0]
        date_token = generic[-2]
        venue = generic[-1]
        title = " - ".join(generic[1:-2])
        return {
            "drive_path": path.rstrip("/") + "/",
            "source_bucket": source_bucket or "Artonis-Event",
            "code": code,
            "event_type": "EXH",
            "date_token": date_token,
            "start_date": parse_date_token(date_token),
            "city": "",
            "title": clean_text(title),
            "artists_text": clean_text(artists),
            "venue": clean_text(venue),
        }
    if len(generic) == 2:
        return {
            "drive_path": path.rstrip("/") + "/",
            "source_bucket": source_bucket or "Artonis-Event",
            "code": code,
            "event_type": "EXH",
            "date_token": "",
            "start_date": "",
            "city": "",
            "title": clean_text(generic[1]),
            "artists_text": clean_text(generic[0]),
        }
    return {
        "drive_path": path.rstrip("/") + "/",
        "source_bucket": source_bucket,
        "code": code,
        "event_type": "EXH" if "/EXH_" in f"/{path}" else "EXT",
        "date_token": "",
        "start_date": "",
        "city": "",
        "title": clean_text(folder.replace("_", " ")),
        "artists_text": "",
    }


VN_SURNAMES = (
    "Nguyễn","Trần","Lê","Phạm","Hồ","Võ","Đặng","Đỗ","Bùi","Đoàn",
    "Hoàng","Huỳnh","Ngô","Vũ","Dương","Cao","Trương","Mai","Lý","Tào",
    "Doãn","Lương","Đinh","Lâm","Phan","Tô","Đào","Tạ","Quách","Ung",
    "Uyên","Ma","Chu","Cồ","Khổng","Tôn","Lã","Lại","Kiều",
)


def _split_by_vn_surnames(text):
    """Split concatenated Vietnamese names on surname boundaries.
    Returns the original text as a single-item list unless EVERY resulting chunk has ≥ 2 words —
    this avoids wrongly splitting 'Huỳnh Lê Nhật Tấn' (solo artist, Lê is middle name)."""
    pattern = r"(?=\b(?:" + "|".join(re.escape(s) for s in VN_SURNAMES) + r")\b)"
    parts = re.split(pattern, text)
    parts = [clean_text(p) for p in parts if clean_text(p)]
    if len(parts) <= 1:
        return parts or [text]
    # Only accept split if every chunk has 2+ words (real Vietnamese names are ≥2 words)
    if all(len(p.split()) >= 2 for p in parts):
        return parts
    # Otherwise: surname appeared in middle position of a single name → keep as one
    return [text]


def split_artists(value):
    """Split a concatenated artist names string into individual artist names.
    Handles: 'A & B', 'A và B', 'A, B', 'A + B', 'A (1940-2024) B (1950) C', 'A\nB\nC',
    and bare concatenations like 'Phạm Thanh Toàn Nguyễn Ngọc Dân'."""
    text = clean_text(value)
    if not text:
        return []

    # Case 1: year-paren delimits each artist, e.g. "Hồ Hữu Thủ (1940 - 2024) Nguyễn Lâm (1941) Lê Thanh (1942)"
    if re.search(r"\(\s*\d{4}", text):
        # Strip alt-name parens like "(Huỳnh Văn Mười)" before splitting
        stripped = re.sub(r"\([^)0-9]+\)", "", text)
        # Each artist ends at the closing ) of its year paren — split on ")"
        raw_chunks = stripped.split(")")
        names = []
        for chunk in raw_chunks:
            # Take everything before "(" as the name
            before_paren = chunk.split("(")[0]
            name = clean_text(before_paren)
            if name and len(name) > 1:
                names.append(name)
        return names

    # Case 2: newline-separated
    if "\n" in text:
        items = [clean_text(x) for x in text.split("\n")]
        items = [x for x in items if x and len(x) > 1]
        if len(items) >= 2:
            return items

    # Case 3: normalize common separators to comma
    normalized = re.sub(r"\s*&\s*", ",", text)
    normalized = re.sub(r"\s+và\s+", ",", normalized, flags=re.I)
    normalized = re.sub(r"\s+and\s+", ",", normalized, flags=re.I)
    normalized = re.sub(r"\s+\+\s+", ",", normalized)
    items = [clean_text(x) for x in re.split(r"[,;/]+", normalized)]
    items = [x for x in items if x and len(x) > 1]

    # Case 4: if still one item and it has multiple VN surnames → split by surname
    if len(items) == 1:
        parts = _split_by_vn_surnames(items[0])
        if len(parts) >= 2:
            return parts

    return items


_NON_ARTIST_KEYWORDS = (
    "bao tang", "museum", "studio", "gallery", "galerie", "to chuc",
    "foundation", "centre", "center", "institut", "institute",
    "sotheby", "christie", "drouot", "auction house",
    "truong my thuat", "school of",
)


def looks_like_organizer(name):
    """True if name is likely an organizer/venue/studio/gallery, not an artist."""
    norm = normalize_key(name)
    if any(kw in norm for kw in _NON_ARTIST_KEYWORDS):
        return True
    # Heuristic: very long names (>8 words) are likely multi-artist concat that
    # the splitter failed to break apart — refuse to save as single artist.
    if len(norm.split()) > 8:
        return True
    return False


def upsert_artist(conn, name):
    name = clean_text(name)
    if not name:
        return None
    if looks_like_organizer(name):
        print(f"  SKIP non-artist string: {name!r}")
        return None
    normalized = normalize_key(name)
    row = conn.execute("select id from artists where normalized_name = ?", (normalized,)).fetchone()
    if row:
        conn.execute("update artists set name = ?, updated_at = ? where id = ?", (name, now_iso(), row["id"]))
        return row["id"]
    conn.execute(
        """
        insert into artists(name, normalized_name, updated_at)
        values (?, ?, ?)
        on conflict(name) do update set normalized_name=excluded.normalized_name, updated_at=excluded.updated_at
        """,
        (name, normalized, now_iso()),
    )
    row = conn.execute("select id from artists where name = ?", (name,)).fetchone()
    return row["id"] if row else None


def upsert_exhibition(conn, item):
    conn.execute(
        """
        insert into exhibitions(
            drive_path, source_bucket, code, event_type, date_token, start_date, city,
            title, artists_text, organizer, venue, online_status, artwork_count,
            metadata_json, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(drive_path) do update set
            source_bucket=excluded.source_bucket,
            code=excluded.code,
            event_type=excluded.event_type,
            date_token=excluded.date_token,
            start_date=coalesce(nullif(excluded.start_date, ''), exhibitions.start_date),
            city=coalesce(nullif(excluded.city, ''), exhibitions.city),
            title=coalesce(nullif(excluded.title, ''), exhibitions.title),
            artists_text=coalesce(nullif(excluded.artists_text, ''), exhibitions.artists_text),
            organizer=coalesce(nullif(excluded.organizer, ''), exhibitions.organizer),
            venue=coalesce(nullif(excluded.venue, ''), exhibitions.venue),
            online_status=coalesce(nullif(excluded.online_status, ''), exhibitions.online_status),
            artwork_count=coalesce(excluded.artwork_count, exhibitions.artwork_count),
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            item.get("drive_path", ""),
            item.get("source_bucket", ""),
            item.get("code", ""),
            item.get("event_type", ""),
            item.get("date_token", ""),
            item.get("start_date", ""),
            item.get("city", ""),
            item.get("title", ""),
            item.get("artists_text", ""),
            item.get("organizer", ""),
            item.get("venue", ""),
            item.get("online_status", ""),
            item.get("artwork_count"),
            json.dumps(item.get("metadata", {}), ensure_ascii=False),
            now_iso(),
        ),
    )
    row = conn.execute("select id from exhibitions where drive_path = ?", (item.get("drive_path", ""),)).fetchone()
    exhibition_id = row["id"] if row else None
    if exhibition_id:
        conn.execute("delete from exhibition_artists where exhibition_id = ?", (exhibition_id,))
    for artist in split_artists(item.get("artists_text", "")):
        artist_id = upsert_artist(conn, artist)
        if exhibition_id and artist_id:
            conn.execute(
                "insert or ignore into exhibition_artists(exhibition_id, artist_id) values (?, ?)",
                (exhibition_id, artist_id),
            )
    return exhibition_id


def classify_source_file(path):
    lower = normalize_key(path)
    ext = Path(path).suffix.lower()
    has_price_hint = any(normalize_key(h) in lower for h in PRICE_FILE_HINTS)
    has_catalogue_hint = ext == ".pdf" or any(normalize_key(h) in lower for h in CATALOGUE_HINTS)
    if ext in (".xlsx", ".xls", ".csv"):
        kind = "price_table" if has_price_hint else "spreadsheet"
    elif ext == ".pdf":
        kind = "price_catalogue" if has_price_hint else "catalogue"
    elif ext in (".jpg", ".jpeg", ".png"):
        kind = "price_image" if has_price_hint else "image"
    elif ext in (".docx", ".pptx"):
        kind = "document"
    else:
        kind = "other"
    return ext, kind, int(has_price_hint), int(has_catalogue_hint)


def upsert_source_file(conn, exhibition_id, path):
    ext, kind, price_hint, catalogue_hint = classify_source_file(path)
    conn.execute(
        """
        insert into source_files(exhibition_id, drive_path, filename, extension, source_kind, has_price_hint, has_catalogue_hint, imported_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(drive_path) do update set
            exhibition_id=excluded.exhibition_id,
            filename=excluded.filename,
            extension=excluded.extension,
            source_kind=excluded.source_kind,
            has_price_hint=excluded.has_price_hint,
            has_catalogue_hint=excluded.has_catalogue_hint,
            imported_at=excluded.imported_at
        """,
        (exhibition_id, path, Path(path).name, ext, kind, price_hint, catalogue_hint, now_iso()),
    )
    row = conn.execute("select id from source_files where drive_path = ?", (path,)).fetchone()
    return row["id"] if row else None


def maybe_reset_event_data(conn):
    conn.executescript(
        """
        delete from price_observations;
        delete from source_files;
        delete from exhibitions;
        delete from artists;
        delete from imports;
        """
    )
    conn.commit()


def rclone_drive_flags(args):
    team_drive = getattr(args, "team_drive", "") or ""
    return ["--drive-team-drive", team_drive] if team_drive else []


def scan_drive(args):
    conn = db()
    if args.reset:
        maybe_reset_event_data(conn)
    base = args.base.strip("/")
    remote_prefix = args.remote.rstrip(":") + ":"
    remote_path = f"{remote_prefix}{base}" if base else remote_prefix
    listed = run_rclone(["lsf", remote_path, "--recursive", "--max-depth", "2"] + rclone_drive_flags(args), timeout=240)
    raw_lines = [line.strip() for line in listed.splitlines() if line.strip()]
    folder_names = set()
    file_paths = []
    for line in raw_lines:
        if line.endswith("/"):
            folder_names.add(line.strip("/"))
            continue
        ext = Path(line).suffix.lower()
        if ext in SOURCE_FILE_EXTENSIONS and "/" in line:
            folder_names.add(line.split("/", 1)[0])
            file_paths.append(line)
    folders = sorted(folder_names)
    if args.limit:
        folders = folders[: args.limit]
    folder_ids = {}
    file_count = 0
    for folder in folders:
        item = parse_exhibition_folder(folder)
        exhibition_id = upsert_exhibition(conn, item)
        folder_ids[folder] = exhibition_id
    for rel in file_paths:
        root_folder = rel.split("/", 1)[0]
        if root_folder not in folder_ids:
            continue
        file_count += 1
        drive_path = f"{base}/{rel}".strip("/") if base else rel
        upsert_source_file(conn, folder_ids[root_folder], drive_path)
    conn.execute(
        "insert into imports(source, detail, status, count, created_at) values (?, ?, ?, ?, ?)",
        ("drive", remote_path, "ok", len(folders), now_iso()),
    )
    conn.commit()
    refresh_artist_stats(conn)
    print(f"Scanned {len(folders)} exhibition folders and {file_count} source files.")


def col_index(cell_ref):
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value


def read_xlsx(path):
    rows_by_sheet = {}
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{XML_MAIN}si"):
                parts = []
                for node in si.iter():
                    if node.tag == f"{XML_MAIN}t" and node.text:
                        parts.append(node.text)
                shared.append("".join(parts))

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rels = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_root.findall(f"{REL_NS}Relationship")}
        sheets = []
        for sheet in workbook.findall(f"{XML_MAIN}sheets/{XML_MAIN}sheet"):
            name = sheet.attrib.get("name", "Sheet")
            rid = sheet.attrib.get(f"{XML_REL}id")
            target = rels.get(rid, "")
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            sheets.append((name, target))

        for name, target in sheets:
            if target not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(target))
            rows = []
            for row in root.findall(f".//{XML_MAIN}row"):
                cells = {}
                max_col = 0
                for cell in row.findall(f"{XML_MAIN}c"):
                    ref = cell.attrib.get("r", "")
                    idx = col_index(ref)
                    max_col = max(max_col, idx)
                    value_node = cell.find(f"{XML_MAIN}v")
                    inline_node = cell.find(f"{XML_MAIN}is/{XML_MAIN}t")
                    value = ""
                    if inline_node is not None and inline_node.text:
                        value = inline_node.text
                    elif value_node is not None and value_node.text is not None:
                        raw = value_node.text
                        if cell.attrib.get("t") == "s":
                            value = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
                        else:
                            value = raw
                    cells[idx] = clean_text(value)
                if cells:
                    rows.append([cells.get(i, "") for i in range(1, max_col + 1)])
            rows_by_sheet[name] = rows
    return rows_by_sheet


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {"CSV": [[clean_text(cell) for cell in row] for row in csv.reader(fh)]}


def find_header(rows):
    best_idx = -1
    best_score = 0
    for idx, row in enumerate(rows[:30]):
        keys = [normalize_key(c) for c in row]
        score = 0
        for key in keys:
            if any(word in key for word in ("hoa si", "artist", "ten tac gia", "tac gia")):
                score += 3
            if any(word in key for word in ("gia", "price", "usd", "vnd")):
                score += 3
            if any(word in key for word in ("ten tac pham", "artwork", "title", "tac pham")):
                score += 2
            if any(word in key for word in ("kich thuoc", "dimension", "dimention", "size", "phys")):
                score += 1
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx if best_score >= 3 else -1


def map_headers(header):
    mapping = {}
    for idx, name in enumerate(header):
        key = normalize_key(name)
        if not key:
            continue
        if any(token in key for token in ("hoa si", "artist", "ten tac gia", "tac gia")):
            mapping["artist"] = idx
        elif any(token in key for token in ("ten tac pham", "artwork", "title", "tac pham")):
            mapping["artwork_title"] = idx
        elif any(token in key for token in ("chat lieu", "medium", "material")):
            mapping["medium"] = idx
        elif any(token in key for token in ("kich thuoc", "dimension", "dimention", "size", "phys")):
            mapping["dimensions"] = idx
        elif key in ("nam", "year") or "sang tac" in key:
            mapping["year"] = idx
        elif any(token in key for token in ("gia", "price", "usd", "vnd")):
            mapping["price"] = idx
        elif any(token in key for token in ("trang thai", "status", "sold", "available")):
            mapping["status"] = idx
    return mapping


def parse_price(value):
    text = clean_text(value)
    if not text:
        return None, ""
    currency = ""
    low = text.lower()
    if "$" in text or "usd" in low:
        currency = "USD"
    elif "vnd" in low or "vnđ" in low or "đ" in low:
        currency = "VND"
    elif "eur" in low or "€" in text:
        currency = "EUR"
    numbers = re.findall(r"\d[\d.,]*", text)
    if not numbers:
        return None, currency
    raw = numbers[0]
    # Handle Vietnamese thousands separator: "2.000" → 2000, "4.500" → 4500
    # Rule: if period is followed by exactly 3 digits (thousands sep), remove it
    # If comma present alongside period, comma is thousands sep
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", "")
    elif "." in raw:
        parts = raw.split(".")
        # All parts after first are 3 digits → all are thousands separators
        if all(len(p) == 3 for p in parts[1:]):
            raw = raw.replace(".", "")
    try:
        amount = float(raw)
    except ValueError:
        return None, currency
    return amount, currency


def to_usd(amount, currency):
    if currency == "VND" and amount:
        return round(amount / VND_TO_USD_RATE, 2), "USD"
    return amount, currency


# Sources where the dim string is written as Height × Width (most catalogues:
# Bonhams, Sotheby's, French houses, plus all Invaluable upstream houses that
# don't have their own per-source crawler).  Christie's labels W and H
# explicitly in JSON, so its parser sets order itself.  Le Auction reads from
# item.width / item.height — both explicit, so the order doesn't matter.
_HW_FIRST_SOURCES = frozenset({
    "bonhams", "sothebys", "aguttes", "drouot", "gros-delettrez",
    "tajan", "artcurial", "millon", "osenat", "invaluable",
})


def parse_dimensions(text, source=None):
    """Parse a dim string like '200x100cm' / '45 x 55 cm' / '132cm x 73cm' →
    (width_cm, height_cm).  Returns (None, None) when not parseable.

    Most auction catalogues write H × W in the text — the user-facing display
    convention.  For those sources (declared in _HW_FIRST_SOURCES above) we
    interpret the FIRST number as height and the SECOND as width.  Sources
    that explicitly label their values (Christie's JSON, Le Auction API)
    set width/height before this function and don't depend on the order."""
    if not text:
        return None, None
    t = clean_text(text).lower()
    t = re.sub(r"[×*]", "x", t)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:cm)?\s*x\s*(\d+(?:[.,]\d+)?)\s*(?:cm)?", t)
    if not m:
        return None, None
    try:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
        if source in _HW_FIRST_SOURCES:
            # First = height, second = width.
            w, h = b, a
        else:
            # W × H — Christie's text fallback, Le Auction reconstructed
            # strings, anything else with no declared convention.
            w, h = a, b
        if 3 <= w <= 500 and 3 <= h <= 500:
            return w, h
    except ValueError:
        pass
    return None, None


def compute_area_and_price_per_m2(dimensions, price_amount, source=None):
    """Return (width_cm, height_cm, area_m2, price_per_m2) — any may be None.
    `source` lets parse_dimensions pick H × W or W × H interpretation."""
    w, h = parse_dimensions(dimensions, source=source)
    if w is None or h is None:
        return None, None, None, None
    area_m2 = round((w * h) / 10000, 4)
    price_per_m2 = round(price_amount / area_m2, 2) if price_amount and area_m2 > 0 else None
    return w, h, area_m2, price_per_m2


def _extract_price_from_text(text):
    """Return (amount, currency) from free text, preferring USD over VND."""
    pm = re.search(r"(?:gi[aá]\s+b[aá]n\s*[:\s]+)?(\d[\d.,]+)\s*(?:usd|vnđ|vnd|đ\b)", text, re.IGNORECASE)
    if pm:
        return parse_price(pm.group(0))
    pm2 = re.search(r"gi[aá]\s+b[aá]n\s*[:\s]+([\d.,]+)", text, re.IGNORECASE)
    if pm2:
        return parse_price(pm2.group(1) + " usd")
    return None, "USD"


def _parse_format_abbreviation(text):
    """Format: N. Tp. {title}; Cl. {medium}; Kt. {dims}; St-{year}; Giá bán: {price}"""
    m = re.search(r"Tp[.\s]+(.+?)(?:\s*;|\s*Cl\.)", text, re.IGNORECASE)
    if not m:
        return None
    title = clean_text(m.group(1))
    m2 = re.search(r"Cl[.\s]+(.+?)(?:\s*;|\s*Kt\.)", text, re.IGNORECASE)
    medium = clean_text(m2.group(1)) if m2 else ""
    m3 = re.search(r"Kt[.\s]+(.+?)(?:\s*;|\s*St[-\s])", text, re.IGNORECASE)
    dims = clean_text(m3.group(1)) if m3 else ""
    m4 = re.search(r"St[-\s]+(\d{4})", text, re.IGNORECASE)
    year = m4.group(1) if m4 else ""
    amount, currency = _extract_price_from_text(text)
    if not title or amount is None:
        return None
    return {"artwork_title": title, "medium": medium, "dimensions": dims,
            "year": year, "price_amount": amount, "currency": currency, "status": ""}


def _parse_format_labeled(text):
    """Format: Tên tác phẩm/Tên tranh: … Chất liệu: … Kích thước: … Năm sáng tác: … Giá bán: …
    Uses accent-stripped text for pattern matching to handle all Vietnamese diacritics."""
    norm = strip_accents(text)  # ASCII-safe for regex

    def extract_between(label_pattern, stop_patterns, src=norm, original=text):
        """Find label in norm, return value slice from original text."""
        m = re.search(label_pattern, src, re.IGNORECASE)
        if not m:
            return ""
        start = m.end()
        # Skip leading colon/space
        sub_norm = src[start:]
        sub_orig = original[start:]
        stop = len(sub_norm)
        for sp in stop_patterns:
            sm = re.search(sp, sub_norm, re.IGNORECASE)
            if sm:
                stop = min(stop, sm.start())
        return clean_text(sub_orig[:stop])

    stops_after_title = [r"Chat\s+lieu", r"Kich\s+thu", r"Nam\s+sang", r"Gia\s+ban"]
    title = extract_between(r"Ten\s+(?:tac\s+pham|tranh)\s*[:\s]+", stops_after_title)
    if not title:
        return None

    medium = extract_between(r"Chat\s+lieu\s*[:\s]+", [r"Kich\s+thu", r"Nam\s+sang", r"Gia\s+ban"])
    dims = extract_between(r"Kich\s+thu[ooc]+\s*[:\s]+", [r"Nam\s+sang", r"Chat\s+lieu", r"Gia\s+ban"])
    m4 = re.search(r"Nam\s+sang\s+tac\s*[:\s]+(\d{4})", norm, re.IGNORECASE)
    year = m4.group(1) if m4 else ""
    amount, currency = _extract_price_from_text(text)
    if not title or amount is None:
        return None
    return {"artwork_title": title, "medium": medium, "dimensions": dims,
            "year": year, "price_amount": amount, "currency": currency, "status": ""}


def _parse_format_inline_usd(text):
    """Inline artwork format. Two sub-variants:
    A) $-prefix: "… $ 30,000 Title, Year  [VND amount]  medium  dims cm"  (Lý Trực Sơn, Trần Văn Thảo)
    B) USD-suffix: "Title  medium  WxH cm  Year  <amount> USD"           (Dương Thùy Dương)
    Multiple artworks per page.
    """
    results = []

    # Variant A: $ prefix, VND middle optional
    pat_a = re.compile(
        r"\$\s*([\d,]+)\s+"                     # $ price
        r"(.+?),?\s*(\d{4})\s+"                 # title, year
        r"(?:VND\s*[\d,]+\s+)?"                 # optional VND amount
        r"(.+?)\s+"                              # medium
        r"(\d+\s*(?:cm)?\s*[x×X]\s*\d+\s*(?:cm|in))",  # dimensions: "150cm x 150cm" or "150 x 150 cm"
        re.IGNORECASE
    )
    for m in pat_a.finditer(text):
        try: amount = float(m.group(1).replace(",", ""))
        except: continue
        title = clean_text(m.group(2))
        if title and amount:
            results.append({"artwork_title": title, "medium": clean_text(m.group(4)),
                            "dimensions": clean_text(m.group(5)), "year": m.group(3),
                            "price_amount": amount, "currency": "USD", "status": ""})

    if results:
        return results

    # Variant B: "... dims (cm) year amount USD"
    pat_b = re.compile(
        r"([A-ZÀ-Ỹ][\w\s\-\(\)àáạảãăắằẳẵặâấầẩẫậèéẹẻẽêếềểễệđìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹÀ-Ỹ/]{4,50}?)\s+"  # title
        r"(?:[A-Za-zà-ỹÀ-Ỹ][\w\s\-/]*?)\s+"     # medium-ish (noise tolerated)
        r"(\d+\s*[x×]\s*\d+)\s*cm\s+"            # dims
        r"(\d{4})\s+"                             # year
        r"(\d[\d,]*)\s*USD",                      # price USD
        re.IGNORECASE
    )
    for m in pat_b.finditer(text):
        try: amount = float(m.group(4).replace(",", ""))
        except: continue
        title = clean_text(m.group(1))
        if title and amount and len(title) > 3:
            results.append({"artwork_title": title, "medium": "",
                            "dimensions": m.group(2) + " cm", "year": m.group(3),
                            "price_amount": amount, "currency": "USD", "status": ""})

    return results


def _ocr_pdf_pages(path, dpi=200, lang="vie+eng"):
    """Render each PDF page to image and run tesseract OCR.
    Yields (page_index, text) tuples. Requires tesseract + pdf2image + poppler."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        raise RuntimeError("OCR requires: pip install pytesseract pdf2image; brew install tesseract tesseract-lang poppler")

    # Ensure tesseract is found (PATH may not include /opt/homebrew/bin in Python env)
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if os.path.exists(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            break

    # Find poppler path for pdf2image
    poppler_path = None
    for p in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.exists(os.path.join(p, "pdfinfo")):
            poppler_path = p
            break

    kwargs = {"dpi": dpi}
    if poppler_path:
        kwargs["poppler_path"] = poppler_path
    images = convert_from_path(str(path), **kwargs)
    for i, img in enumerate(images):
        try:
            text = pytesseract.image_to_string(img, lang=lang)
        except Exception as e:
            text = ""
        yield i, text


def _parse_ocr_price_line(text):
    """Parse OCR'd text from a single page. OCR is noisy; look for price anchors.
    Handles formats like '7.000 $', '$ 7000', '7,500 USD', '182.000.000 VNĐ'.
    Returns list of artwork dicts (often just one per page)."""
    # Find all price candidates
    price_patterns = [
        re.compile(r"([\d][\d.,]{2,})\s*\$(?!\w)"),           # "7.000 $"
        re.compile(r"\$\s*([\d][\d.,]{2,})"),                  # "$ 7,000"
        re.compile(r"([\d][\d.,]{3,})\s*(?:USD|usd)"),         # "7500 USD"
        re.compile(r"([\d][\d.,]{5,})\s*(?:VND|VNĐ|vnd|vnđ|đ)\b"),  # "182,000,000 VND"
    ]
    currencies = ["USD", "USD", "USD", "VND"]

    results = []
    # Split text into lines, search each for prices
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        for pat, curr in zip(price_patterns, currencies):
            m = pat.search(line)
            if not m:
                continue
            amount, _ = parse_price(m.group(1) + (" USD" if curr == "USD" else " VND"))
            if amount is None or amount < 10:  # sanity
                continue
            # Context: everything else on this page could be title/medium/dims
            results.append({
                "price_amount": amount, "currency": curr,
                "raw_line": line,
            })
            break  # one price per line
    return results


def _merge_ocr_with_page_context(ocr_text, page_text_from_layer=""):
    """Combine OCR'd price findings with structured context from text layer (if available).
    For files where text layer has title/medium but prices are images."""
    artworks = []
    price_hits = _parse_ocr_price_line(ocr_text)
    if not price_hits:
        return []

    # Try to extract artwork details from either text layer or OCR
    combined = (page_text_from_layer + "\n" + ocr_text).strip()
    flat = " ".join(combined.split())

    # Try structured parsers first
    labeled = _parse_format_labeled(flat)
    if labeled:
        # Override price with OCR price (more reliable than text layer for these files)
        labeled["price_amount"] = price_hits[0]["price_amount"]
        labeled["currency"] = price_hits[0]["currency"]
        return [labeled]

    # No structured format: just record the price with minimal metadata
    for hit in price_hits:
        # Try to grab title (first non-numeric capitalized line from combined text)
        title = ""
        for line in combined.split("\n"):
            line = line.strip()
            if line and not re.search(r"\$|USD|VND", line, re.IGNORECASE):
                if re.search(r"[A-Za-zÀ-Ỹà-ỹ]{4,}", line):
                    title = clean_text(line)[:80]
                    break
        # Dimensions hint
        dm = re.search(r"(\d{2,3}\s*[x×]\s*\d{2,3})\s*cm", flat, re.IGNORECASE)
        dims = dm.group(1) + " cm" if dm else ""
        # Year hint
        ym = re.search(r"\b(20\d{2})\b", flat)
        year = ym.group(1) if ym else ""
        artworks.append({
            "artwork_title": title or "Unknown",
            "medium": "",
            "dimensions": dims,
            "year": year,
            "price_amount": hit["price_amount"],
            "currency": hit["currency"],
            "status": "",
        })
    return artworks


def parse_pdf_price_catalogue(path, use_ocr_fallback=True, ocr_verbose=False):
    """Parse price catalogue PDF — tries multiple text-layer formats, then OCR fallback.
    Text-layer formats:
    1. Abbreviation style: Tp./Cl./Kt./St-/Giá bán (Khổng Đỗ Duy)
    2. Labeled style: Tên tác phẩm/Chất liệu/Kích thước/Năm/Giá bán (Trần Thế Vĩnh, Nguyễn Thị Thu Hà)
    3. Inline USD style: $ price Title, Year [VND ...] Medium Dims (Lý Trực Sơn, Trần Văn Thảo)
    OCR fallback: if text-layer yields no results, render each page and OCR.
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber required: pip install pdfplumber")

    artworks = []
    page_texts = []  # keep for OCR fallback context
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            page_texts.append(text)
            if not text.strip():
                continue
            flat = " ".join(text.split())

            inline = _parse_format_inline_usd(flat)
            if inline:
                artworks.extend(inline)
                continue

            aw = _parse_format_labeled(flat)
            if aw:
                artworks.append(aw)
                continue

            aw = _parse_format_abbreviation(flat)
            if aw:
                artworks.append(aw)

    if artworks or not use_ocr_fallback:
        return artworks

    # OCR fallback
    if ocr_verbose:
        print(f"  [OCR] text-layer parse yielded 0 — running OCR on {path.name if hasattr(path,'name') else path}")
    try:
        for i, ocr_text in _ocr_pdf_pages(path):
            layer_text = page_texts[i] if i < len(page_texts) else ""
            page_artworks = _merge_ocr_with_page_context(ocr_text, layer_text)
            artworks.extend(page_artworks)
            if ocr_verbose and page_artworks:
                for a in page_artworks:
                    print(f"    OCR p{i+1}: '{a['artwork_title'][:40]}' {a['price_amount']} {a['currency']}")
    except Exception as e:
        if ocr_verbose:
            print(f"  [OCR ERROR] {e}")

    return artworks


def _insert_observation(conn, artist_id, exhibition_id, source_file_id, artwork):
    amount, currency = to_usd(artwork["price_amount"], artwork["currency"])
    dims = artwork.get("dimensions", "")
    width_cm, height_cm, area_m2, price_per_m2 = compute_area_and_price_per_m2(dims, amount)
    conn.execute(
        """
        insert into price_observations(
            artist_id, exhibition_id, source_file_id, artwork_title, medium, dimensions,
            width_cm, height_cm, area_m2, price_per_m2,
            year, price_amount, currency, status, raw_row_json, confidence, observed_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artist_id,
            exhibition_id,
            source_file_id,
            artwork.get("artwork_title", ""),
            artwork.get("medium", ""),
            dims,
            width_cm, height_cm, area_m2, price_per_m2,
            artwork.get("year", ""),
            amount,
            currency,
            artwork.get("status", ""),
            artwork.get("raw_row_json", "{}"),
            0.7,
            now_iso(),
        ),
    )


def import_price_file(conn, local_path, source_drive_path="", exhibition_drive_path="", artist_name=""):
    local = Path(local_path)

    exhibition_id = None
    if exhibition_drive_path:
        # Try both NFC and NFD forms — rclone may store paths in decomposed (NFD) form
        for form in ("NFC", "NFD"):
            normalized = unicodedata.normalize(form, exhibition_drive_path.rstrip("/") + "/")
            row = conn.execute("select id from exhibitions where drive_path = ?", (normalized,)).fetchone()
            if row:
                exhibition_id = row["id"]
                break
    source_file_id = None
    if source_drive_path:
        source_file_id = upsert_source_file(conn, exhibition_id, source_drive_path)

    inserted = 0

    # PDF catalogue path
    if local.suffix.lower() == ".pdf":
        artworks = parse_pdf_price_catalogue(local)
        fallback_artist = artist_name or ""
        if not fallback_artist and exhibition_id:
            erow = conn.execute("select artists_text from exhibitions where id = ?", (exhibition_id,)).fetchone()
            artists = split_artists(erow["artists_text"] if erow else "")
            fallback_artist = artists[0] if len(artists) == 1 else ""
        for aw in artworks:
            a = fallback_artist
            if not a:
                continue
            artist_id = upsert_artist(conn, a)
            aw["raw_row_json"] = json.dumps(aw, ensure_ascii=False)
            _insert_observation(conn, artist_id, exhibition_id, source_file_id, aw)
            inserted += 1
        conn.commit()
        return inserted

    # Excel / CSV path
    if local.suffix.lower() == ".csv":
        sheets = read_csv(local)
    else:
        sheets = read_xlsx(local)

    for sheet_name, rows in sheets.items():
        header_idx = find_header(rows)
        if header_idx < 0:
            continue
        header = rows[header_idx]
        mapping = map_headers(header)
        if "price" not in mapping:
            continue
        fallback_artist = artist_name or ""
        if not fallback_artist and exhibition_id:
            erow = conn.execute("select artists_text from exhibitions where id = ?", (exhibition_id,)).fetchone()
            artists = split_artists(erow["artists_text"] if erow else "")
            fallback_artist = artists[0] if len(artists) == 1 else ""
        for row in rows[header_idx + 1 :]:
            def get(field):
                idx = mapping.get(field)
                return clean_text(row[idx]) if idx is not None and idx < len(row) else ""

            artist = get("artist") or fallback_artist
            price_text = get("price")
            amount, currency = parse_price(price_text)
            if not artist or amount is None:
                continue
            artist_id = upsert_artist(conn, artist)
            raw = {header[i]: row[i] if i < len(row) else "" for i in range(len(header)) if clean_text(header[i])}
            aw = {
                "artwork_title": get("artwork_title"),
                "medium": get("medium"),
                "dimensions": get("dimensions"),
                "year": get("year"),
                "price_amount": amount,
                "currency": currency,
                "status": get("status"),
                "raw_row_json": json.dumps(raw, ensure_ascii=False),
            }
            _insert_observation(conn, artist_id, exhibition_id, source_file_id, aw)
            inserted += 1
    conn.commit()
    return inserted


def import_metadata_file(conn, local_path, source_drive_path=""):
    local = Path(local_path)
    sheets = read_csv(local) if local.suffix.lower() == ".csv" else read_xlsx(local)
    inserted = 0
    for sheet_name, rows in sheets.items():
        if not rows:
            continue
        header_idx = 0
        header = rows[header_idx]
        keys = [normalize_key(h) for h in header]
        if not any("trien lam" in key or "exhibition" in key for key in keys):
            continue
        for row in rows[header_idx + 1 :]:
            values = {keys[i]: clean_text(row[i]) if i < len(row) else "" for i in range(len(keys))}
            title = values.get("ten trien lam", "") or values.get("exhibition", "") or values.get("title", "")
            if not title:
                continue
            artist = values.get("hoa si", "") or values.get("artist", "")
            start = values.get("ngay bat dau", "") or values.get("start date", "")
            venue = values.get("noi dien ra", "")
            drive_path = find_matching_exhibition_drive_path(conn, title, artist, venue, start)
            item = {
                "drive_path": drive_path or f"metadata://{source_drive_path or local.name}/{sheet_name}/{inserted}",
                "source_bucket": "metadata",
                "code": title,
                "event_type": "EXH",
                "start_date": start,
                "city": values.get("tinh thanh", ""),
                "title": title,
                "artists_text": artist,
                "organizer": values.get("don vi to chuc", ""),
                "venue": values.get("noi dien ra", ""),
                "online_status": values.get("giam tuyen", ""),
                "artwork_count": int(float(values["so luong tac pham"])) if values.get("so luong tac pham", "").replace(".", "", 1).isdigit() else None,
                "metadata": values,
            }
            upsert_exhibition(conn, item)
            inserted += 1
    conn.execute(
        "insert into imports(source, detail, status, count, created_at) values (?, ?, ?, ?, ?)",
        ("metadata", source_drive_path or str(local_path), "ok", inserted, now_iso()),
    )
    conn.commit()
    refresh_artist_stats(conn)
    return inserted


def find_matching_exhibition_drive_path(conn, title, artist, venue="", start_date=""):
    """Find the canonical Drive-folder exhibition that this metadata row
    duplicates, so upsert_exhibition can UPDATE the canonical row instead
    of INSERTing a fresh "metadata://..." one.

    Match heuristics (any one wins):
      1. Same venue + same start_date — strongest signal. Two real
         exhibitions almost never open at the same venue on the same day,
         and the master Excel often paraphrases the title which made the
         old title-fuzzy match miss.
      2. Title fuzzy + artist overlap (legacy: ≥4 score).
    """
    title_key = normalize_key(title)
    venue_key = normalize_key(venue)
    artist_names = split_artists(artist)
    artist_keys = [normalize_key(name) for name in artist_names]

    # Pass 1: venue + start_date exact match — catches the case the master
    # spreadsheet paraphrased the title.
    if venue_key and start_date:
        for row in conn.execute(
            "select drive_path, venue, start_date from exhibitions "
            "where drive_path not like 'metadata://%' and start_date = ?",
            (start_date,),
        ):
            if normalize_key(row["venue"] or "") == venue_key:
                return row["drive_path"]

    # Pass 2: legacy title-fuzzy + artist scoring.
    best = None
    best_score = 0
    for row in conn.execute("select drive_path, title, artists_text from exhibitions where drive_path not like 'metadata://%'"):
        score = 0
        row_title = normalize_key(row["title"])
        row_artists = normalize_key(row["artists_text"])
        if title_key and (title_key == row_title or title_key in row_title or row_title in title_key):
            score += 4
        if artist_keys and any(key and key in row_artists for key in artist_keys):
            score += 3
        if score > best_score:
            best = row["drive_path"]
            best_score = score
    return best if best_score >= 4 else ""


def refresh_artist_stats(conn):
    register_functions(conn)
    conn.execute(
        """
        update artists set
            exhibition_count = (
                select count(distinct e.id)
                from exhibitions e
                where normalize_like(e.artists_text, artists.normalized_name) = 1
            ),
            price_count = (select count(*) from price_observations p where p.artist_id = artists.id and p.price_amount > 10),
            min_price = (select min(price_amount) from price_observations p where p.artist_id = artists.id and p.price_amount > 10),
            max_price = (select max(price_amount) from price_observations p where p.artist_id = artists.id and p.price_amount > 10),
            avg_price = (select avg(price_amount) from price_observations p where p.artist_id = artists.id and p.price_amount > 10),
            avg_price_per_m2 = (select avg(price_per_m2) from price_observations p where p.artist_id = artists.id and p.price_per_m2 > 0 and p.price_per_m2 < 1000000),
            auction_count = (select count(*) from sale_results s where s.artist_id = artists.id),
            updated_at = ?
        """,
        (now_iso(),),
    )
    # Compute median price_per_m2 and overall stats per artist
    artist_ids = [r[0] for r in conn.execute(
        "select id from artists where price_count > 0 or auction_count > 0"
    ).fetchall()]
    for aid in artist_ids:
        # Gallery $/m² (from price_observations which is already USD)
        gallery_ppm = [r[0] for r in conn.execute(
            "select price_per_m2 from price_observations where artist_id=? and price_per_m2>0 and price_per_m2<1000000",
            (aid,)
        ).fetchall()]
        if gallery_ppm:
            gallery_ppm.sort()
            mid = len(gallery_ppm) // 2
            median_gallery = gallery_ppm[mid] if len(gallery_ppm) % 2 else (gallery_ppm[mid-1] + gallery_ppm[mid]) / 2
            conn.execute("update artists set median_price_per_m2=? where id=?", (round(median_gallery, 2), aid))
        # Overall stats merging gallery prices (USD) + auction prices (USD).
        # Filter: price > $10 to exclude typos/OCR errors, currency USD or empty (empty = already USD after to_usd conversion)
        prices = [r[0] for r in conn.execute(
            "select price_amount from price_observations where artist_id=? and price_amount > 10 and (currency='USD' or currency='')", (aid,)
        ).fetchall()]
        # Use premium-inclusive price as the "real" price the buyer paid
        prices += [r[0] for r in conn.execute(
            "select coalesce(price_with_premium_usd, price_usd) from sale_results where artist_id=? and coalesce(price_with_premium_usd, price_usd) > 10", (aid,)
        ).fetchall()]
        if prices:
            conn.execute(
                "update artists set overall_min_usd=?, overall_avg_usd=?, overall_max_usd=? where id=?",
                (round(min(prices), 2), round(sum(prices)/len(prices), 2), round(max(prices), 2), aid),
            )
        # Overall median $/m² combining gallery + auction
        ppm_all = list(gallery_ppm)
        ppm_all += [r[0] for r in conn.execute(
            "select price_per_m2_usd from sale_results where artist_id=? and price_per_m2_usd > 0 and price_per_m2_usd < 1000000", (aid,)
        ).fetchall()]
        if ppm_all:
            ppm_all.sort()
            mid = len(ppm_all) // 2
            median_all = ppm_all[mid] if len(ppm_all) % 2 else (ppm_all[mid-1] + ppm_all[mid]) / 2
            conn.execute("update artists set overall_median_per_m2_usd=? where id=?", (round(median_all, 2), aid))
    conn.commit()


def register_functions(conn):
    conn.create_function("normalize_like", 2, lambda text, needle: 1 if needle and needle in normalize_key(text or "") else 0)


def api_payload(conn):
    register_functions(conn)
    refresh_artist_stats(conn)
    # Per-artist kind counts — JS uses these to pick role tags (Họa sĩ / Nhà điêu khắc /
    # Họa sĩ đồ họa / etc.). installation/performance/video are reserved for future
    # contemporary artists (Tuấn Andrew Nguyễn, Lê Brothers, Lê Quý Anh Hào…).
    artists = [
        dict(row)
        for row in conn.execute(
            """
            select a.id, a.name, coalesce(a.display_name, a.name) as display_name,
                   a.birth_year, a.death_year,
                   a.exhibition_count, a.price_count, a.min_price, a.max_price, a.avg_price,
                   a.avg_price_per_m2, a.median_price_per_m2,
                   a.auction_count, a.overall_min_usd, a.overall_avg_usd, a.overall_max_usd,
                   a.overall_median_per_m2_usd,
                   (coalesce(a.price_count,0) + coalesce(a.auction_count,0)) as total_count,
                   (select count(*) from sale_results s
                     where s.artist_id=a.id and s.kind='painting') as painting_count,
                   (select count(*) from sale_results s
                     where s.artist_id=a.id and s.kind='sculpture') as sculpture_count,
                   (select count(*) from sale_results s
                     where s.artist_id=a.id and s.kind='print') as print_count,
                   (select count(*) from sale_results s
                     where s.artist_id=a.id and s.kind='installation') as installation_count,
                   (select count(*) from sale_results s
                     where s.artist_id=a.id and s.kind='performance') as performance_count,
                   (select count(*) from sale_results s
                     where s.artist_id=a.id and s.kind='video') as video_count
            from artists a
            where a.price_count > 0 or a.auction_count > 0
            order by
                case when a.birth_year is null then 2 when a.birth_year < 0 then 1 else 0 end,
                a.birth_year,
                a.name
            """
        )
    ]
    exhibitions = []
    for row in conn.execute(
        """
        select e.*,
            (select count(*) from source_files sf where sf.exhibition_id=e.id) as file_count,
            (select count(*) from source_files sf where sf.exhibition_id=e.id and sf.has_price_hint=1) as price_file_count,
            (select count(*) from source_files sf where sf.exhibition_id=e.id and sf.has_catalogue_hint=1) as catalogue_file_count
        from exhibitions e
        order by coalesce(start_date, '') desc, title
        limit 500
        """
    ):
        d = dict(row)
        # Parse venue_segments if present
        if d.get("venue_segments_json"):
            try:
                d["venue_segments"] = json.loads(d["venue_segments_json"])
            except Exception:
                d["venue_segments"] = []
        exhibitions.append(d)
    files = [dict(row) for row in conn.execute("select * from source_files order by imported_at desc limit 300")]
    observations = [
        dict(row)
        for row in conn.execute(
            """
            select p.*, a.name as artist_name, e.title as exhibition_title
            from price_observations p
            left join artists a on a.id=p.artist_id
            left join exhibitions e on e.id=p.exhibition_id
            order by p.observed_at desc
            limit 500
            """
        )
    ]
    imports = [dict(row) for row in conn.execute("select * from imports order by created_at desc limit 20")]
    sales = [
        dict(row)
        for row in conn.execute(
            """
            select s.*, coalesce(a.display_name, a.name, s.artist_name_raw) as artist_name
            from sale_results s
            left join artists a on a.id=s.artist_id
            order by s.sale_date desc, s.price_usd desc
            limit 5000
            """
        )
    ]

    # Galleries summary — aggregate from venue + venue_segments_json
    # Build map: gallery_name → list of (exhibition_id, start_date)
    gallery_map = {}
    all_exhs = conn.execute("""
        select id, title, start_date, artists_text, venue, organizer, venue_segments_json
        from exhibitions
        where drive_path not like 'metadata://%'
    """).fetchall()
    for e in all_exhs:
        eid, title, sdate, exh_artists_text, venue, organizer, segments_json = e
        # Determine all venues for this exhibition
        venues = []
        if segments_json:
            try:
                for seg in json.loads(segments_json):
                    venues.append((seg.get("venue", ""), seg.get("start_date", sdate)))
            except Exception:
                pass
        if not venues:
            name = venue or organizer or "Không rõ"
            venues.append((name, sdate))
        for vname, vdate in venues:
            vname = vname or "Không rõ"
            if vname not in gallery_map:
                gallery_map[vname] = []
            gallery_map[vname].append({"id": eid, "title": title, "start_date": vdate, "artists_text": exh_artists_text})

    galleries = []
    for gname, exhs in gallery_map.items():
        # Artists across all exhs of this gallery
        exh_ids = [e["id"] for e in exhs]
        if exh_ids:
            ph = ",".join("?" * len(exh_ids))
            artist_count = conn.execute(
                f"select count(distinct artist_id) from exhibition_artists where exhibition_id in ({ph})",
                exh_ids,
            ).fetchone()[0]
            # Enrich with artists_display
            for e in exhs:
                ad = conn.execute(
                    """select group_concat(coalesce(a.display_name, a.name), ', ')
                       from exhibition_artists ea join artists a on a.id = ea.artist_id
                       where ea.exhibition_id = ?""",
                    (e["id"],),
                ).fetchone()
                e["artists_display"] = ad[0] if ad and ad[0] else ""
        else:
            artist_count = 0

        dates = [e.get("start_date") for e in exhs if e.get("start_date")]
        exhs.sort(key=lambda x: x.get("start_date") or "", reverse=True)
        galleries.append({
            "gallery": gname,
            "exhibition_count": len(exhs),
            "artist_count": artist_count,
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
            "exhibitions": exhs,
        })
    galleries.sort(key=lambda g: (-g["exhibition_count"], g.get("last_date") or ""), reverse=False)
    galleries.sort(key=lambda g: g["exhibition_count"], reverse=True)

    # Auction houses summary (grouped by source + reference data)
    try:
        import sys as _sys
        _sys.path.insert(0, str(APP_ROOT / "data"))
        from auction_houses import AUCTION_HOUSES
    except ImportError:
        AUCTION_HOUSES = {}

    house_rows = conn.execute(
        """
        select source,
               count(*) as lot_count,
               count(distinct artist_id) as artist_count,
               count(distinct sale_date) as sale_days,
               count(distinct sale_page_url) as session_count,
               round(min(price_usd), 0) as min_usd,
               round(avg(price_usd), 0) as avg_usd,
               round(max(price_usd), 0) as max_usd,
               round(avg(case when hammer_price > 0 and price_with_premium > 0
                              then price_with_premium / hammer_price - 1.0
                              else null end) * 100, 1) as avg_premium_pct
        from sale_results
        group by source
        """
    ).fetchall()
    auction_houses = []
    for r in house_rows:
        d = dict(r)
        ref = AUCTION_HOUSES.get(d["source"], {})
        d.update({
            "display_name": ref.get("name", d["source"].title()),
            "country": ref.get("country", ""),
            "kind": ref.get("kind", "house"),    # house | platform
            "founded": ref.get("founded"),
            "premium_rate_pct": ref.get("premium_rate_pct"),
            "premium_note": ref.get("premium_note", ""),
            "vat_pct": ref.get("vat_pct"),
            "tax_note": ref.get("tax_note", ""),
            "website": ref.get("website", ""),
            "vietnamese_art_dept": ref.get("vietnamese_art_dept", ""),
        })
        auction_houses.append(d)
    summary = {
        "artist_count": len(artists),
        "exhibition_count": conn.execute("select count(*) c from exhibitions").fetchone()["c"],
        "source_file_count": conn.execute("select count(*) c from source_files").fetchone()["c"],
        "price_observation_count": conn.execute("select count(*) c from price_observations").fetchone()["c"],
        "price_file_count": conn.execute("select count(*) c from source_files where has_price_hint=1").fetchone()["c"],
        "catalogue_file_count": conn.execute("select count(*) c from source_files where has_catalogue_hint=1").fetchone()["c"],
        "auction_sale_count": conn.execute("select count(*) c from sale_results").fetchone()["c"],
    }
    report = build_report(conn)
    return {"summary": summary, "artists": artists, "exhibitions": exhibitions, "files": files, "observations": observations, "imports": imports, "sales": sales, "auction_houses": auction_houses, "galleries": galleries, "report": report}


def build_report(conn):
    """HENI-style aggregate report. Returns dict of card → list-of-rows."""
    today = now_iso()[:10]
    two_years_ago = f"{int(today[:4]) - 2}{today[4:]}"
    one_year_ago = f"{int(today[:4]) - 1}{today[4:]}"
    ninety_days_ago = now_iso()[:10]  # simple: last-90d via python
    from datetime import datetime, timedelta
    ninety_days_ago = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    twenty_eight_days_ago = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")

    def rows(sql, *params):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # 1) Họa sĩ doanh số cao nhất 24 tháng — total premium-inclusive USD revenue
    highest_sellers = rows(
        """select a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  count(*) as lot_count,
                  sum(coalesce(s.price_with_premium_usd, s.price_usd)) as total_usd,
                  avg(coalesce(s.price_with_premium_usd, s.price_usd)) as avg_usd
             from sale_results s join artists a on a.id = s.artist_id
            where s.price_usd is not null and s.sale_date >= ?
         group by a.id order by total_usd desc limit 100""", two_years_ago)

    # 1b) Họa sĩ với số lượng tác phẩm bán nhiều nhất (24 tháng) — by lot count
    most_lots_24m = rows(
        """select a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  count(*) as lot_count,
                  sum(coalesce(s.price_with_premium_usd, s.price_usd)) as total_usd,
                  avg(coalesce(s.price_with_premium_usd, s.price_usd)) as avg_usd
             from sale_results s join artists a on a.id = s.artist_id
            where s.price_usd is not null and s.sale_date >= ?
         group by a.id order by lot_count desc, total_usd desc limit 100""", two_years_ago)

    # 1c) Họa sĩ doanh số TRUNG BÌNH cao nhất — value-per-lot ranking (24m, ≥3 lots to be meaningful)
    highest_avg_24m = rows(
        """select a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  count(*) as lot_count,
                  sum(coalesce(s.price_with_premium_usd, s.price_usd)) as total_usd,
                  avg(coalesce(s.price_with_premium_usd, s.price_usd)) as avg_usd,
                  max(coalesce(s.price_with_premium_usd, s.price_usd)) as max_usd
             from sale_results s join artists a on a.id = s.artist_id
            where s.price_usd is not null and s.sale_date >= ?
         group by a.id having count(*) >= 3
         order by avg_usd desc limit 100""", two_years_ago)

    # 2) Kỷ lục giá mới — lots that equal each artist's all-time max, within last 12 months.
    # Dedupe to one lot per artist (most recent, highest-priced).
    record_prices = rows(
        """with maxp as (
             select artist_id, max(price_usd) as maxv from sale_results where price_usd is not null group by artist_id
           ), ranked as (
             select s.*,
                    row_number() over (partition by s.artist_id order by s.sale_date desc, s.price_usd desc) as rn
               from sale_results s join maxp m on m.artist_id = s.artist_id
              where s.price_usd is not null and s.price_usd = m.maxv and s.sale_date >= ?
           )
           select a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  r.artwork_title, r.sale_date, r.price_usd, r.source, r.source_url
             from ranked r join artists a on a.id = r.artist_id
            where r.rn = 1
         order by r.price_usd desc, r.sale_date desc limit 100""", one_year_ago)

    # 3) Top lot bán cao nhất 12 tháng (dedupe duplicate records — same artist+title+price)
    top_lots = rows(
        """with ranked as (
             select s.*,
                    row_number() over (
                      partition by s.artist_id, lower(trim(coalesce(s.artwork_title,''))), round(s.price_usd)
                      order by s.id
                    ) as rn
               from sale_results s
              where s.price_usd is not null and s.status = 'sold' and s.sale_date >= ?
           )
           select r.id, a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  r.artwork_title, r.sale_date, r.price_usd, r.source, r.source_url, r.auction_title
             from ranked r join artists a on a.id = r.artist_id
            where r.rn = 1
         order by r.price_usd desc limit 100""", one_year_ago)

    # 4) Phiên đấu giá đáng chú ý — group by sale_date + auction_title
    top_sessions = rows(
        """select s.source, s.sale_date, s.auction_title, s.sale_location,
                  count(*) as lot_count, sum(s.price_usd) as total_usd,
                  max(s.sale_page_url) as sale_page_url
             from sale_results s
            where s.price_usd is not null and s.status='sold' and s.sale_date >= ?
         group by s.sale_date, s.auction_title
         order by total_usd desc limit 100""", one_year_ago)

    # 5) Doanh số theo địa điểm 12 tháng — normalize fragmented city strings across crawlers
    by_location = rows(
        """with normalized as (
             select case
                      when lower(sale_location) like 'hong kong%' then 'Hong Kong'
                      when lower(sale_location) like '%new york%' or lower(sale_location) = 'united states' then 'New York'
                      when lower(sale_location) like '%paris%' or lower(sale_location) = 'france' then 'Paris'
                      when lower(sale_location) like '%london%' or lower(sale_location) = 'united kingdom' then 'London'
                      when lower(sale_location) like '%singapore%' then 'Singapore'
                      when sale_location = '' or sale_location is null then 'Không rõ'
                      else sale_location
                    end as location,
                    price_usd
               from sale_results
              where price_usd is not null and sale_date >= ?
           )
           select location, count(*) as lot_count, sum(price_usd) as total_usd, avg(price_usd) as avg_usd
             from normalized group by location order by total_usd desc limit 100""", one_year_ago)

    # 6) Lot sắp đấu giá — future sale_date
    upcoming_lots = rows(
        """select s.id, a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  s.artwork_title, s.sale_date, s.estimate_low, s.estimate_high, s.currency,
                  s.source, s.source_url, s.auction_title
             from sale_results s join artists a on a.id = s.artist_id
            where s.sale_date > ? and s.estimate_low is not null
         order by s.estimate_low desc limit 100""", today)

    # 7) Phiên đấu giá sắp tới — combine:
    #    (a) sessions with lots already in DB (future sale_date, estimates available)
    #    (b) catalogs on auction-house calendars that don't have lots yet
    upcoming_sessions_a = rows(
        """select s.source, s.sale_date, s.auction_title, s.sale_location,
                  count(*) as lot_count, sum(s.estimate_low) as est_low_total,
                  sum(s.estimate_high) as est_high_total, max(s.sale_page_url) as sale_page_url
             from sale_results s
            where s.sale_date > ? and s.estimate_low is not null
         group by s.sale_date, s.auction_title""", today)
    upcoming_sessions_b = rows(
        """select source, sale_date, auction_title, sale_location,
                  coalesce(expected_lots, 0) as lot_count,
                  null as est_low_total, null as est_high_total, sale_page_url
             from upcoming_auctions
            where sale_date > ?""", today)
    # Dedupe: if a future session already exists in sale_results-aggregate, skip calendar entry
    seen_titles = {(r["sale_date"], r["auction_title"]) for r in upcoming_sessions_a}
    upcoming_sessions = upcoming_sessions_a + [
        r for r in upcoming_sessions_b if (r["sale_date"], r["auction_title"]) not in seen_titles
    ]
    upcoming_sessions.sort(key=lambda r: (r.get("est_low_total") or 0, r["sale_date"]), reverse=True)
    upcoming_sessions = upcoming_sessions[:8]

    # 8) Đang hoạt động mạnh (last 90 days) — most lots
    most_active = rows(
        """select a.id as artist_id, coalesce(a.display_name, a.name) as artist_name,
                  count(*) as recent_count, sum(s.price_usd) as recent_usd
             from sale_results s join artists a on a.id = s.artist_id
            where s.sale_date >= ?
         group by a.id order by recent_count desc, recent_usd desc limit 100""", ninety_days_ago)

    # 9b) Per-issue lot lists for investigation (replaces aggregate data_gaps card).
    lots_missing_date = rows(
        """select s.id, s.source, s.source_url, s.artwork_title,
                  s.artist_name_raw,
                  coalesce(a.display_name, a.name) as artist_name,
                  a.id as artist_id,
                  coalesce(s.price_with_premium_usd, s.price_usd) as price_usd
             from sale_results s left join artists a on a.id = s.artist_id
            where s.sale_date is null or s.sale_date = ''
         order by coalesce(s.price_with_premium_usd, s.price_usd, 0) desc limit 200""")

    # Sculptures are inherently 3D — they don't have a 2D footprint and aren't "missing" dim
    # (they use H. only). Excluded from this list so the queue stays actionable.
    lots_missing_dim = rows(
        """select s.id, s.source, s.source_url, s.artwork_title,
                  s.sale_date,
                  s.artist_name_raw,
                  coalesce(a.display_name, a.name) as artist_name,
                  a.id as artist_id,
                  coalesce(s.price_with_premium_usd, s.price_usd) as price_usd
             from sale_results s left join artists a on a.id = s.artist_id
            where (s.dimensions is null or s.dimensions = '')
              and s.kind != 'sculpture'
         order by coalesce(s.price_with_premium_usd, s.price_usd, 0) desc limit 200""")

    # Per-artist coverage gaps — exclude workshops/studios (they don't have birth years).
    artist_gaps = rows(
        """select id as artist_id,
                  coalesce(display_name, name) as artist_name,
                  birth_year, death_year,
                  auction_count
             from artists
            where (auction_count > 0 or price_count > 0)
              and (birth_year is null and death_year is null)
              and lower(coalesce(display_name, name)) not like '%xưởng%'
              and lower(coalesce(display_name, name)) not like '%atelier%'
              and lower(coalesce(display_name, name)) not like '%studio%'
              and lower(name) not like '%atelier%'
              and lower(name) not like '%studio%'
         order by auction_count desc limit 100""")

    # List of empty-title lots so user can manually verify
    empty_titles = rows(
        """select s.id, s.source, s.sale_date, s.artist_name_raw,
                  coalesce(a.display_name, a.name) as artist_name,
                  s.source_url
             from sale_results s left join artists a on a.id = s.artist_id
            where s.artwork_title is null or s.artwork_title = ''
         order by s.sale_date desc limit 200""")

    # By-kind breakdown — paintings vs sculptures vs lacquer vs works on paper
    by_kind = rows(
        """select kind, count(*) as lot_count,
                  count(distinct artist_id) as artist_count,
                  sum(coalesce(price_with_premium_usd, price_usd, 0)) as total_usd
             from sale_results
         group by kind order by total_usd desc""")

    # Top sculpture lots (12m + all-time) for the dedicated sculpture card
    top_sculptures = rows(
        """select s.id, s.source, s.source_url, s.sale_date,
                  s.artwork_title, s.height_cm, s.medium,
                  coalesce(a.display_name, a.name, s.artist_name_raw) as artist_name,
                  a.id as artist_id,
                  coalesce(s.price_with_premium_usd, s.price_usd) as price_usd
             from sale_results s left join artists a on a.id = s.artist_id
            where s.kind='sculpture'
              and coalesce(s.price_with_premium_usd, s.price_usd) is not null
         order by coalesce(s.price_with_premium_usd, s.price_usd) desc limit 100""")

    # 9w) Outlier detection — flag lots whose $/m² deviates strongly from the
    # artist's own median. Useful both ways:
    #   - Bargain finds: lot $/m² < 50% of artist's median (sold cheap)
    #   - Premium prices: lot $/m² > 200% of artist's median (sold dear)
    # Filters: artist must have ≥5 sold lots so median is meaningful;
    # lot must be a painting/print (sculpture has no $/m²); price ≥ $5K to
    # exclude very small/decorative pieces that dominate the bargain end.
    bargain_lots = rows(
        """select s.id, s.source, s.source_url, s.sale_date, s.artwork_title,
                  s.dimensions, s.medium, s.kind,
                  coalesce(a.display_name, a.name) as artist_name, a.id as artist_id,
                  coalesce(s.price_with_premium_usd, s.price_usd) as price_usd,
                  s.price_per_m2_usd,
                  a.overall_median_per_m2_usd as median_ppm,
                  round(100.0 * (s.price_per_m2_usd - a.overall_median_per_m2_usd)
                                / a.overall_median_per_m2_usd, 0) as dev_pct
             from sale_results s join artists a on a.id = s.artist_id
            where s.kind in ('painting','print')
              and s.price_per_m2_usd > 0
              and a.overall_median_per_m2_usd > 0
              and a.auction_count >= 5
              and coalesce(s.price_with_premium_usd, s.price_usd) >= 5000
              and (s.price_per_m2_usd / a.overall_median_per_m2_usd) <= 0.5
         order by (s.price_per_m2_usd / a.overall_median_per_m2_usd) asc
         limit 100""")

    premium_lots = rows(
        """select s.id, s.source, s.source_url, s.sale_date, s.artwork_title,
                  s.dimensions, s.medium, s.kind,
                  coalesce(a.display_name, a.name) as artist_name, a.id as artist_id,
                  coalesce(s.price_with_premium_usd, s.price_usd) as price_usd,
                  s.price_per_m2_usd,
                  a.overall_median_per_m2_usd as median_ppm,
                  round(100.0 * (s.price_per_m2_usd - a.overall_median_per_m2_usd)
                                / a.overall_median_per_m2_usd, 0) as dev_pct
             from sale_results s join artists a on a.id = s.artist_id
            where s.kind in ('painting','print')
              and s.price_per_m2_usd > 0
              and a.overall_median_per_m2_usd > 0
              and a.auction_count >= 5
              and (s.price_per_m2_usd / a.overall_median_per_m2_usd) >= 2.0
         order by (s.price_per_m2_usd / a.overall_median_per_m2_usd) desc
         limit 100""")

    # 9w) Suspicious / potentially-forged lots. Flag if either:
    #  - $/m² < 30% of artist's median FOR THE SAME SUPPORT (canvas/silk/paper/lacquer/panel)
    #    → fair apples-to-apples comparison (e.g. Le Pho silk vs Le Pho silk median, not vs overall)
    #  - Title/medium/provenance contains attribution caveats (attribué à, école de, …)
    # Filter sale_date >= 2018-01-01 because Indochine market boom started ~2017-18 —
    # comparing 2010 sales against 2025 medians yields false positives.
    suspicious_lots = rows(
        """with support_med as (
             select s.artist_id, s.support_type,
                    cast(avg(case when rn = (cnt+1)/2 or rn = cnt/2+1 then price_per_m2_usd end) as real) as med
             from (
               select artist_id, support_type, price_per_m2_usd,
                      row_number() over (partition by artist_id, support_type order by price_per_m2_usd) as rn,
                      count(*) over (partition by artist_id, support_type) as cnt
               from sale_results
               where kind='painting' and price_per_m2_usd > 0 and price_per_m2_usd < 50000000
                 and support_type is not null and artist_id is not null
             ) s
             where s.cnt >= 3
             group by s.artist_id, s.support_type, s.cnt
           )
           select s.id, s.source, s.source_url, s.sale_date, s.artwork_title,
                  s.dimensions, s.medium, s.provenance, s.kind, s.support_type,
                  s.estimate_low, s.estimate_high, s.currency,
                  coalesce(a.display_name, a.name) as artist_name, a.id as artist_id,
                  coalesce(s.price_with_premium_usd, s.price_usd) as price_usd,
                  s.price_per_m2_usd,
                  coalesce(sm.med, a.overall_median_per_m2_usd) as median_ppm,
                  round(100.0 * s.price_per_m2_usd / coalesce(sm.med, a.overall_median_per_m2_usd), 0) as pct_of_median,
                  case when sm.med is not null then s.support_type else 'overall' end as median_basis,
                  case
                    when lower(coalesce(s.artwork_title,'') || ' ' || coalesce(s.medium,'')) glob '*attribu*' then 'attribué/attributed'
                    when lower(coalesce(s.artwork_title,'') || ' ' || coalesce(s.medium,'')) glob '*école de *' then 'école de'
                    when lower(coalesce(s.artwork_title,'') || ' ' || coalesce(s.medium,'')) glob '*manière de*' then 'manière de'
                    when lower(coalesce(s.artwork_title,'') || ' ' || coalesce(s.medium,'')) glob '*studio of*' then 'studio of'
                    when lower(coalesce(s.artwork_title,'') || ' ' || coalesce(s.medium,'')) glob '*manner of*' then 'manner of'
                    when lower(coalesce(s.artwork_title,'') || ' ' || coalesce(s.medium,'')) glob '*circle of*' then 'circle of'
                    else 'price_anomaly'
                  end as flag_reason
             from sale_results s
             join artists a on a.id = s.artist_id
             left join support_med sm on sm.artist_id = s.artist_id and sm.support_type = s.support_type
            where s.kind = 'painting' and s.status = 'sold'
              and s.price_per_m2_usd > 0 and s.area_m2 >= 0.05
              and a.overall_median_per_m2_usd > 0 and a.auction_count >= 5
              and coalesce(s.price_with_premium_usd, s.price_usd) >= 1000
              and s.sale_date >= '2018-01-01'
              and a.display_name in (
                'Lê Phổ','Mai Trung Thứ','Vũ Cao Đàm','Bùi Xuân Phái','Nguyễn Gia Trí',
                'Nguyễn Phan Chánh','Tô Ngọc Vân','Lê Thị Lựu','Phạm Hậu',
                'Joseph Inguimberty','Alix Aymé','Lương Xuân Nhị','Phạm Văn Đôn',
                'Hoàng Tích Chù','Trần Văn Cẩn','Nguyễn Sang','Nguyễn Tư Nghiêm','Lê Bá Đảng'
              )
              and (
                (s.price_per_m2_usd / coalesce(sm.med, a.overall_median_per_m2_usd)) <= 0.30
                or s.artwork_title like '%attribu%'
                or s.artwork_title like '%école de %'
                or s.artwork_title like '%manière de%'
                or s.artwork_title like '%studio of%'
                or s.artwork_title like '%manner of%'
                or s.artwork_title like '%circle of%'
              )
         order by (s.price_per_m2_usd / coalesce(sm.med, a.overall_median_per_m2_usd)) asc
         limit 100""")

    # Median $/m² per (artist, year) — for top 6 Indochine masters.
    # Lets users see historical price progression (Lê Phổ 2010 ≠ Lê Phổ 2024).
    yearly_median_per_artist = rows(
        """with top_artists as (
             select id, coalesce(display_name, name) as name
             from artists
             where display_name in ('Lê Phổ','Mai Trung Thứ','Vũ Cao Đàm','Bùi Xuân Phái',
                                    'Nguyễn Gia Trí','Lê Thị Lựu')
           ),
           yr as (
             select s.artist_id, t.name as artist_name,
                    cast(substr(s.sale_date, 1, 4) as integer) as yr,
                    s.price_per_m2_usd
             from sale_results s join top_artists t on t.id = s.artist_id
             where s.kind='painting' and s.price_per_m2_usd > 0
               and s.price_per_m2_usd < 50000000
               and s.sale_date >= '2010-01-01'
           ),
           ranked as (
             select artist_id, artist_name, yr, price_per_m2_usd,
                    row_number() over (partition by artist_id, yr order by price_per_m2_usd) as rn,
                    count(*) over (partition by artist_id, yr) as cnt
             from yr
           )
           select artist_id, artist_name, yr, cnt as n,
                  round(avg(case when rn = (cnt+1)/2 or rn = cnt/2+1 then price_per_m2_usd end)) median_ppm
           from ranked
           group by artist_id, artist_name, yr, cnt
           having cnt >= 2
           order by artist_name, yr""")

    # 9z) Momentum — estimate-vs-hammer ratio per artist. Hammer significantly above
    # the midpoint estimate signals heating market; below = cooling. Need ≥3 lots
    # with full estimate data for the stat to mean anything.
    momentum_artists = rows(
        """with eligible as (
             select s.artist_id,
                    s.hammer_price,
                    (s.estimate_low + s.estimate_high) / 2.0 as est_mid,
                    s.estimate_high
               from sale_results s
              where s.status='sold'
                and s.estimate_low is not null and s.estimate_low > 0
                and s.estimate_high is not null and s.estimate_high > 0
                and s.hammer_price is not null and s.hammer_price > 0
                and s.artist_id is not null
           )
           select a.id as artist_id,
                  coalesce(a.display_name, a.name) as artist_name,
                  count(*) as n,
                  round(avg((e.hammer_price - e.est_mid) / e.est_mid * 100.0), 1) as avg_overshoot_pct,
                  round(100.0 * sum(case when e.hammer_price > e.estimate_high then 1 else 0 end) / count(*), 1) as pct_over_high,
                  round(100.0 * sum(case when e.hammer_price < e.est_mid then 1 else 0 end) / count(*), 1) as pct_under_mid
             from eligible e join artists a on a.id = e.artist_id
         group by a.id
           having count(*) >= 3
         order by avg_overshoot_pct desc limit 100""")

    # 9y) Per-medium $/m² benchmark for paintings + prints. Computes median in
    # SQLite via row-numbered self-join (no percentile_cont). Median is more
    # representative than mean because top masterpieces skew the average heavily
    # (1 Lê Phổ ink-on-silk at $9M/m² pulls the ink average way up).
    medium_benchmark = rows(
        """with t as (
             select case
                 when lower(medium) like '%laque%' or lower(medium) like '%lacquer%' or lower(medium) like '%sơn mài%' then 'Sơn mài'
                 when lower(medium) like '%huile%' or lower(medium) like '%oil%' then 'Sơn dầu'
                 when lower(medium) like '%aquarelle%' or lower(medium) like '%watercolour%' or lower(medium) like '%watercolor%' then 'Aquarelle'
                 when lower(medium) like '%encre%' or lower(medium) like '%ink%' then 'Mực'
                 when lower(medium) like '%gouache%' then 'Gouache'
                 when lower(medium) like '%pastel%' then 'Pastel'
                 when lower(medium) like '%soie%' or lower(medium) like '%silk%' then 'Lụa'
                 when lower(medium) like '%lithograph%' or lower(medium) like '%estampe%' or lower(medium) like '%gravure%' then 'Đồ họa'
                 else 'Khác'
               end as paint_medium,
               price_per_m2_usd, area_m2
             from sale_results
            where price_per_m2_usd > 0 and price_per_m2_usd < 50000000
              and kind in ('painting','print')
           ),
           ranked as (
             select paint_medium, price_per_m2_usd, area_m2,
                    row_number() over (partition by paint_medium order by price_per_m2_usd) as rn,
                    count(*) over (partition by paint_medium) as cnt
               from t
           ),
           medians as (
             select paint_medium,
                    avg(price_per_m2_usd) as median_ppm
               from ranked
              where rn in ((cnt+1)/2, (cnt+2)/2)
           group by paint_medium
           ),
           agg as (
             select paint_medium, count(*) as n,
                    avg(price_per_m2_usd) as avg_ppm,
                    min(price_per_m2_usd) as min_ppm,
                    max(price_per_m2_usd) as max_ppm,
                    avg(area_m2) as avg_area_m2
               from t
           group by paint_medium
           )
           select agg.paint_medium,
                  agg.n,
                  round(agg.avg_ppm, 0) as avg_ppm,
                  round(m.median_ppm, 0) as median_ppm,
                  round(agg.min_ppm, 0) as min_ppm,
                  round(agg.max_ppm, 0) as max_ppm,
                  round(agg.avg_area_m2, 3) as avg_area_m2
             from agg join medians m using (paint_medium)
            where agg.n >= 10
         order by median_ppm desc""")

    # 9) Coverage per source — useful for "do we have enough data?"
    coverage = rows(
        """select source,
                  count(*) as lot_count,
                  count(distinct artist_id) as artist_count,
                  min(substr(sale_date, 1, 4)) as year_min,
                  max(substr(sale_date, 1, 4)) as year_max,
                  count(distinct substr(sale_date, 1, 4)) as years_covered,
                  count(distinct sale_page_url) as session_count
             from sale_results
            where sale_date != ''
         group by source order by lot_count desc""")

    return {
        "highest_sellers_24m": highest_sellers,
        "most_lots_24m": most_lots_24m,
        "highest_avg_24m": highest_avg_24m,
        "record_prices": record_prices,
        "top_lots_12m": top_lots,
        "top_sessions_12m": top_sessions,
        "by_location_12m": by_location,
        "upcoming_lots": upcoming_lots,
        "upcoming_sessions": upcoming_sessions,
        "most_active_90d": most_active,
        "coverage": coverage,
        "by_kind": by_kind,
        "top_sculptures": top_sculptures,
        "momentum_artists": momentum_artists,
        "medium_benchmark": medium_benchmark,
        "bargain_lots": bargain_lots,
        "premium_lots": premium_lots,
        "suspicious_lots": suspicious_lots,
        "yearly_median_per_artist": yearly_median_per_artist,
        "lots_missing_date": lots_missing_date,
        "lots_missing_dim": lots_missing_dim,
        "artist_gaps": artist_gaps,
        "empty_titles": empty_titles,
        "generated_at": now_iso(),
    }


HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Artonis — Artist Price Intelligence</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --ink:#1a1a1a;
      --muted:#6b6b6b;
      --line:#e5e0d5;
      --line-soft:#f0ece2;
      --paper:#faf8f4;
      --panel:#ffffff;
      --green:#1f4a36;
      --green-soft:#e0ebe4;
      --rose:#8a3d4b;
      --rose-soft:#f3e2e4;
      --gold:#8a6420;
      --gold-soft:#f3ead6;
      --blue:#2b5770;
      --blue-soft:#dbe6ec;
      --shadow: 0 1px 3px rgba(20,20,20,0.04), 0 1px 2px rgba(20,20,20,0.02);
      --shadow-lg: 0 4px 12px rgba(20,20,20,0.06), 0 2px 4px rgba(20,20,20,0.03);
    }
    * { box-sizing:border-box; }
    body {
      margin:0;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      color:var(--ink);
      background:var(--paper);
      font-size:14px;
      line-height:1.5;
      -webkit-font-smoothing:antialiased;
    }
    h1, h2, h3 { font-family:'Fraunces', Georgia, serif; font-weight:700; letter-spacing:-0.01em; }
    header {
      padding:28px 36px 22px;
      border-bottom:1px solid var(--line);
      background:linear-gradient(180deg, #f5f1e8 0%, #f3ede1 100%);
    }
    header .brand { display:flex; align-items:center; gap:14px; margin-bottom:4px; }
    header .logo {
      width:40px; height:40px;
      background:var(--green); color:#fff;
      border-radius:8px;
      display:flex; align-items:center; justify-content:center;
      font-family:'Fraunces', serif; font-size:22px; font-weight:700;
    }
    h1 { margin:0; font-size:28px; font-weight:700; }
    header p { margin:4px 0 0; color:var(--muted); max-width:1040px; font-size:14px; }
    main { padding:24px 36px 60px; max-width:1600px; margin:0 auto; }
    .stats { display:grid; grid-template-columns:repeat(7,1fr); gap:12px; margin-bottom:24px; }
    .stat {
      background:var(--panel);
      border:1px solid var(--line);
      border-radius:10px;
      padding:14px 16px;
      box-shadow:var(--shadow);
    }
    .stat b { display:block; font-family:'Fraunces', serif; font-size:24px; font-weight:700; margin-bottom:2px; color:var(--ink); }
    .stat span { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:0.03em; }
    .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:20px 0 14px; }
    input, select, button {
      height:40px;
      border:1px solid var(--line);
      border-radius:8px;
      background:#fff;
      padding:0 14px;
      font-size:14px;
      font-family:inherit;
      color:var(--ink);
      outline:none;
      transition:border-color 0.15s, box-shadow 0.15s;
    }
    input:focus, select:focus { border-color:var(--green); box-shadow:0 0 0 3px rgba(31,74,54,0.08); }
    input { min-width:320px; flex:1; max-width:500px; }
    button {
      cursor:pointer;
      background:var(--green); color:#fff;
      border-color:var(--green);
      font-weight:500;
      padding:0 18px;
    }
    button:hover { background:#164030; }
    .tabs { display:flex; gap:2px; margin:22px 0 16px; border-bottom:1px solid var(--line); }
    .tab {
      border:0; background:transparent;
      color:var(--muted);
      padding:11px 18px;
      height:auto;
      font-size:14px;
      font-weight:500;
      border-radius:0;
      border-bottom:2px solid transparent;
      position:relative;
      top:1px;
    }
    .tab:hover { color:var(--ink); background:transparent; }
    .tab.active { color:var(--green); border-bottom-color:var(--green); background:transparent; }
    section { display:none; }
    section.active { display:block; }

    /* Tables */
    .table-wrap { background:#fff; border:1px solid var(--line); border-radius:10px; overflow:hidden; box-shadow:var(--shadow); }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:12px 14px; border-bottom:1px solid var(--line-soft); text-align:left; vertical-align:top; font-size:13.5px; }
    th {
      background:#f7f3e9;
      font-size:11px;
      text-transform:uppercase;
      letter-spacing:0.04em;
      color:#5a564d;
      font-weight:600;
      border-bottom:1px solid var(--line);
    }
    tr:last-child td { border-bottom:0; }
    tr:hover td { background:#fbf8f0; }
    .num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }

    /* Tags */
    .tag {
      display:inline-flex; align-items:center;
      padding:3px 9px; border-radius:5px;
      background:var(--gold-soft); color:var(--gold);
      font-size:11.5px; font-weight:500;
      margin:0 4px 4px 0;
      white-space:nowrap;
    }
    .tag.price { background:var(--rose-soft); color:var(--rose); }
    .tag.catalogue { background:var(--green-soft); color:var(--green); }
    .tag.source { background:var(--blue-soft); color:var(--blue); text-transform:capitalize; }
    .tag.sm { padding:2px 7px; font-size:10.5px; }

    .muted { color:var(--muted); font-size:12.5px; }
    .empty {
      padding:40px;
      background:#fff;
      border:1px solid var(--line);
      border-radius:10px;
      color:var(--muted);
      text-align:center;
      font-style:italic;
    }
    a { color:var(--blue); text-decoration:none; }
    a:hover { text-decoration:underline; }

    /* Estimate range bar (visual) */
    .est-bar {
      display:flex; flex-direction:column;
      font-size:12px;
      min-width:140px;
    }
    .est-range { color:var(--muted); font-size:11px; }
    .est-hammer { font-weight:600; font-size:13px; }
    .est-hammer.over { color:var(--green); }
    .est-hammer.under { color:var(--rose); }
    .est-hammer.within { color:var(--ink); }

    /* Card grid (for auction houses) */
    .card-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:16px; }
    .card {
      background:#fff;
      border:1px solid var(--line);
      border-radius:12px;
      padding:20px;
      box-shadow:var(--shadow);
      transition:box-shadow 0.2s;
    }
    .card:hover { box-shadow:var(--shadow-lg); }
    .card-header { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:10px; }
    .card-title { font-family:'Fraunces', serif; font-size:20px; font-weight:700; margin:0; }
    .card-sub { color:var(--muted); font-size:12px; }
    .card-kv { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:13px; margin-top:10px; }
    .card-kv dt { color:var(--muted); }
    .card-kv dd { margin:0; font-weight:500; }
    .card-stats { display:flex; gap:16px; margin-top:12px; padding-top:12px; border-top:1px solid var(--line-soft); }
    .card-stat b { display:block; font-family:'Fraunces', serif; font-size:18px; font-weight:700; }
    .card-stat span { font-size:11px; color:var(--muted); text-transform:uppercase; }

    /* Detail page */
    .back-link { display:inline-block; margin-bottom:14px; color:var(--muted); font-size:13px; cursor:pointer; text-decoration:none; }
    .back-link:hover { color:var(--ink); }
    .detail-hero { background:#fff; border:1px solid var(--line); border-radius:12px; padding:24px 28px; margin-bottom:20px; box-shadow:var(--shadow); }
    .detail-hero h2 { font-family:'Fraunces', serif; font-size:32px; font-weight:700; margin:0 0 6px; }
    .detail-hero .sub { color:var(--muted); margin:0; font-size:14px; }
    .detail-kpis { display:flex; flex-wrap:wrap; gap:32px; margin-top:18px; padding-top:16px; border-top:1px solid var(--line-soft); }
    .kpi b { display:block; font-family:'Fraunces', serif; font-size:28px; font-weight:700; color:var(--ink); }
    .kpi span { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:0.04em; }
    .section-title { font-family:'Fraunces', serif; font-size:20px; font-weight:700; margin:28px 0 12px; }
    .chart-wrap { background:#fff; border:1px solid var(--line); border-radius:10px; padding:12px 16px; box-shadow:var(--shadow); }
    /* Report tab */
    .report-hero { margin:4px 0 20px; }
    .report-hero h2 { font-family:'Fraunces', serif; font-size:28px; margin:0 0 4px; }
    .report-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(380px, 1fr)); gap:16px; }
    .report-card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px 22px; box-shadow:var(--shadow); }
    .report-title { font-family:'Fraunces', serif; font-size:19px; font-weight:700; margin:0 0 2px; }
    .report-sub { font-size:13px; margin:0 0 14px; }
    .report-table { width:100%; border-collapse:collapse; font-size:13px; }
    .report-table th { text-align:left; font-weight:500; color:var(--muted); border-bottom:1px solid var(--line); padding:8px 6px; }
    .report-table th.num, .report-table td.num { text-align:right; }
    .report-table { table-layout:fixed; }
    .report-table td { padding:8px 6px; border-bottom:1px solid #f1f5f9; vertical-align:middle; }
    .report-table td.rank, .report-table th.rank { width:28px; color:var(--muted); }
    .report-table tbody tr:last-child td { border-bottom:none; }
    .report-table td.trunc, .report-table th.trunc { max-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .report-table td.trunc .artist-tag { max-width:100%; }
    .report-table td.primary { font-weight:500; color:#0f172a; }
    .report-table td.num { white-space:nowrap; font-variant-numeric:tabular-nums; }
    .report-table th.col-artist, .report-table td.col-artist { width:36%; }
    .report-table th.col-artist-wide, .report-table td.col-artist-wide { width:62%; }
    .report-table th.col-title, .report-table td.col-title { width:44%; }
    .report-table th.col-title-wide, .report-table td.col-title-wide { width:52%; }
    .report-table th.col-num, .report-table td.col-num { width:70px; }
    .report-table th.col-date, .report-table td.col-date { width:78px; }
    .report-table th.col-view, .report-table td.col-view { width:56px; text-align:right; }
    .report-table th.col-src, .report-table td.col-src { width:110px; }
    .report-table th.col-session, .report-table td.col-session { width:60%; }
    .artist-tag { display:inline-block; padding:3px 10px; border:1.5px solid #ef4444; color:#ef4444; border-radius:999px; font-size:12px; font-weight:500; cursor:pointer; text-decoration:none; white-space:nowrap; max-width:180px; overflow:hidden; text-overflow:ellipsis; }
    .artist-tag:hover { background:#fef2f2; }
    .city-tag { display:inline-block; padding:3px 10px; border:1.5px solid #f59e0b; color:#f59e0b; border-radius:999px; font-size:12px; font-weight:500; }
    .view-btn { display:inline-block; padding:2px 10px; border:1px solid var(--line); border-radius:6px; font-size:11px; font-weight:500; color:var(--muted); text-decoration:none; cursor:pointer; }
    .show-all { display:inline-block; margin-top:10px; font-size:12px; color:var(--blue); cursor:pointer; }
    .show-all:hover { text-decoration:underline; }
    .kind-badge { display:inline-block; font-size:10px; font-weight:500; padding:2px 7px; border-radius:6px; vertical-align:middle; margin-left:6px; }
    .kind-badge.house { background:#dcfce7; color:#166534; }
    .kind-badge.platform { background:#fef3c7; color:#92400e; }
    /* Kind pill — non-painting types in sales/lot tables */
    .kind-pill { display:inline-block; font-size:10px; font-weight:600; padding:2px 7px; border-radius:4px; vertical-align:middle; letter-spacing:0.2px; white-space:nowrap; }
    .kind-pill.kind-sculpture { background:#fde7d4; color:#9a4a16; }
    .kind-pill.kind-print     { background:#dbeafe; color:#1e40af; }
    .kind-pill.kind-drawing   { background:#f3e8ff; color:#6b21a8; }
    .kind-pill.kind-medal     { background:#fef3c7; color:#92400e; }
    /* Support filter chips on artist price chart */
    .support-chips { display:flex; gap:6px; flex-wrap:wrap; margin:8px 0 12px; }
    .support-chips .chip { padding:5px 12px; border:1px solid var(--line); background:#fff; border-radius:999px; font-size:12px; font-weight:500; color:var(--ink); cursor:pointer; transition:all 0.15s; }
    .support-chips .chip:hover { background:var(--bg); }
    .support-chips .chip.active { background:var(--ink); color:#fff; border-color:var(--ink); }
    /* Suspicious lots block — full-width dedicated section */
    .report-block-fullwidth { display:block; }
    .report-block-fullwidth .report-card { background:#fff; border:1px solid #fecaca; border-left:3px solid #dc2626; overflow-x:auto; padding:18px 22px; }
    .report-block-fullwidth .report-table { table-layout:fixed; min-width:1500px; width:1500px; }
    .report-block-fullwidth .report-table td, .report-block-fullwidth .report-table th { vertical-align:top; padding:10px 8px; }
    .report-block-fullwidth .report-table th.sus-col-artist, .report-block-fullwidth .report-table td.sus-col-artist { width:140px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .report-block-fullwidth .report-table th.sus-col-title, .report-block-fullwidth .report-table td.sus-col-title { width:auto; min-width:380px; white-space:normal; word-wrap:break-word; }
    .report-block-fullwidth .report-table th.sus-col-narrow, .report-block-fullwidth .report-table td.sus-col-narrow { width:80px; white-space:nowrap; }
    .report-block-fullwidth .report-table th.sus-col-dim, .report-block-fullwidth .report-table td.sus-col-dim { width:120px; white-space:nowrap; }
    .report-block-fullwidth .report-table th.sus-col-date, .report-block-fullwidth .report-table td.sus-col-date { width:100px; white-space:nowrap; }
    .report-block-fullwidth .report-table th.sus-col-num, .report-block-fullwidth .report-table td.sus-col-num { width:120px; white-space:nowrap; text-align:right; }
    .report-block-fullwidth .report-table th.sus-col-flag, .report-block-fullwidth .report-table td.sus-col-flag { width:120px; white-space:nowrap; }
    .role-badge { display:inline-block; font-size:13px; font-weight:500; padding:3px 10px; border-radius:999px; background:var(--green-soft); color:var(--green); vertical-align:middle; margin-left:10px; font-family:Inter; }
    /* Inline role pill for artist list rows — smaller and per-role colour */
    .role-pill { display:inline-block; font-size:10px; font-weight:600; padding:2px 7px; border-radius:4px; vertical-align:middle; margin-left:6px; letter-spacing:0.2px; white-space:nowrap; }
    .role-pill.role-painter      { background:#e0ebe4; color:#1f4a36; }
    .role-pill.role-sculptor     { background:#fde7d4; color:#9a4a16; }
    .role-pill.role-printmaker   { background:#dbeafe; color:#1e40af; }
    .role-pill.role-installation { background:#f3e8ff; color:#6b21a8; }
    .role-pill.role-performance  { background:#fce7f3; color:#9d174d; }
    .role-pill.role-video        { background:#ccfbf1; color:#115e59; }
    .role-pill.role-multi        { background:#e0e7ff; color:#3730a3; }
    .role-pill.role-workshop     { background:#fef3c7; color:#92400e; }
    /* Momentum % colouring — hot (overshoot >+30%), cold (<-10%) */
    .pct-hot  { color:#16a34a; font-weight:600; }
    .pct-cold { color:#dc2626; font-weight:600; }
    .view-btn:hover { background:#f8fafc; color:#0f172a; }
    .src-badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:500; background:#f1f5f9; color:#475569; }
    .src-badge.src-bonhams { background:#eef2ff; color:#4338ca; }
    .src-badge.src-christies { background:#fef2f2; color:#b91c1c; }
    .src-badge.src-sothebys { background:#fffbeb; color:#b45309; }
    .src-badge.src-millon { background:#f0fdf4; color:#15803d; }
    .src-badge.src-invaluable { background:#faf5ff; color:#7c3aed; }
    a.row-link, .row-link { color:var(--blue); cursor:pointer; text-decoration:none; }
    a.row-link:hover, .row-link:hover { text-decoration:underline; }

    @media (max-width: 1180px) {
      .stats { grid-template-columns:repeat(4,1fr); }
    }
    @media (max-width: 820px) {
      .stats { grid-template-columns:repeat(2,1fr); }
      main, header { padding-left:18px; padding-right:18px; }
      table { display:block; overflow-x:auto; white-space:nowrap; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="logo">A</div>
      <h1>Artonis — Artist Price Intelligence</h1>
    </div>
    <p>Nền tảng tổng hợp giá tranh Việt Nam từ nhiều nguồn: triển lãm galleries, đấu giá quốc tế (Bonhams, Millon, Invaluable) và các catalog nội bộ.</p>
  </header>
  <main>
    <div id="stats" class="stats"></div>
    <div class="toolbar">
      <input id="q" placeholder="Tìm họa sĩ, triển lãm, tác phẩm..." autocomplete="off">
      <select id="filter">
        <option value="all">Tất cả</option>
        <option value="has_price">Có file giá</option>
        <option value="has_catalogue">Có catalogue</option>
        <option value="has_observation">Đã parse giá</option>
      </select>
      <button id="reload">↻ Refresh</button>
    </div>
    <div id="tabs-wrap">
      <div class="tabs">
        <button class="tab active" data-tab="artists">Nghệ sĩ</button>
        <button class="tab" data-tab="sales">Đấu giá</button>
        <button class="tab" data-tab="auction-houses">Nhà đấu giá</button>
        <button class="tab" data-tab="exhibitions">Triển lãm</button>
        <button class="tab" data-tab="galleries">Gallery & Venue</button>
        <button class="tab" data-tab="report">Report</button>
        <button class="tab" data-tab="files">Nguồn file</button>
      </div>
    </div>
    <section id="artists" class="active"></section>
    <section id="sales"></section>
    <section id="auction-houses"></section>
    <section id="exhibitions"></section>
    <section id="galleries"></section>
    <section id="report"></section>
    <section id="files"></section>
    <section id="detail-page"></section>
  </main>
<script>
let data = null;
let activeTab = "artists";
const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
function money(v){ return v === null || v === undefined ? "" : fmt.format(v); }
function na(v){ return (v === null || v === undefined || v === 0 || v === "") ? '<span class="muted">N/A</span>' : null; }
function moneyOrNa(v){ const n = na(v); return n !== null ? n : '$' + fmt.format(Math.round(v)); }
function numOrNa(v){ const n = na(v); return n !== null ? n : fmt.format(v); }
function fmtYears(b, d, name){
  // Workshops (Xưởng / Atelier / Studio) aren't people — show a workshop tag instead of years.
  if (name && /xưởng|atelier|studio/i.test(name)) return '<span class="muted">Xưởng</span>';
  if (b === -20) return 'Thế kỷ 20';
  if (b === -19) return 'Thế kỷ 19';
  if (!b) return '<span class="muted">N/A</span>';
  return b + (d ? '–' + d : '');
}

// Medium tags — describe what the artist works in (Tate/MoMA/Artsy convention).
// One tag per kind the artist has; no "role title" overlay.
// Workshops keep their entity tag because they're orgs, not individual practices.
const KIND_TAG = {
  painting:     'Tranh',
  sculpture:    'Điêu khắc',
  print:        'Đồ họa',
  installation: 'Sắp đặt',
  performance:  'Trình diễn',
  video:        'Video',
};
function artistRoles(a){
  if (a.display_name && /xưởng|atelier|studio/i.test(a.display_name)) return ['Xưởng'];
  const counts = {
    painting:     a.painting_count || 0,
    sculpture:    a.sculpture_count || 0,
    print:        a.print_count || 0,
    installation: a.installation_count || 0,
    performance:  a.performance_count || 0,
    video:        a.video_count || 0,
  };
  return Object.keys(counts).filter(k => counts[k] > 0).map(k => KIND_TAG[k]);
}
// Backwards-compat for callers wanting a joined label string.
function artistRole(a){
  return artistRoles(a).join(' • ');
}
function text(v){ return (v ?? "").toString(); }
function esc(v){ return text(v).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }

// Distinct color per top artist for the yearly-median multi-line chart
const ARTIST_COLOR = {
  'Lê Phổ':         '#dc2626',
  'Mai Trung Thứ':  '#2563eb',
  'Vũ Cao Đàm':     '#16a34a',
  'Bùi Xuân Phái':  '#9333ea',
  'Nguyễn Gia Trí': '#ea580c',
  'Lê Thị Lựu':     '#0891b2',
};
function colorForArtist(name){ return ARTIST_COLOR[name] || '#64748b'; }

// Multi-line chart: median $/m² per top artist over years (log Y because $30K-$2.8M range).
function buildYearlyMedianChart(byArtist, opts){
  opts = opts || {};
  const W = opts.width || 920;
  const H = opts.height || 380;
  const PAD_L = opts.isPreview ? 50 : 80;
  const PAD_R = opts.isPreview ? 12 : 30;
  const PAD_T = 24;
  const PAD_B = opts.isPreview ? 30 : 44;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  const allPts = [];
  Object.entries(byArtist).forEach(([name, arr]) => {
    arr.forEach(p => allPts.push({ artist: name, yr: p.yr, v: p.median_ppm, n: p.n }));
  });
  if (allPts.length < 2) return '';

  const years = allPts.map(p => p.yr);
  const yMin = Math.min(...years), yMax = Math.max(...years);
  const vals = allPts.map(p => p.v).filter(v => v > 0);
  if (vals.length < 2) return '';
  const vMin = Math.max(1, Math.min(...vals));
  const vMax = Math.max(...vals);
  const logMin = Math.log10(vMin), logMax = Math.log10(vMax);

  const xScale = y => yMax === yMin ? PAD_L + plotW/2
    : PAD_L + ((y - yMin) / (yMax - yMin)) * plotW;
  const yScale = v => PAD_T + plotH - ((Math.log10(Math.max(v, 1)) - logMin) / (logMax - logMin || 1)) * plotH;

  const span = yMax - yMin;
  const step = Math.max(1, Math.ceil(span / (opts.isPreview ? 5 : 10)));
  const xTicks = [];
  for (let y = yMin; y <= yMax; y += step) xTicks.push(y);
  if (xTicks[xTicks.length - 1] !== yMax) xTicks.push(yMax);

  const yTicks = [];
  for (let exp = Math.floor(logMin); exp <= Math.ceil(logMax); exp++) {
    const base = Math.pow(10, exp);
    [1, 3].forEach(m => {
      const v = base * m;
      if (v >= vMin * 0.9 && v <= vMax * 1.1) yTicks.push(v);
    });
  }

  const grid = yTicks.map(v => `<line x1="${PAD_L}" x2="${W - PAD_R}" y1="${yScale(v).toFixed(1)}" y2="${yScale(v).toFixed(1)}" stroke="#f1f5f9" stroke-width="1"/>`).join('') +
               xTicks.map(y => `<line x1="${xScale(y).toFixed(1)}" x2="${xScale(y).toFixed(1)}" y1="${PAD_T}" y2="${PAD_T + plotH}" stroke="#f8fafc" stroke-width="1"/>`).join('');

  const lines = Object.entries(byArtist).map(([name, arr]) => {
    if (arr.length < 2) return '';
    const color = colorForArtist(name);
    const pathD = arr.map((p, i) => `${i === 0 ? 'M' : 'L'}${xScale(p.yr).toFixed(1)},${yScale(p.median_ppm).toFixed(1)}`).join(' ');
    const dots = arr.map(p => `<circle cx="${xScale(p.yr).toFixed(1)}" cy="${yScale(p.median_ppm).toFixed(1)}" r="3" fill="${color}" stroke="#fff" stroke-width="1.5"><title>${esc(name)} — ${p.yr}: $${Math.round(p.median_ppm).toLocaleString()}/m² (n=${p.n})</title></circle>`).join('');
    return `<path d="${pathD}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" stroke-opacity="0.85"/>${dots}`;
  }).join('');

  const yLabels = yTicks.map(v => `<text x="${PAD_L - 8}" y="${(yScale(v) + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="#94a3b8" font-family="Inter">${fmtCompact(v)}</text>`).join('');
  const xLabels = xTicks.map(y => `<text x="${xScale(y).toFixed(1)}" y="${H - PAD_B + 16}" text-anchor="middle" font-size="10" fill="#64748b" font-family="Inter">${y}</text>`).join('');
  const bg = `<rect x="${PAD_L}" y="${PAD_T}" width="${plotW}" height="${plotH}" fill="#fafafa" stroke="#e2e8f0" stroke-width="1"/>`;

  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" style="max-width:${W}px;height:auto;background:#fff">
    ${bg}${grid}${lines}${yLabels}${xLabels}
  </svg>`;
}

// Kind pill — flag non-painting lots in sales table so user can spot them at a glance.
// Painting is the default (no badge → reduces noise).
const KIND_LABELS = {
  sculpture: { label: 'Tượng', cls: 'kind-sculpture' },
  print:     { label: 'Tranh in', cls: 'kind-print' },
  drawing:   { label: 'Ký họa', cls: 'kind-drawing' },
  medal:     { label: 'Huy chương', cls: 'kind-medal' },
};
function kindBadge(kind){
  const k = KIND_LABELS[kind];
  if (!k) return '';
  return ` <span class="kind-pill ${k.cls}">${k.label}</span>`;
}
// Vietnamese label for support_type — used in artist chart filter chips + sales table
const SUPPORT_LABEL_VN = {
  canvas: 'Canvas', silk: 'Lụa', paper: 'Giấy',
  lacquer: 'Sơn mài', panel: 'Panel/board', metal: 'Kim loại',
};

// Collect yearly price points for an artist (combines auction sales + gallery observations).
// Each point: { year, price_usd, label, source }. Uses sale_date for auctions, exhibition start_date for gallery.
function collectYearlyPrices(artist){
  const pts = [];
  (data.sales || []).forEach(s => {
    if (!(s.artist_id == artist.id || s.artist_name === (artist.display_name || artist.name))) return;
    if (!s.price_usd || !s.sale_date) return;
    const yr = parseInt((s.sale_date || '').slice(0, 4), 10);
    if (!yr || yr < 1990 || yr > 2030) return;
    pts.push({ year: yr, price_usd: +s.price_usd, source: s.source || 'auction',
               support: s.support_type || null, kind: s.kind || 'painting',
               label: (s.artwork_title || '') + (s.auction_title ? ' — ' + s.auction_title : '') });
  });
  // Gallery observations use exhibition.start_date for the X-axis
  const exhByTitle = {};
  (data.exhibitions || []).forEach(e => { exhByTitle[e.title] = e; });
  (data.observations || []).forEach(o => {
    if (o.artist_name !== (artist.display_name || artist.name)) return;
    if (!o.price_amount) return;
    // Convert to USD if possible using a minimal map (assume VND — main gallery currency)
    let usd = null;
    const rate = { VND: 1/25000, USD: 1, EUR: 1.08, GBP: 1.28, HKD: 0.128, SGD: 0.75 };
    if (o.currency && rate[o.currency]) usd = o.price_amount * rate[o.currency];
    if (!usd) return;
    const exh = exhByTitle[o.exhibition_title];
    const yr = exh && exh.start_date ? parseInt((exh.start_date || '').slice(0, 4), 10) : null;
    if (!yr || yr < 1990 || yr > 2030) return;
    pts.push({ year: yr, price_usd: usd, source: 'gallery',
               label: (o.artwork_title || '') + (o.exhibition_title ? ' — ' + o.exhibition_title : '') });
  });
  return pts;
}

// Aggregate points by year → {year, median, p25, p75, min, max, n}.
function yearlyAgg(pts){
  const by = {};
  pts.forEach(p => { (by[p.year] = by[p.year] || []).push(p.price_usd); });
  return Object.keys(by).map(y => {
    const arr = by[y].slice().sort((a,b) => a - b);
    const med = arr[Math.floor(arr.length / 2)];
    const p25 = arr[Math.floor(arr.length * 0.25)];
    const p75 = arr[Math.floor(arr.length * 0.75)];
    return { year: +y, median: med, p25, p75, min: arr[0], max: arr[arr.length-1], n: arr.length };
  }).sort((a,b) => a.year - b.year);
}

// Re-render artist chart in place with selected support filter.
function setChartSupport(support){
  window._chartActiveSupport = support;
  const a = window._chartArtist;
  if (!a) return;
  const newHTML = priceHistoryChart(a);
  // Find and replace existing chart block (chips + chart-wrap)
  const heading = [...document.querySelectorAll('h3.section-title')].find(h => h.textContent.includes('Biến động giá'));
  if (!heading) return;
  const toRemove = [heading];
  let n = heading.nextElementSibling;
  while (n && (n.classList.contains('support-chips') || n.classList.contains('chart-wrap'))) {
    toRemove.push(n);
    n = n.nextElementSibling;
  }
  const tmp = document.createElement('div');
  tmp.innerHTML = newHTML;
  const parent = heading.parentNode;
  while (tmp.firstChild) parent.insertBefore(tmp.firstChild, heading);
  toRemove.forEach(el => el.remove());
}

// Render inline-SVG price-over-time chart. Log-Y scale, scatter dots + median line.
// Adds a support filter chip row when the artist has work in 2+ supports.
function priceHistoryChart(artist){
  const allPts = collectYearlyPrices(artist);
  if (allPts.length < 2) return '';
  window._chartArtist = artist;

  // Build support filter chips (only if 2+ supports with ≥2 points each)
  const supportCounts = {};
  allPts.forEach(p => { const s = p.support || 'unknown'; supportCounts[s] = (supportCounts[s] || 0) + 1; });
  const supportsAvailable = Object.entries(supportCounts)
    .filter(([k, v]) => v >= 2 && k !== 'unknown')
    .sort((a, b) => b[1] - a[1])
    .map(([k]) => k);

  const active = window._chartActiveSupport || 'all';
  const pts = active === 'all' ? allPts : allPts.filter(p => p.support === active);
  if (pts.length < 2) {
    // Reset and re-render with all
    window._chartActiveSupport = 'all';
    return priceHistoryChart(artist);
  }

  let chips = '';
  if (supportsAvailable.length >= 2) {
    const allCls = active === 'all' ? 'active' : '';
    chips = `<div class="support-chips">
      <button class="chip ${allCls}" onclick="setChartSupport('all')">Tất cả (${allPts.length})</button>` +
      supportsAvailable.map(s => {
        const cls = active === s ? 'active' : '';
        return `<button class="chip ${cls}" onclick="setChartSupport('${s}')">${SUPPORT_LABEL_VN[s] || s} (${supportCounts[s]})</button>`;
      }).join('') + `</div>`;
  }
  if (pts.length < 2) return '';
  const agg = yearlyAgg(pts);
  const years = agg.map(a => a.year);
  const yMin = Math.min(...years), yMax = Math.max(...years);
  const prices = pts.map(p => p.price_usd);
  const pMin = Math.max(1, Math.min(...prices));
  const pMax = Math.max(...prices);
  // Log scale helpers
  const logMin = Math.log10(pMin), logMax = Math.log10(pMax);
  const W = 760, H = 320, PAD_L = 72, PAD_R = 24, PAD_T = 24, PAD_B = 42;
  const plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
  const xScale = y => PAD_L + (yMax === yMin ? plotW/2 : ((y - yMin) / (yMax - yMin)) * plotW);
  const yScale = p => PAD_T + plotH - ((Math.log10(Math.max(p, 1)) - logMin) / (logMax - logMin || 1)) * plotH;
  // X-axis year ticks (integer steps, ≤10 ticks)
  const step = Math.max(1, Math.ceil((yMax - yMin) / 10));
  const xTicks = [];
  for (let y = yMin; y <= yMax; y += step) xTicks.push(y);
  // Y-axis log ticks: 1k, 10k, 100k, 1M, 10M...
  const yTicks = [];
  for (let e = Math.floor(logMin); e <= Math.ceil(logMax); e++) {
    yTicks.push(Math.pow(10, e));
  }
  const fmtUSD = v => {
    if (v >= 1e6) return '$' + (v/1e6).toFixed(v >= 1e7 ? 0 : 1) + 'M';
    if (v >= 1e3) return '$' + Math.round(v/1e3) + 'K';
    return '$' + Math.round(v);
  };
  // Scatter dots (colored by source)
  const colorOf = s => ({ bonhams:'#6366f1', christies:'#ef4444', sothebys:'#f59e0b',
                          millon:'#22c55e', invaluable:'#a855f7',
                          gallery:'#64748b' }[s] || '#94a3b8');
  const dots = pts.map(p => `<circle cx="${xScale(p.year).toFixed(1)}" cy="${yScale(p.price_usd).toFixed(1)}" r="3.5" fill="${colorOf(p.source)}" fill-opacity="0.72" stroke="#fff" stroke-width="0.8"><title>${esc(p.label)}\n${p.year} · ${fmtUSD(p.price_usd)} · ${p.source}</title></circle>`).join('');
  // Median polyline
  const pathD = agg.map((a, i) => `${i === 0 ? 'M' : 'L'}${xScale(a.year).toFixed(1)},${yScale(a.median).toFixed(1)}`).join(' ');
  const medianLine = `<path d="${pathD}" fill="none" stroke="#0f172a" stroke-width="2" stroke-opacity="0.75"/>`;
  const medianDots = agg.map(a => `<circle cx="${xScale(a.year).toFixed(1)}" cy="${yScale(a.median).toFixed(1)}" r="4" fill="#0f172a"><title>Median ${a.year}: ${fmtUSD(a.median)} (n=${a.n})</title></circle>`).join('');
  // Grid + axes
  const xAxisTicks = xTicks.map(y => `
    <line x1="${xScale(y).toFixed(1)}" x2="${xScale(y).toFixed(1)}" y1="${PAD_T}" y2="${PAD_T + plotH}" stroke="#e2e8f0" stroke-dasharray="2,3"/>
    <text x="${xScale(y).toFixed(1)}" y="${H - PAD_B + 18}" text-anchor="middle" font-size="11" fill="#64748b">${y}</text>`).join('');
  const yAxisTicks = yTicks.map(p => `
    <line x1="${PAD_L}" x2="${W - PAD_R}" y1="${yScale(p).toFixed(1)}" y2="${yScale(p).toFixed(1)}" stroke="#e2e8f0" stroke-dasharray="2,3"/>
    <text x="${PAD_L - 8}" y="${(yScale(p) + 3).toFixed(1)}" text-anchor="end" font-size="11" fill="#64748b">${fmtUSD(p)}</text>`).join('');
  // Legend
  const sources = [...new Set(pts.map(p => p.source))];
  const legend = sources.map((s, i) => `<g transform="translate(${PAD_L + i*100}, ${H - 6})">
    <circle cx="0" cy="-4" r="4" fill="${colorOf(s)}"/>
    <text x="8" y="0" font-size="11" fill="#334155">${esc(s)}</text></g>`).join('');
  return `
    <h3 class="section-title">Biến động giá qua các năm <span class="muted" style="font-family:Inter;font-weight:400;font-size:13px">(${pts.length} điểm, ${agg.length} năm, USD — log scale)</span></h3>
    ${chips}
    <div class="chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" style="max-width:${W}px;height:auto">
        <rect x="${PAD_L}" y="${PAD_T}" width="${plotW}" height="${plotH}" fill="#f8fafc" stroke="#cbd5e1"/>
        ${yAxisTicks}${xAxisTicks}
        ${dots}
        ${medianLine}${medianDots}
        ${legend}
      </svg>
    </div>`;
}
function includes(row, q){ return JSON.stringify(row).toLowerCase().includes(q.toLowerCase()); }
function filtered(list){
  if (!Array.isArray(list)) return [];
  const q = document.querySelector("#q").value.trim();
  const f = document.querySelector("#filter").value;
  return list.filter(row => {
    if (q && !includes(row, q)) return false;
    if (f === "has_price" && !(row.price_file_count > 0 || row.has_price_hint > 0)) return false;
    if (f === "has_catalogue" && !(row.catalogue_file_count > 0 || row.has_catalogue_hint > 0)) return false;
    if (f === "has_observation" && !(row.price_count > 0 || row.price_amount > 0)) return false;
    return true;
  });
}
function renderStats(){
  const s = data.summary;
  const items = [
    ["Nghệ sĩ", s.artist_count],
    ["Triển lãm", s.exhibition_count],
    ["Giá từ gallery", s.price_observation_count],
    ["KQ đấu giá", s.auction_sale_count],
    ["Nhà đấu giá", (data.auction_houses||[]).length],
    ["Catalogue", s.catalogue_file_count],
    ["File giá", s.price_file_count],
  ];
  document.querySelector("#stats").innerHTML = items.map(([label,value]) => `<div class="stat"><b>${money(value)}</b><span>${label}</span></div>`).join("");
}
function table(headers, rows, empty){
  if (!rows.length) return `<div class="empty">${empty}</div>`;
  const th = headers.map(h => {
    const label = typeof h === 'string' ? h : h.label;
    const isNum = typeof h === 'object' ? h.num : /\$|\/m²|^số|^triển lãm$|^sinh|^mất|^estimate|^hammer|^premium/i.test(label);
    return `<th${isNum ? ' class="num"' : ''}>${label}</th>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

// Format estimate-hammer comparison visually
function hammerVsEst(hammer, estLow, estHigh){
  if (!hammer) return '';
  let cls = 'within';
  if (estLow && hammer < estLow) cls = 'under';
  else if (estHigh && hammer > estHigh) cls = 'over';
  return cls;
}
// Compact inline sparkline for artists table (yearly median, log-Y).
function sparkline(artist){
  const pts = collectYearlyPrices(artist);
  if (pts.length < 2) return '';
  const agg = yearlyAgg(pts);
  if (agg.length < 2) return '';
  const W = 110, H = 28, PAD = 2;
  const years = agg.map(a => a.year);
  const yMin = Math.min(...years), yMax = Math.max(...years);
  const vals = agg.map(a => a.median);
  const logs = vals.map(v => Math.log10(Math.max(v, 1)));
  const lMin = Math.min(...logs), lMax = Math.max(...logs);
  const xs = agg.map(a => PAD + (yMax === yMin ? (W-2*PAD)/2 : ((a.year - yMin)/(yMax - yMin)) * (W - 2*PAD)));
  const ys = logs.map(l => PAD + (H - 2*PAD) - ((l - lMin)/(lMax - lMin || 1)) * (H - 2*PAD));
  const path = xs.map((x, i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ');
  const trend = vals[vals.length - 1] >= vals[0] ? '#16a34a' : '#dc2626';
  const first = `<circle cx="${xs[0].toFixed(1)}" cy="${ys[0].toFixed(1)}" r="1.8" fill="#94a3b8"/>`;
  const last = `<circle cx="${xs[xs.length-1].toFixed(1)}" cy="${ys[ys.length-1].toFixed(1)}" r="2.2" fill="${trend}"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="vertical-align:middle">
    <path d="${path}" fill="none" stroke="${trend}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    ${first}${last}
    <title>${agg.length} năm · ${agg[0].year}–${agg[agg.length-1].year}</title>
  </svg>`;
}

// Tiny medium tag, coloured per category. Accepts a single tag or an array.
const ROLE_CLASS = {
  'Tranh':       'role-painter',
  'Điêu khắc':   'role-sculptor',
  'Đồ họa':      'role-printmaker',
  'Sắp đặt':     'role-installation',
  'Trình diễn':  'role-performance',
  'Video':       'role-video',
  'Xưởng':       'role-workshop',
};
function rolePill(role){
  if (!role) return '';
  const cls = ROLE_CLASS[role] || 'role-painter';
  return `<span class="role-pill ${cls}">${esc(role)}</span>`;
}
function rolePills(roles){
  if (!roles || !roles.length) return '';
  return roles.map(rolePill).join(' ');
}

function renderArtists(){
  const rows = filtered(data.artists).map(a => {
    const display = a.display_name || a.name;
    const years = fmtYears(a.birth_year, a.death_year, display);
    const roles = artistRoles(a);
    return `<tr style="cursor:pointer" onclick="location.hash='#artist/${a.id}'">
      <td><strong class="row-link">${esc(display)}</strong> ${rolePills(roles)}<br><span class="muted">${years}</span></td>
      <td class="num">${numOrNa(a.price_count)}</td>
      <td class="num">${numOrNa(a.auction_count)}</td>
      <td class="num">${moneyOrNa(a.overall_min_usd)}</td>
      <td class="num">${moneyOrNa(a.overall_avg_usd)}</td>
      <td class="num"><strong style="color:var(--green)">${moneyOrNa(a.overall_max_usd)}</strong></td>
      <td class="num">${moneyOrNa(a.overall_median_per_m2_usd)}</td>
      <td>${sparkline(a)}</td>
      <td class="num">${numOrNa(a.exhibition_count)}</td>
    </tr>`;
  });
  document.querySelector("#artists").innerHTML = table(
    ["Nghệ sĩ","Số TP gallery","Số TP đấu giá","Min $ (gõ búa)","Avg $ (gõ búa)","Max $ (gõ búa)","Median $/m²","Xu hướng","Triển lãm"],
    rows, "Chưa có nghệ sĩ nào có data giá."
  );
}
function renderExhibitions(){
  const rows = filtered(data.exhibitions).map(e => `<tr style="cursor:pointer" onclick="location.hash='#exhibition/${e.id}'">
    <td><strong class="row-link">${esc(e.title)}</strong></td>
    <td>${esc((e.artists_text || '').replace(/\n/g,', '))}</td>
    <td>${esc(e.start_date || e.date_token || '')}</td>
    <td>${esc(e.venue || e.organizer || e.city || '—')}</td>
    <td>
      ${e.file_count ? `<span class="tag sm">${e.file_count} files</span>` : ''}
      ${e.price_file_count ? `<span class="tag price sm">${e.price_file_count} giá</span>` : ''}
      ${e.catalogue_file_count ? `<span class="tag catalogue sm">${e.catalogue_file_count} catalogue</span>` : ''}
    </td>
  </tr>`);
  document.querySelector("#exhibitions").innerHTML = table(["Triển lãm","Nghệ sĩ","Ngày","Địa điểm","Nguồn"], rows, "Chưa có triển lãm nào.");
}
function renderObservations(){
  const rows = filtered(data.observations).map(p => `<tr>
    <td><strong>${esc(p.artist_name)}</strong><br><span class="muted">${esc(p.exhibition_title)}</span></td>
    <td>${esc(p.artwork_title)}</td>
    <td>${esc(p.medium)}</td>
    <td>${esc(p.dimensions)}${p.area_m2 ? `<br><span class="muted">${p.area_m2} m²</span>` : ""}</td>
    <td class="num">${money(p.price_amount)} ${esc(p.currency)}</td>
    <td class="num">${p.price_per_m2 ? money(Math.round(p.price_per_m2)) + " $/m²" : ""}</td>
    <td>${esc(p.status)}</td>
  </tr>`);
  document.querySelector("#observations").innerHTML = table(["Họa sĩ","Tác phẩm","Chất liệu","Kích thước","Giá","Giá/m²","Status"], rows, "Chưa parse được dòng giá nào từ bảng giá.");
}
function renderSales(){
  const rows = filtered(data.sales || []).map(s => {
    const artistDisplay = s.artist_name || s.artist_name_raw;
    const house = (s.auction_title || '').split('—')[0].trim() || s.source || '';
    const dept = (s.auction_title || '').split('—')[1]?.trim() || '';
    const cur = s.currency || '';
    const isEstimateOnly = s.status === 'estimate_only';
    // Estimate vs hammer visual
    const estLow = s.estimate_low, estHigh = s.estimate_high, hammer = s.hammer_price;
    const estClass = hammerVsEst(hammer, estLow, estHigh);
    const estBadge = isEstimateOnly ? '≈' : (estClass === 'over' ? '↑' : (estClass === 'under' ? '↓' : '='));
    return `<tr>
      <td>
        <strong>${esc(artistDisplay)}</strong>
        ${s.lot_number ? `<br><span class="muted">lot ${esc(s.lot_number)}</span>` : ''}
      </td>
      <td>
        <a href="${esc(s.source_url)}" target="_blank">${esc(s.artwork_title) || '(no title)'}</a>${kindBadge(s.kind)}
        ${s.dimensions ? `<br><span class="muted">${esc(s.dimensions)}${s.area_m2 ? ' · '+s.area_m2+' m²' : ''}${s.support_type ? ' · '+esc(SUPPORT_LABEL_VN[s.support_type] || s.support_type) : ''}</span>` : ''}
      </td>
      <td>
        <span class="tag source sm">${esc(s.source)}</span>
        <div style="margin-top:4px">${esc(house)}</div>
        ${dept ? `<span class="muted">${esc(dept)}</span>` : ''}
        ${s.sale_location ? `<br><span class="muted">${esc(s.sale_location)}</span>` : ''}
      </td>
      <td>${esc(s.sale_date || '')}</td>
      <td class="num">
        ${(estLow || estHigh) ? `<span class="muted">${money(Math.round(estLow || 0))}–${money(Math.round(estHigh || 0))} ${esc(cur)}</span>` : '<span class="muted">—</span>'}
      </td>
      <td class="num">
        <span class="est-hammer ${estClass}" title="${isEstimateOnly ? 'Giá estimate (chưa có hammer công bố)' : 'Giá gõ búa thực tế'}">${money(Math.round(hammer || 0))} ${esc(cur)} ${estBadge}</span>
        <br><span class="muted">${money(Math.round(s.price_usd || 0))} USD${isEstimateOnly ? ' <em style="color:var(--gold)">(est.)</em>' : ''}</span>
      </td>
      <td class="num">
        ${s.price_with_premium ? `${money(Math.round(s.price_with_premium))} ${esc(cur)}<br><span class="muted">${money(Math.round(s.price_with_premium_usd || 0))} USD</span>` : '<span class="muted">—</span>'}
      </td>
      <td class="num">${s.price_per_m2_usd ? money(Math.round(s.price_per_m2_usd)) : '<span class="muted">—</span>'}</td>
    </tr>`;
  });
  document.querySelector("#sales").innerHTML = table(
    [
      "Họa sĩ",
      "Tác phẩm",
      "Nhà đấu giá",
      "Ngày bán",
      {label:"Estimate", num:true},
      {label:"Giá gõ búa", num:true},
      {label:"+Premium buyer", num:true},
      {label:"$/m²", num:true},
    ],
    rows, "Chưa có kết quả đấu giá nào."
  );
}

function renderAuctionHouses(){
  const houses = data.auction_houses || [];
  if (!houses.length){ document.querySelector("#auction-houses").innerHTML = '<div class="empty">Chưa có data đấu giá.</div>'; return; }
  // Sort houses first, platforms last
  houses.sort((a, b) => {
    const ka = a.kind === 'platform' ? 1 : 0;
    const kb = b.kind === 'platform' ? 1 : 0;
    if (ka !== kb) return ka - kb;
    return (b.lot_count || 0) - (a.lot_count || 0);
  });
  const cards = houses.map(h => {
    const founded = h.founded ? `· est. ${h.founded}` : '';
    const kindBadge = h.kind === 'platform'
      ? `<span class="kind-badge platform">Platform</span>`
      : `<span class="kind-badge house">Nhà đấu giá</span>`;
    return `<div class="card" style="cursor:pointer" onclick="location.hash='#auction/${encodeURIComponent(h.source)}'">
      <div class="card-header">
        <div>
          <h3 class="card-title row-link">${esc(h.display_name)} ${kindBadge}</h3>
          <span class="card-sub">${esc(h.country || '')} ${founded}</span>
        </div>
        ${h.website ? `<a href="${esc(h.website)}" target="_blank" style="font-size:12px" onclick="event.stopPropagation()">↗</a>` : ''}
      </div>
      <dl class="card-kv">
        <dt>Dept VN art:</dt><dd>${esc(h.vietnamese_art_dept || '—')}</dd>
        <dt>Buyer's premium:</dt><dd><strong>${h.premium_rate_pct !== null && h.premium_rate_pct !== undefined ? h.premium_rate_pct + '%' : '—'}</strong></dd>
        ${h.premium_note ? `<dt></dt><dd><span class="muted">${esc(h.premium_note)}</span></dd>` : ''}
        <dt>Tax/VAT:</dt><dd>${h.vat_pct !== null && h.vat_pct !== undefined ? h.vat_pct + '%' : '—'}</dd>
        ${h.tax_note ? `<dt></dt><dd><span class="muted">${esc(h.tax_note)}</span></dd>` : ''}
      </dl>
      <div class="card-stats">
        <div class="card-stat"><b>${money(h.lot_count)}</b><span>Lots</span></div>
        <div class="card-stat"><b>${money(h.session_count || 0)}</b><span>Phiên</span></div>
        <div class="card-stat"><b>${money(h.artist_count)}</b><span>Nghệ sĩ</span></div>
        <div class="card-stat"><b>$${money(Math.round(h.max_usd || 0))}</b><span>Max hammer</span></div>
      </div>
    </div>`;
  }).join('');
  document.querySelector("#auction-houses").innerHTML = `<div class="card-grid">${cards}</div>`;
}
function renderFiles(){
  const rows = filtered(data.files).map(f => `<tr>
    <td><strong>${esc(f.filename)}</strong><br><span class="muted">${esc(f.drive_path)}</span></td>
    <td>${esc(f.extension)}</td>
    <td>${esc(f.source_kind)}</td>
    <td>${f.has_price_hint ? '<span class="tag price">price</span>' : ""}${f.has_catalogue_hint ? '<span class="tag catalogue">catalogue</span>' : ""}</td>
  </tr>`);
  document.querySelector("#files").innerHTML = table(["File","Ext","Loại","Hint"], rows, "Chưa có source file nào.");
}
function galleryKind(name){
  const low = (name || '').toLowerCase();
  // Museums / associations first (more specific)
  if (/bảo tàng|museum|hội mỹ thuật|art museum|trung tâm/i.test(low)) return 'venue';
  // Known private galleries
  if (/gallery|galarie|galerie|art space|house of art|artspace|salon|huyen art|wiking|chillala|annam|quỳnh|aiii|schiller|sann/i.test(low)) return 'gallery';
  return 'other';
}
function renderGalleries(){
  const gs = data.galleries || [];
  if (!gs.length){ document.querySelector("#galleries").innerHTML = '<div class="empty">Chưa có data gallery.</div>'; return; }
  // Group by kind
  const byKind = { gallery: [], venue: [], other: [] };
  gs.forEach(g => byKind[galleryKind(g.gallery)].push(g));
  const labels = { gallery: '🎨 Gallery (phòng trưng bày)', venue: '🏛️ Bảo tàng / Hội mỹ thuật', other: '📍 Nơi khác' };
  const renderCards = (list) => list.map(g => `
    <div class="card" style="cursor:pointer" onclick="location.hash='#gallery/${encodeURIComponent(g.gallery)}'">
      <div class="card-header">
        <div>
          <h3 class="card-title row-link">${esc(g.gallery)}</h3>
          <span class="card-sub">${g.first_date || '?'} → ${g.last_date || '?'}</span>
        </div>
      </div>
      <div class="card-stats">
        <div class="card-stat"><b>${g.exhibition_count}</b><span>Triển lãm</span></div>
        <div class="card-stat"><b>${g.artist_count || 0}</b><span>Nghệ sĩ</span></div>
      </div>
    </div>`).join('');
  const html = ['gallery','venue','other'].filter(k => byKind[k].length).map(k => `
    <h3 class="section-title">${labels[k]} (${byKind[k].length})</h3>
    <div class="card-grid" style="grid-template-columns:repeat(auto-fill,minmax(280px,1fr))">${renderCards(byKind[k])}</div>
  `).join('');
  document.querySelector("#galleries").innerHTML = html;
}
// ============ REPORT TAB (HENI-style data cards) ============
// columns: [{label, cls, align?}, ...]; rows: [{cells: [string_html_or_text, ...], action?}]
// Stash full datasets so the "Xem tất cả" link can re-render with no truncation.
const REPORT_FULL_DATA = {};

function _reportCardBody(columns, rows){
  const hasAction = rows.length && rows[0].action;
  const body = rows.length
    ? rows.map((r, i) => `<tr>
        <td class="rank">${i + 1}</td>
        ${r.cells.map((c, j) => `<td class="${columns[j] && columns[j].cls || ''}" ${c.tip ? `title="${esc(c.tip)}"` : ''}>${c.html || esc(c.text || '')}</td>`).join('')}
        ${r.action ? `<td class="col-view"><a class="view-btn" ${r.action.href ? `href="${esc(r.action.href)}" target="_blank" rel="noopener"` : ''} ${r.action.onclick ? `onclick="${r.action.onclick}"` : ''}>VIEW</a></td>` : ''}
      </tr>`).join('')
    : `<tr><td colspan="${columns.length + 1 + (hasAction ? 1 : 0)}" class="muted" style="text-align:center;padding:20px">Chưa có dữ liệu.</td></tr>`;
  const head = columns.map(c => `<th class="${c.cls || ''}">${esc(c.label)}</th>`).join('');
  return `
    <table class="report-table">
      <thead><tr><th class="rank">#</th>${head}${hasAction ? '<th class="col-view"></th>' : ''}</tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function reportCard(title, subtitle, columns, rows, opts){
  opts = opts || {};
  // Hide cards with no data — keeps the Report tidy.
  if (!rows || rows.length === 0) return '';
  const cardKey = opts.key || title;
  const limit = opts.preview || 8;
  const preview = rows.slice(0, limit);
  // Stash for "Xem tất cả" expansion
  REPORT_FULL_DATA[cardKey] = { title, subtitle, columns, rows };
  const showAllLink = rows.length > limit
    ? `<a class="show-all" onclick="showAllReport(${JSON.stringify(cardKey).replace(/"/g, '&quot;')})">Xem tất cả ${rows.length} →</a>`
    : '';
  return `
    <div class="report-card">
      <h3 class="report-title">${esc(title)}</h3>
      <div class="report-sub muted">${esc(subtitle)}</div>
      ${_reportCardBody(columns, preview)}
      ${showAllLink}
    </div>`;
}

// Expand a card: drive via hash so the browser back button + "Quay lại" both work.
function showAllReport(key){
  window._lastTab = 'report';
  location.hash = '#report/' + encodeURIComponent(key);
}

function renderReportDetail(key){
  const d = REPORT_FULL_DATA[key];
  if (!d) { showDetail(`${backLinkToTab('report')}<div class="empty">Không tìm thấy data.</div>`); return; }
  const html = `
    ${backLinkToTab('report')}
    <div class="detail-hero">
      <h2>${esc(d.title)}</h2>
      <p class="sub">${esc(d.subtitle)} — ${d.rows.length} hàng</p>
    </div>
    <div class="report-card" style="max-width:none">
      ${_reportCardBody(d.columns, d.rows)}
    </div>`;
  showDetail(html);
}

function backLinkToTab(tab){
  return `<a class="back-link" onclick="window._lastTab='${tab}'; location.hash=''; return false" href="#">← Quay lại</a>`;
}

function fmtCompact(v){
  if (!v || isNaN(v)) return '$0';
  const n = Math.abs(v);
  if (n >= 1e9) return '$' + (v/1e9).toFixed(1) + 'B';
  if (n >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + Math.round(v/1e3) + 'K';
  return '$' + Math.round(v);
}

function artistTag(artist_id, name){
  return `<a class="artist-tag" onclick="location.hash='#artist/${artist_id}'">${esc(name)}</a>`;
}
function cityTag(name){
  return `<span class="city-tag">${esc(name)}</span>`;
}
function sourceBadge(src){
  const label = ({ bonhams:"Bonhams", christies:"Christie's", sothebys:"Sotheby's",
                   millon:"Millon", aguttes:"Aguttes",
                   "global-auction":"Global Auction",
                   invaluable:"Invaluable (platform)"})[src] || src;
  return `<span class="src-badge src-${esc(src)}">${esc(label)}</span>`;
}

function renderReport(){
  const r = data.report || {};
  const host = document.querySelector('#report');
  if (!host) return;

  // Column presets
  const COL_ARTIST  = { label:'Nghệ sĩ', cls:'col-artist trunc' };
  const COL_TITLE   = { label:'Tác phẩm', cls:'col-title trunc primary' };
  const COL_DATE    = { label:'Ngày', cls:'col-date num' };
  const COL_USD     = { label:'Giá USD', cls:'col-num num' };
  const COL_TOTAL   = { label:'Tổng', cls:'col-num num' };
  const COL_AVG     = { label:'Giá TB', cls:'col-num num' };
  const COL_LOT     = { label:'Lot', cls:'col-num num' };
  const COL_CITY    = { label:'Thành phố', cls:'col-artist trunc' };
  const COL_SRC     = { label:'Nguồn', cls:'col-src' };
  const COL_SESS    = { label:'Phiên', cls:'col-session trunc' };

  // 1) Highest revenue (renamed from "bán chạy nhất" to be precise about $-volume)
  const card1 = reportCard('Nghệ sĩ doanh số cao nhất', '(24 tháng — tổng giá đã bao gồm phí mua, USD)',
    [COL_ARTIST, COL_TOTAL, COL_AVG, COL_LOT],
    (r.highest_sellers_24m || []).map(x => ({ cells: [
      { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
      { text: fmtCompact(x.total_usd) },
      { text: fmtCompact(x.avg_usd) },
      { text: x.lot_count },
    ] })),
    { key: 'highest_revenue' });

  // 1b) Most lots sold
  const card1b = reportCard('Nghệ sĩ với số lượng tác phẩm bán nhiều nhất', '(24 tháng — xếp theo số lot)',
    [COL_ARTIST, COL_LOT, COL_TOTAL, COL_AVG],
    (r.most_lots_24m || []).map(x => ({ cells: [
      { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
      { text: x.lot_count },
      { text: fmtCompact(x.total_usd) },
      { text: fmtCompact(x.avg_usd) },
    ] })),
    { key: 'most_lots' });

  // 1c) Highest average price — who's the most "valuable" artist per lot
  const card1c = reportCard('Nghệ sĩ doanh số trung bình cao nhất', '(24 tháng — giá trung bình mỗi lot, lọc ≥3 lot)',
    [COL_ARTIST, COL_AVG, COL_LOT, COL_TOTAL],
    (r.highest_avg_24m || []).map(x => ({ cells: [
      { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
      { text: fmtCompact(x.avg_usd) },
      { text: x.lot_count },
      { text: fmtCompact(x.total_usd) },
    ] })),
    { key: 'highest_avg' });

  // 2) Record prices
  const COL_TITLE_WIDE = { label:'Tác phẩm', cls:'col-title-wide trunc primary' };
  const card2 = reportCard('Kỷ lục giá mới', '(Lot lập đỉnh mọi thời đại, 12 tháng qua)',
    [COL_ARTIST, COL_TITLE_WIDE, COL_USD],
    (r.record_prices || []).map(x => ({
      cells: [
        { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
        { text: x.artwork_title || '—', tip: (x.sale_date ? x.sale_date + ' · ' : '') + (x.artwork_title || '') },
        { text: fmtCompact(x.price_usd) },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'record_prices' });

  // 3) Top selling lots 12m — no title column, more room for artist name + tooltip shows title
  const COL_ARTIST_WIDE = { label:'Nghệ sĩ', cls:'col-artist-wide trunc' };
  const card3 = reportCard('Lot bán cao nhất', '(12 tháng gần đây, hover để xem tên tác phẩm)',
    [COL_ARTIST_WIDE, COL_SRC, COL_USD],
    (r.top_lots_12m || []).map(x => ({
      cells: [
        { html: artistTag(x.artist_id, x.artist_name), tip: x.artwork_title ? `${x.artist_name} — ${x.artwork_title}` : x.artist_name },
        { html: sourceBadge(x.source) },
        { text: fmtCompact(x.price_usd) },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'top_lots' });

  // 4) Top sessions 12m
  const card4 = reportCard('Phiên đấu giá đáng chú ý', '(12 tháng, xếp theo tổng USD)',
    [COL_SESS, COL_LOT, COL_TOTAL],
    (r.top_sessions_12m || []).map(x => {
      const house = (x.auction_title || '').split('—')[0].trim() || x.source;
      const dept = (x.auction_title || '').split('—').slice(1).join('—').trim();
      const sub = [x.sale_location, (x.sale_date || '').slice(0, 10)].filter(Boolean).join(' · ');
      return {
        cells: [
          { html: `<strong>${esc(house)}</strong>${dept ? ` <span class="muted">${esc(dept.slice(0, 36))}</span>` : ''}<br><span class="muted" style="font-size:11px">${esc(sub)}</span>`,
            tip: x.auction_title || '' },
          { text: x.lot_count },
          { text: fmtCompact(x.total_usd) },
        ],
        action: x.sale_page_url ? { href: x.sale_page_url } : null,
      };
    }),
    { key: 'top_sessions' });

  // 5) By location 12m
  const card5 = reportCard('Doanh số theo thành phố', '(12 tháng)',
    [COL_CITY, COL_TOTAL, COL_AVG, COL_LOT],
    (r.by_location_12m || []).map(x => ({ cells: [
      { html: cityTag(x.location), tip: x.location },
      { text: fmtCompact(x.total_usd) },
      { text: fmtCompact(x.avg_usd) },
      { text: x.lot_count },
    ] })),
    { key: 'by_location' });

  // 6) Upcoming lots
  const card6 = reportCard('Lot sắp đấu giá', '(Xếp theo ước tính thấp)',
    [COL_ARTIST, COL_TITLE, COL_DATE, { label:'Estimate', cls:'col-num num' }],
    (r.upcoming_lots || []).map(x => ({
      cells: [
        { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
        { text: x.artwork_title || '—', tip: x.artwork_title || '' },
        { text: (x.sale_date || '').slice(5) },
        { text: fmtCompact(x.estimate_low) + (x.estimate_high ? '–' + fmtCompact(x.estimate_high) : '') },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'upcoming_lots' });

  // 7) Upcoming sessions
  const card7 = reportCard('Phiên đấu giá sắp tới', '(Tháng tới)',
    [COL_SESS, COL_LOT, { label:'Tổng estimate', cls:'col-num num' }],
    (r.upcoming_sessions || []).map(x => {
      const house = (x.auction_title || '').split('—')[0].trim() || x.source;
      const dept = (x.auction_title || '').split('—').slice(1).join('—').trim();
      const sub = [x.sale_location, (x.sale_date || '').slice(0, 10)].filter(Boolean).join(' · ');
      return {
        cells: [
          { html: `<strong>${esc(house)}</strong>${dept ? ` <span class="muted">${esc(dept.slice(0, 36))}</span>` : ''}<br><span class="muted" style="font-size:11px">${esc(sub)}</span>`,
            tip: x.auction_title || '' },
          { text: x.lot_count },
          { text: fmtCompact(x.est_low_total) + '–' + fmtCompact(x.est_high_total) },
        ],
        action: x.sale_page_url ? { href: x.sale_page_url } : null,
      };
    }),
    { key: 'upcoming_sessions' });

  // 8) Most active 90d
  const card8 = reportCard('Đang hoạt động mạnh', '(90 ngày qua)',
    [COL_ARTIST, COL_LOT, { label:'Tổng USD', cls:'col-num num' }],
    (r.most_active_90d || []).map(x => ({ cells: [
      { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
      { text: x.recent_count },
      { text: fmtCompact(x.recent_usd) },
    ] })),
    { key: 'most_active' });

  // 8b) By-kind breakdown — Tranh / Điêu khắc / Đồ họa (+ future installation/performance/video)
  const KIND_LABEL = {
    painting:     'Tranh',
    sculpture:    'Điêu khắc',
    print:        'Đồ họa',
    installation: 'Sắp đặt',
    performance:  'Trình diễn',
    video:        'Video',
  };
  const cardByKind = reportCard('Phân loại tác phẩm',
    '(Tranh = sơn dầu / sơn mài / giấy / lụa. Đồ họa = lithograph / khắc gỗ. Điêu khắc tách riêng.)',
    [{label:'Loại', cls:'col-artist trunc'},
     {label:'Lot', cls:'col-num num'},
     {label:'Nghệ sĩ', cls:'col-num num'},
     {label:'Tổng USD', cls:'col-num num'}],
    (r.by_kind || []).map(x => ({ cells: [
      { text: KIND_LABEL[x.kind] || x.kind },
      { text: x.lot_count },
      { text: x.artist_count },
      { text: fmtCompact(x.total_usd) },
    ] })),
    { key: 'by_kind' });

  // 8c) Top sculptures — separate ranking since sculptures don't fit area-based ppm
  const cardSculptures = reportCard('Điêu khắc',
    '(Lot điêu khắc — sort theo giá đã bao gồm phí mua, USD)',
    [{label:'Nghệ sĩ', cls:'col-artist trunc'},
     {label:'Tác phẩm', cls:'col-title-wide trunc'},
     {label:'H. (cm)', cls:'col-num num'},
     COL_USD],
    (r.top_sculptures || []).map(x => ({
      cells: [
        { html: x.artist_id ? artistTag(x.artist_id, x.artist_name) : esc(x.artist_name || '?') },
        { text: x.artwork_title || '—', tip: x.artwork_title || '' },
        { text: x.height_cm ? Math.round(x.height_cm) : '—' },
        { text: fmtCompact(x.price_usd) },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'top_sculptures' });

  // 8d) Momentum — average % overshoot of midpoint estimate per artist.
  // Hot signal: lots routinely beat high estimate (>0% overshoot). Cooling: <0%.
  function fmtPct(v){
    if (v == null) return '—';
    const sign = v > 0 ? '+' : '';
    // Cap absurd values for display (some Vietnamese house under-estimates → 14000% overshoot)
    if (Math.abs(v) >= 1000) return `${sign}${(v/100).toFixed(0)}×`;
    return `${sign}${v.toFixed(1)}%`;
  }
  function pctClass(v){
    if (v == null) return '';
    if (v > 30) return 'pct-hot';
    if (v < -10) return 'pct-cold';
    return '';
  }
  const cardMomentum = reportCard('Momentum nghệ sĩ',
    '(Trung bình giá búa vượt mốc giữa estimate. >+30% = nóng, <-10% = nguội. Lọc ≥3 lot.)',
    [{label:'Nghệ sĩ', cls:'col-artist trunc'},
     {label:'Lot', cls:'col-num num'},
     {label:'Vượt mốc giữa', cls:'col-num num'},
     {label:'>High', cls:'col-num num'},
     {label:'<Mid', cls:'col-num num'}],
    (r.momentum_artists || []).map(x => ({
      cells: [
        { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
        { text: x.n },
        { html: `<span class="${pctClass(x.avg_overshoot_pct)}">${fmtPct(x.avg_overshoot_pct)}</span>` },
        { text: `${x.pct_over_high.toFixed(0)}%` },
        { text: `${x.pct_under_mid.toFixed(0)}%` },
      ],
    })),
    { key: 'momentum_artists' });

  // 8f) Bargain finds — lots that sold ≤50% of the artist's own median $/m².
  // Useful for collectors hunting under-priced opportunities. Filters: artist
  // has ≥5 sold lots (so median is meaningful), lot ≥$5K (skip decorative).
  const cardBargains = reportCard('Bargain finds',
    '(Lot bán ≤50% giá $/m² trung vị của nghệ sĩ — cơ hội mua dưới giá market)',
    [{label:'Nghệ sĩ', cls:'col-artist trunc'},
     {label:'Tác phẩm', cls:'col-title-wide trunc'},
     COL_USD,
     {label:'$/m²', cls:'col-num num'},
     {label:'vs median', cls:'col-num num'}],
    (r.bargain_lots || []).map(x => ({
      cells: [
        { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
        { text: x.artwork_title || '—', tip: (x.dimensions || '') + ' · ' + (x.medium || '') },
        { text: fmtCompact(x.price_usd) },
        { text: '$' + fmtCompact(x.price_per_m2_usd).slice(1) },
        { html: `<span class="pct-cold">${x.dev_pct}%</span>` },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'bargain_lots' });

  // 8g) Premium prices — lots sold ≥200% of the artist's median (heating signal)
  const cardPremiums = reportCard('Giá premium bất thường',
    '(Lot bán ≥2× $/m² trung vị của nghệ sĩ — masterpiece hoặc demand spike)',
    [{label:'Nghệ sĩ', cls:'col-artist trunc'},
     {label:'Tác phẩm', cls:'col-title-wide trunc'},
     COL_USD,
     {label:'$/m²', cls:'col-num num'},
     {label:'vs median', cls:'col-num num'}],
    (r.premium_lots || []).map(x => ({
      cells: [
        { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
        { text: x.artwork_title || '—', tip: (x.dimensions || '') + ' · ' + (x.medium || '') },
        { text: fmtCompact(x.price_usd) },
        { text: '$' + fmtCompact(x.price_per_m2_usd).slice(1) },
        { html: `<span class="pct-hot">+${x.dev_pct}%</span>` },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'premium_lots' });

  // 8h) Suspicious / potentially-forged lots — Indochine masters at <30% median $/m²
  // OR lots with attribution caveats (attribué/école de/manner of/studio of).
  const cardSuspicious = reportCard('🚨 Lot nghi vấn — kiểm tra thủ công',
    '(Tranh giả Đông Dương phổ biến trên thị trường quốc tế. Cờ đỏ: ' +
    'Indochine masters bán <30% trung vị $/m² của họ THEO ĐÚNG SUPPORT (canvas/lụa/giấy/sơn mài), ' +
    'hoặc title ghi "attribué/école de/manner of/studio of". Lọc 2018+ để loại sales era cũ. ' +
    'Đây CHỈ là tín hiệu — cần kiểm chứng provenance/chữ ký/vật liệu trước khi kết luận.)',
    [{label:'Nghệ sĩ', cls:'sus-col-artist'},
     {label:'Tác phẩm · Nguồn', cls:'sus-col-title'},
     {label:'Support', cls:'sus-col-narrow'},
     {label:'Kích thước', cls:'sus-col-dim'},
     {label:'Ngày bán', cls:'sus-col-date'},
     {label:'Estimate', cls:'sus-col-num num'},
     {label:'Giá USD', cls:'sus-col-num num'},
     {label:'% median', cls:'sus-col-narrow num'},
     {label:'Cờ đỏ', cls:'sus-col-flag'}],
    (r.suspicious_lots || []).map(x => {
      const flagBadge = x.flag_reason === 'price_anomaly'
        ? `<span class="pct-cold">≤30%</span>`
        : `<span style="color:#dc2626;font-weight:600;font-size:11px">${esc(x.flag_reason)}</span>`;
      const estTxt = (x.estimate_low || x.estimate_high)
        ? `${money(Math.round(x.estimate_low || 0))}–${money(Math.round(x.estimate_high || 0))}`
        : '—';
      const supportLabel = x.support_type ? (SUPPORT_LABEL_VN[x.support_type] || x.support_type) : '—';
      return {
        cells: [
          { html: artistTag(x.artist_id, x.artist_name) },
          { html: `${esc(x.artwork_title || '—')} <span class="tag source sm" style="margin-left:6px">${esc(x.source)}</span>` },
          { text: supportLabel },
          { text: x.dimensions || '—' },
          { text: x.sale_date || '—' },
          { text: estTxt },
          { text: fmtCompact(x.price_usd) },
          { html: `<span class="pct-cold">${x.pct_of_median}%</span>` },
          { html: flagBadge },
        ],
        action: x.source_url ? { href: x.source_url } : null,
      };
    }),
    { key: 'suspicious_lots', preview: 30 });

  // 8i) Yearly median chart — multi-line, log Y. Shows price progression for top 6 masters.
  function _yearlyMedianCard(){
    const dataPts = r.yearly_median_per_artist || [];
    if (dataPts.length === 0) return '';
    const byArtist = {};
    dataPts.forEach(d => {
      (byArtist[d.artist_name] = byArtist[d.artist_name] || []).push({
        yr: d.yr, median_ppm: +d.median_ppm, n: d.n
      });
    });
    Object.values(byArtist).forEach(arr => arr.sort((a, b) => a.yr - b.yr));
    const chart = buildYearlyMedianChart(byArtist, { width: 380, height: 200, isPreview: true });
    if (!chart) return '';
    const legend = Object.keys(byArtist).sort().map(name =>
      `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:10px;font-size:11px;color:#475569">
        <span style="display:inline-block;width:10px;height:2px;background:${colorForArtist(name)}"></span>${esc(name)}
      </span>`
    ).join('');
    return `
      <div class="report-card">
        <h3 class="report-title">Median $/m² qua các năm — top 6 masters</h3>
        <div class="report-sub muted">(Lê Phổ 2011 ≠ Lê Phổ 2024. Lọc năm có ≥2 lot painting.)</div>
        ${chart}
        <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">${legend}</div>
      </div>`;
  }
  const cardYearlyMedian = _yearlyMedianCard();

  // 8e) Per-medium price/m² benchmark — what does sơn mài vs sơn dầu vs lụa cost?
  // Median is the headline number; mean shown alongside for context.
  const cardMediumBenchmark = reportCard('Giá theo chất liệu',
    '(Median $/m² là benchmark — mean bị skew bởi masterpiece. Lọc chất liệu có ≥10 lot.)',
    [{label:'Chất liệu', cls:'col-artist trunc'},
     {label:'Lot', cls:'col-num num'},
     {label:'Median $/m²', cls:'col-num num'},
     {label:'Mean $/m²', cls:'col-num num'},
     {label:'Range', cls:'col-num num'},
     {label:'Avg dt (m²)', cls:'col-num num'}],
    (r.medium_benchmark || []).map(x => ({
      cells: [
        { text: x.paint_medium },
        { text: x.n },
        { text: '$' + fmtCompact(x.median_ppm).slice(1) },  // strip leading $ then re-add (compact)
        { text: '$' + fmtCompact(x.avg_ppm).slice(1) },
        { text: fmtCompact(x.min_ppm) + '–' + fmtCompact(x.max_ppm) },
        { text: x.avg_area_m2.toFixed(2) },
      ],
    })),
    { key: 'medium_benchmark' });

  // 9) Coverage per source — Năm (count) column dropped per UX feedback
  const card9 = reportCard('Coverage theo nhà đấu giá', '(Số lot, họa sĩ, phạm vi năm đã crawl)',
    [{label:'Nhà đấu giá', cls:'col-artist trunc'},
     {label:'Lot', cls:'col-num num'},
     {label:'Nghệ sĩ', cls:'col-num num'},
     {label:'Phạm vi', cls:'col-date num'}],
    (r.coverage || []).map(x => ({ cells: [
      { html: sourceBadge(x.source) },
      { text: x.lot_count },
      { text: x.artist_count },
      { text: (x.year_min || '?') + '–' + (x.year_max || '?') },
    ] })),
    { key: 'coverage' });

  // 10a) Lots thiếu ngày — per-lot list for direct investigation
  const cardMissingDate = reportCard('Lots thiếu ngày',
    '(Click VIEW để mở trang nguồn và bổ sung ngày đấu)',
    [{label:'Nguồn', cls:'col-src'},
     {label:'Nghệ sĩ', cls:'col-artist trunc'},
     {label:'Tác phẩm', cls:'col-title-wide trunc'},
     COL_USD],
    (r.lots_missing_date || []).map(x => ({
      cells: [
        { html: sourceBadge(x.source) },
        { html: x.artist_id ? artistTag(x.artist_id, x.artist_name || x.artist_name_raw) : esc(x.artist_name_raw || '?') },
        { text: x.artwork_title || '—', tip: x.artwork_title || '' },
        { text: fmtCompact(x.price_usd) },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'lots_missing_date' });

  // 10b) Lots thiếu kích thước — per-lot list
  const cardMissingDim = reportCard('Lots thiếu kích thước',
    '(Click VIEW để mở trang nguồn và bổ sung kích thước)',
    [{label:'Nguồn', cls:'col-src'},
     {label:'Nghệ sĩ', cls:'col-artist trunc'},
     {label:'Tác phẩm', cls:'col-title-wide trunc'},
     COL_USD],
    (r.lots_missing_dim || []).map(x => ({
      cells: [
        { html: sourceBadge(x.source) },
        { html: x.artist_id ? artistTag(x.artist_id, x.artist_name || x.artist_name_raw) : esc(x.artist_name_raw || '?') },
        { text: x.artwork_title || '—', tip: x.artwork_title || '' },
        { text: fmtCompact(x.price_usd) },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'lots_missing_dim' });

  // 11) Empty-title lots — drop "Ngày" column (per UX feedback), keep VIEW link
  const cardEmptyTitles = reportCard('Lots không có tên tác phẩm',
    '(Click VIEW để mở trang nguồn — bạn có thể manually report đúng tên)',
    [{label:'Nguồn', cls:'col-src'},
     {label:'Nghệ sĩ', cls:'col-artist trunc'}],
    (r.empty_titles || []).map(x => ({
      cells: [
        { html: sourceBadge(x.source) },
        { html: x.artist_id ? artistTag(x.artist_id, x.artist_name || x.artist_name_raw) : esc(x.artist_name_raw || '?') },
      ],
      action: x.source_url ? { href: x.source_url } : null,
    })),
    { key: 'empty_titles' });

  // 12) Artists missing birth/death years
  const cardArtistGaps = reportCard('Nghệ sĩ chưa có năm sinh',
    '(Có lot đấu giá nhưng birth_year/death_year đều null)',
    [{label:'Nghệ sĩ', cls:'col-artist-wide trunc'},
     {label:'Lot', cls:'col-num num'}],
    (r.artist_gaps || []).map(x => ({ cells: [
      { html: artistTag(x.artist_id, x.artist_name), tip: x.artist_name },
      { text: x.auction_count || 0 },
    ] })),
    { key: 'artist_gaps' });

  // Reordered per user spec: average-price card before lot-count card.
  host.innerHTML = `
    <div class="report-hero">
      <h2>Artonis Data Report</h2>
      <p class="muted">Tổng hợp thị trường — cập nhật ${(r.generated_at || '').slice(0, 10)}</p>
    </div>
    <div class="report-grid">
      ${card1}${card1c}${card1b}
      ${card2}${card3}${card8}
      ${cardMomentum}${cardMediumBenchmark}
      ${cardPremiums}${cardBargains}
      ${card4}${card9}${card5}
      ${card7}${card6}
      ${cardYearlyMedian}
      ${cardByKind}${cardSculptures}
    </div>

    <h2 class="section-title" style="margin-top:32px">🚨 Lot nghi vấn — kiểm tra thủ công</h2>
    <div class="report-block-fullwidth">
      ${cardSuspicious}
    </div>

    <h2 class="section-title" style="margin-top:32px">Dữ liệu chưa đầy đủ</h2>
    <div class="report-grid">
      ${cardMissingDate}${cardMissingDim}${cardEmptyTitles}${cardArtistGaps}
    </div>`;
}

function render(){
  renderStats(); renderArtists(); renderExhibitions(); renderSales(); renderAuctionHouses(); renderGalleries(); renderReport(); renderFiles();
  handleRoute();
}

// ============ ROUTING + DETAIL PAGES ============

function showListView(tabId){
  // Show tabs + the selected list section
  document.getElementById('tabs-wrap').style.display = '';
  document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
  document.getElementById(tabId || 'artists').classList.add('active');
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === (tabId || 'artists')));
}

function showDetail(html){
  document.getElementById('tabs-wrap').style.display = 'none';
  document.querySelectorAll('section').forEach(s => s.classList.remove('active'));
  const d = document.getElementById('detail-page');
  d.classList.add('active');
  d.innerHTML = html;
  window.scrollTo(0, 0);
}

function backLink(tabName){
  return `<a class="back-link" onclick="location.hash=''; return false" href="#">← Quay lại</a>`;
}

function renderArtistDetail(id){
  const a = data.artists.find(x => x.id == id);
  if (!a) { showDetail(`${backLink()}<div class="empty">Không tìm thấy họa sĩ.</div>`); return; }
  const display = a.display_name || a.name;
  const years = fmtYears(a.birth_year, a.death_year, display);

  // Group observations by exhibition_title
  const obs = (data.observations || []).filter(o => o.artist_name === a.name || o.artist_name === a.display_name);
  const byExh = {};
  obs.forEach(o => {
    const key = o.exhibition_title || 'Không rõ triển lãm';
    if (!byExh[key]) byExh[key] = [];
    byExh[key].push(o);
  });

  // Auction sales of this artist
  const sales = (data.sales || []).filter(s => s.artist_id == a.id || s.artist_name === display);
  sales.sort((x,y) => (y.sale_date || '').localeCompare(x.sale_date || ''));

  // Build exhibitions section
  const exhSections = Object.entries(byExh).map(([exh, list]) => {
    const rows = list.map(p => `
      <tr>
        <td>${esc(p.artwork_title || '—')}</td>
        <td>${esc(p.medium || '')}</td>
        <td>${esc(p.dimensions || '')}${p.area_m2 ? `<br><span class="muted">${p.area_m2} m²</span>` : ''}</td>
        <td class="num">${money(p.price_amount)} ${esc(p.currency)}</td>
        <td class="num">${p.price_per_m2 ? '$' + money(Math.round(p.price_per_m2)) + '/m²' : ''}</td>
        <td>${esc(p.status || '')}</td>
      </tr>`).join('');
    return `
      <h3 class="section-title">${esc(exh)} <span class="muted" style="font-family:Inter;font-weight:400;font-size:13px">(${list.length} TP)</span></h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Tác phẩm</th><th>Chất liệu</th><th>Kích thước</th><th class="num">Giá</th><th class="num">$/m²</th><th>Status</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  }).join('');

  // Render a single sales row. Sculptures don't have $/m² so the column is suppressed.
  function saleRow(s){
    const house = (s.auction_title || '').split('—')[0].trim() || s.source;
    const cur = s.currency || '';
    const medYearBits = [s.medium, s.year ? `${s.year}` : ''].filter(Boolean).join(' · ');
    const provTip = s.provenance ? ` title="Provenance: ${esc(s.provenance.slice(0, 600))}"` : '';
    const isSculpt = s.kind === 'sculpture';
    return `<tr${provTip}>
      <td>${esc(s.sale_date || '')}</td>
      <td>
        <a href="${esc(s.source_url)}" target="_blank">${esc(s.artwork_title || '(no title)')}</a>
        ${medYearBits ? `<br><span class="muted" style="font-size:12px">${esc(medYearBits)}</span>` : ''}
        ${s.provenance ? `<br><span class="muted" style="font-size:11px">📜 có provenance</span>` : ''}
      </td>
      <td>${esc(house)}</td>
      <td>${esc(s.dimensions || '')}${(!isSculpt && s.area_m2) ? `<br><span class="muted">${s.area_m2} m²</span>` : ''}</td>
      <td class="num">${money(Math.round(s.hammer_price || 0))} ${esc(cur)}<br><span class="muted">$${money(Math.round(s.price_usd || 0))}</span>${s.price_with_premium_usd ? `<br><span class="muted" style="font-size:11px">+phí: $${money(Math.round(s.price_with_premium_usd))}</span>` : ''}</td>
      <td class="num">${(!isSculpt && s.price_per_m2_usd) ? '$' + money(Math.round(s.price_per_m2_usd)) : (isSculpt ? '<span class="muted">—</span>' : '')}</td>
    </tr>`;
  }
  function salesTable(list){
    return `<div class="table-wrap"><table>
      <thead><tr><th>Ngày</th><th>Tác phẩm</th><th>Nhà đấu giá</th><th>KT</th><th class="num">Giá gõ búa / Giá thực</th><th class="num">$/m²</th></tr></thead>
      <tbody>${list.map(saleRow).join('')}</tbody>
    </table></div>`;
  }
  // Split into paintings (kind != sculpture) vs sculptures so users can see which side
  // of the artist's practice each lot belongs to. Keeps a single combined table when
  // there are no sculptures (the common case).
  const paintings   = sales.filter(s => s.kind !== 'sculpture');
  const sculptures  = sales.filter(s => s.kind === 'sculpture');
  let salesSection = '';
  if (sales.length) {
    if (sculptures.length && paintings.length) {
      salesSection = `
        <h3 class="section-title">Tranh <span class="muted" style="font-family:Inter;font-weight:400;font-size:13px">(${paintings.length} lot)</span></h3>
        ${salesTable(paintings)}
        <h3 class="section-title">Điêu khắc <span class="muted" style="font-family:Inter;font-weight:400;font-size:13px">(${sculptures.length} lot)</span></h3>
        ${salesTable(sculptures)}`;
    } else {
      const title = sculptures.length ? 'Điêu khắc' : 'Lịch sử đấu giá';
      salesSection = `
        <h3 class="section-title">${title} <span class="muted" style="font-family:Inter;font-weight:400;font-size:13px">(${sales.length} lot)</span></h3>
        ${salesTable(sales)}`;
    }
  }

  const roles = artistRoles(a);
  const html = `
    ${backLink()}
    <div class="detail-hero">
      <h2>${esc(display)} ${rolePills(roles)}</h2>
      <p class="sub">Năm sinh – mất: ${years}</p>
      <div class="detail-kpis">
        <div class="kpi"><b>${numOrNa(a.auction_count)}</b><span>Lots đấu giá</span></div>
        <div class="kpi"><b>${numOrNa(a.price_count)}</b><span>TP từ gallery</span></div>
        <div class="kpi"><b>${moneyOrNa(a.overall_min_usd)}</b><span>Min $</span></div>
        <div class="kpi"><b>${moneyOrNa(a.overall_avg_usd)}</b><span>Avg $</span></div>
        <div class="kpi"><b style="color:var(--green)">${moneyOrNa(a.overall_max_usd)}</b><span>Max $</span></div>
        <div class="kpi"><b>${moneyOrNa(a.overall_median_per_m2_usd)}</b><span>Median $/m²</span></div>
      </div>
    </div>
    ${priceHistoryChart(a)}
    ${exhSections}
    ${salesSection}
  `;
  showDetail(html);
}

function renderExhibitionDetail(id){
  const e = data.exhibitions.find(x => x.id == id);
  if (!e) { showDetail(`${backLink()}<div class="empty">Không tìm thấy triển lãm.</div>`); return; }
  const obs = (data.observations || []).filter(o => o.exhibition_title === e.title);

  const rows = obs.map(p => `
    <tr>
      <td><strong>${esc(p.artist_name || '')}</strong></td>
      <td>${esc(p.artwork_title || '—')}</td>
      <td>${esc(p.medium || '')}</td>
      <td>${esc(p.dimensions || '')}${p.area_m2 ? `<br><span class="muted">${p.area_m2} m²</span>` : ''}</td>
      <td class="num">${money(p.price_amount)} ${esc(p.currency)}</td>
      <td class="num">${p.price_per_m2 ? '$' + money(Math.round(p.price_per_m2)) + '/m²' : ''}</td>
    </tr>`).join('');

  const venues = e.venue_segments || [];
  const venueHtml = venues.length ? venues.map(v => `
    <div style="margin-top:8px">
      <strong>Phần ${v.part}:</strong> ${esc(v.start_date || '')}${v.end_date ? ' → ' + esc(v.end_date) : ''}
      <span class="muted"> · ${esc(v.venue)}</span>
    </div>
  `).join('') : `<p class="sub"><strong>Địa điểm:</strong> ${esc(e.venue || e.organizer || e.city || 'N/A')}<br>
    <strong>Ngày:</strong> ${esc(e.start_date || e.date_token || 'N/A')}</p>`;

  const html = `
    ${backLink()}
    <div class="detail-hero">
      <h2>${esc(e.title)}</h2>
      <p class="sub"><strong>Nghệ sĩ:</strong> ${esc((e.artists_text || '').replace(/\n/g, ', ')) || 'N/A'}</p>
      ${venueHtml}
      <div class="detail-kpis">
        <div class="kpi"><b>${obs.length}</b><span>Tác phẩm có giá</span></div>
        <div class="kpi"><b>${e.file_count || 0}</b><span>Files</span></div>
        <div class="kpi"><b>${e.catalogue_file_count || 0}</b><span>Catalogue</span></div>
      </div>
    </div>
    ${obs.length ? `
      <h3 class="section-title">Danh sách tác phẩm</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Nghệ sĩ</th><th>Tác phẩm</th><th>Chất liệu</th><th>Kích thước</th><th class="num">Giá</th><th class="num">$/m²</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : '<div class="empty">Chưa có dữ liệu giá từ triển lãm này.</div>'}
  `;
  showDetail(html);
}

function renderGalleryDetail(name){
  const g = (data.galleries || []).find(x => x.gallery === name);
  if (!g) { showDetail(`${backLink()}<div class="empty">Không tìm thấy gallery.</div>`); return; }
  const rows = (g.exhibitions || []).map(e => `
    <tr style="cursor:pointer" onclick="location.hash='#exhibition/${e.id}'">
      <td>${esc(e.start_date || '')}</td>
      <td><strong class="row-link">${esc(e.title || '')}</strong></td>
      <td>${esc((e.artists_display || e.artists_text || '').replace(/\n/g, ', '))}</td>
    </tr>`).join('');
  const kind = galleryKind(name);
  const kindLabel = kind === 'venue' ? '🏛️ Bảo tàng / Hội mỹ thuật' : (kind === 'gallery' ? '🎨 Gallery' : '📍 Nơi khác');

  const html = `
    ${backLink()}
    <div class="detail-hero">
      <h2>${esc(name)}</h2>
      <p class="sub">${kindLabel} · Hoạt động: ${g.first_date || '?'} → ${g.last_date || '?'}</p>
      <div class="detail-kpis">
        <div class="kpi"><b>${g.exhibition_count}</b><span>Triển lãm</span></div>
        <div class="kpi"><b>${g.artist_count || 0}</b><span>Nghệ sĩ</span></div>
      </div>
    </div>
    <h3 class="section-title">Danh sách triển lãm</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>Ngày</th><th>Triển lãm</th><th>Họa sĩ</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  `;
  showDetail(html);
}

// Pagination helper: given rows + page, return paginated HTML + nav
function paginate(rows, pageSize, pageKey){
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const page = parseInt(params.get(pageKey) || '1', 10);
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const p = Math.max(1, Math.min(page, totalPages));
  const slice = rows.slice((p-1)*pageSize, p*pageSize);

  const buildUrl = (np) => {
    const newParams = new URLSearchParams(params);
    newParams.set(pageKey, np);
    const base = location.hash.split('?')[0];
    return `${base}?${newParams.toString()}`;
  };
  const nav = totalPages <= 1 ? '' : `
    <div style="display:flex; gap:6px; align-items:center; margin:16px 0; justify-content:center;">
      ${p > 1 ? `<a class="back-link" style="margin:0" href="${buildUrl(p-1)}">← Trang trước</a>` : ''}
      <span class="muted" style="padding:0 10px">Trang ${p}/${totalPages} · ${total} lots</span>
      ${p < totalPages ? `<a class="back-link" style="margin:0" href="${buildUrl(p+1)}">Trang sau →</a>` : ''}
    </div>`;
  return { slice, nav, page: p, totalPages, total };
}

function saleUrlFor(lot){
  // Prefer stored sale_page_url; fallback to deriving from lot URL
  if (!lot) return null;
  if (lot.sale_page_url) return lot.sale_page_url;
  const source = lot.source;
  const lotUrl = lot.source_url;
  if (!lotUrl) return null;
  try {
    if (source === 'bonhams') {
      const m = lotUrl.match(/^(https?:\/\/[^\/]+\/auction\/\d+)\//);
      return m ? m[1] + '/' : null;
    }
    if (source === 'millon') {
      // millon.com FR → /catalogue/…/resultat; millon-vietnam.com → homepage (no per-sale page)
      if (lotUrl.includes('millon-vietnam.com')) return 'https://millon-vietnam.com/';
      const m = lotUrl.match(/^(https?:\/\/[^\/]+\/catalogue\/vente\d+-[^\/]+)\//);
      return m ? m[1] + '/resultat' : null;
    }
    if (source === 'invaluable') {
      const m = lotUrl.match(/^(https?:\/\/[^\/]+)/);
      return m ? m[1] : null;
    }
    if (source === 'sothebys') {
      const m = lotUrl.match(/^(https?:\/\/[^\/]+\/[^\/]+\/buy\/auction\/\d{4}\/[^\/]+)/);
      return m ? m[1] : null;
    }
    if (source === 'christies') {
      const m = lotUrl.match(/^(https?:\/\/[^\/]+\/[^\/]+\/auction\/[^\/]+)/);
      return m ? m[1] : null;
    }
  } catch (e) {}
  return null;
}

function sessionKeyFor(sale){
  // Group lots into sessions: same source + same sale_date + same auction_title
  const dept = (sale.auction_title || '').split('—')[1]?.trim() || '';
  return `${sale.sale_date || 'unknown'}|${dept || sale.auction_title || ''}`;
}

function renderAuctionHouseDetail(source){
  const h = (data.auction_houses || []).find(x => x.source === source);
  if (!h) { showDetail(`${backLink()}<div class="empty">Không tìm thấy nhà đấu giá.</div>`); return; }
  const lots = (data.sales || []).filter(s => s.source === source);

  // Group by session
  const sessions = {};
  lots.forEach(s => {
    const k = sessionKeyFor(s);
    if (!sessions[k]) {
      sessions[k] = {
        key: k,
        sale_date: s.sale_date || '',
        auction_title: s.auction_title || '',
        location: s.sale_location || '',
        lots: [],
      };
    }
    sessions[k].lots.push(s);
  });
  const sessionList = Object.values(sessions).sort((a,b) => (b.sale_date || '').localeCompare(a.sale_date || ''));
  const pag = paginate(sessionList, 20, 'sp');

  const sessionRows = pag.slice.map((sess, idx) => {
    const n = sess.lots.length;
    const sum = sess.lots.reduce((acc, l) => acc + (l.price_usd || 0), 0);
    const mx = sess.lots.reduce((acc, l) => Math.max(acc, l.price_usd || 0), 0);
    const dept = (sess.auction_title || '').split('—')[1]?.trim() || sess.auction_title || '';
    // Icon link to original sale page (prefer stored sale_page_url)
    const firstLot = sess.lots[0] || {};
    const saleUrl = saleUrlFor(firstLot);
    // Separate the icon into its own cell to avoid conflict with row click
    const iconCell = saleUrl
      ? `<a href="${esc(saleUrl)}" target="_blank" title="Mở trang phiên gốc" onclick="event.stopPropagation();" style="display:inline-block; padding:6px 10px; color:var(--blue); text-decoration:none; font-size:18px; line-height:1; border-radius:4px;" onmouseover="this.style.background='#eee'" onmouseout="this.style.background=''">↗</a>`
      : `<span style="display:inline-block; padding:6px 10px; opacity:0.2">↗</span>`;
    return `<tr>
      <td style="width:60px; padding:6px 4px">${iconCell}</td>
      <td style="cursor:pointer" onclick="location.hash='#auction/${encodeURIComponent(source)}/session/${encodeURIComponent(sess.key)}'">${esc(sess.sale_date || 'N/A')}</td>
      <td style="cursor:pointer" onclick="location.hash='#auction/${encodeURIComponent(source)}/session/${encodeURIComponent(sess.key)}'"><strong class="row-link">${esc(dept || 'Session')}</strong>${sess.location ? `<br><span class="muted">${esc(sess.location)}</span>` : ''}</td>
      <td class="num" style="cursor:pointer" onclick="location.hash='#auction/${encodeURIComponent(source)}/session/${encodeURIComponent(sess.key)}'">${n}</td>
      <td class="num" style="cursor:pointer" onclick="location.hash='#auction/${encodeURIComponent(source)}/session/${encodeURIComponent(sess.key)}'">$${money(Math.round(sum))}</td>
      <td class="num" style="cursor:pointer" onclick="location.hash='#auction/${encodeURIComponent(source)}/session/${encodeURIComponent(sess.key)}'">$${money(Math.round(mx))}</td>
    </tr>`;
  }).join('');

  const html = `
    ${backLink()}
    <div class="detail-hero">
      <h2>${esc(h.display_name)}</h2>
      <p class="sub">${esc(h.country || '')} ${h.founded ? '· est. ' + h.founded : ''}</p>
      <p class="sub">${esc(h.vietnamese_art_dept || '')}</p>
      ${h.premium_note ? `<p class="sub"><strong>Buyer's premium:</strong> ${esc(h.premium_note)}</p>` : ''}
      ${h.tax_note ? `<p class="sub"><strong>Thuế:</strong> ${esc(h.tax_note)}</p>` : ''}
      <div class="detail-kpis">
        <div class="kpi"><b>${sessionList.length}</b><span>Phiên đấu giá</span></div>
        <div class="kpi"><b>${h.lot_count}</b><span>Lots tổng</span></div>
        <div class="kpi"><b>${h.artist_count}</b><span>Họa sĩ</span></div>
        <div class="kpi"><b>$${money(Math.round(h.avg_usd || 0))}</b><span>Avg hammer</span></div>
        <div class="kpi"><b>$${money(Math.round(h.max_usd || 0))}</b><span>Max hammer</span></div>
        ${h.avg_premium_pct ? `<div class="kpi"><b>${h.avg_premium_pct}%</b><span>Premium thực tế</span></div>` : ''}
      </div>
    </div>
    <h3 class="section-title">Danh sách phiên đấu giá <span class="muted" style="font-family:Inter;font-weight:400;font-size:13px">(${pag.total})</span></h3>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>Ngày</th><th>Phiên / Dept</th><th class="num">Số lots</th><th class="num">Tổng USD</th><th class="num">Max USD</th></tr></thead>
      <tbody>${sessionRows}</tbody>
    </table></div>
    ${pag.nav}
  `;
  showDetail(html);
}

function renderSessionDetail(source, sessionKey){
  const h = (data.auction_houses || []).find(x => x.source === source);
  const lots = (data.sales || []).filter(s => s.source === source && sessionKeyFor(s) === sessionKey);
  if (!lots.length) { showDetail(`${backLink()}<div class="empty">Không tìm thấy phiên.</div>`); return; }

  const sess = lots[0];
  const dept = (sess.auction_title || '').split('—')[1]?.trim() || sess.auction_title || '';
  lots.sort((a,b) => (b.price_usd || 0) - (a.price_usd || 0));
  const pag = paginate(lots, 30, 'lp');

  const rows = pag.slice.map(s => {
    const cur = s.currency || '';
    return `<tr>
      <td><span class="muted">lot ${esc(s.lot_number)}</span></td>
      <td><strong class="row-link" onclick="location.hash='#artist/${s.artist_id}'">${esc(s.artist_name || s.artist_name_raw)}</strong></td>
      <td><a href="${esc(s.source_url)}" target="_blank">${esc(s.artwork_title || '(no title)')}</a>${s.dimensions ? `<br><span class="muted">${esc(s.dimensions)}${s.area_m2 ? ' · '+s.area_m2+' m²' : ''}</span>` : ''}</td>
      <td class="num">${(s.estimate_low || s.estimate_high) ? '<span class="muted">'+money(Math.round(s.estimate_low||0))+'–'+money(Math.round(s.estimate_high||0))+' '+esc(cur)+'</span>' : ''}</td>
      <td class="num">${money(Math.round(s.hammer_price || 0))} ${esc(cur)}<br><span class="muted">$${money(Math.round(s.price_usd || 0))}</span></td>
      <td class="num">${s.price_with_premium ? money(Math.round(s.price_with_premium)) + ' '+esc(cur) : ''}${s.price_with_premium_usd ? `<br><span class="muted">$${money(Math.round(s.price_with_premium_usd))}</span>` : ''}</td>
      <td class="num">${s.price_per_m2_usd ? '$' + money(Math.round(s.price_per_m2_usd)) : ''}</td>
    </tr>`;
  }).join('');

  const total_usd = lots.reduce((a,l) => a + (l.price_usd || 0), 0);
  const backToHouse = `<a class="back-link" onclick="location.hash='#auction/${encodeURIComponent(source)}'; return false" href="#">← ${esc(h?.display_name || source)}</a>`;
  const html = `
    ${backToHouse}
    <div class="detail-hero">
      <h2>${esc(dept || 'Phiên đấu giá')}</h2>
      <p class="sub">${esc(h?.display_name || source)} · ${esc(sess.sale_date || '')} · ${esc(sess.sale_location || '')}</p>
      <div class="detail-kpis">
        <div class="kpi"><b>${lots.length}</b><span>Lots</span></div>
        <div class="kpi"><b>$${money(Math.round(total_usd))}</b><span>Tổng USD</span></div>
        <div class="kpi"><b>$${money(Math.round(lots[0].price_usd || 0))}</b><span>Max hammer</span></div>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Lot</th><th>Họa sĩ</th><th>Tác phẩm</th><th class="num">Estimate</th><th class="num">Gõ búa</th><th class="num">+Premium</th><th class="num">$/m²</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${pag.nav}
  `;
  showDetail(html);
}

function handleRoute(){
  if (!data) return;
  const hashRaw = location.hash.slice(1);
  // Strip query string
  const hash = hashRaw.split('?')[0];
  if (!hash) { showListView(window._lastTab || 'artists'); return; }
  const parts = hash.split('/');
  const [kind, ...rest] = parts;
  const id = rest.join('/');
  if (kind === 'artist') renderArtistDetail(id);
  else if (kind === 'exhibition') renderExhibitionDetail(id);
  else if (kind === 'gallery') renderGalleryDetail(decodeURIComponent(id));
  else if (kind === 'auction') {
    if (rest.length >= 3 && rest[1] === 'session') {
      renderSessionDetail(decodeURIComponent(rest[0]), decodeURIComponent(rest.slice(2).join('/')));
    } else {
      renderAuctionHouseDetail(decodeURIComponent(rest[0]));
    }
  }
  else if (kind === 'report') renderReportDetail(decodeURIComponent(id));
  else showListView('artists');
}
window.addEventListener('hashchange', handleRoute);
async function load(){
  const res = await fetch("/api/data");
  data = await res.json();
  render();
}
document.querySelector("#q").addEventListener("input", render);
document.querySelector("#filter").addEventListener("change", render);
document.querySelector("#reload").addEventListener("click", load);
document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => {
  activeTab = btn.dataset.tab;
  window._lastTab = activeTab;
  location.hash = '';
  document.getElementById('tabs-wrap').style.display = '';
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === btn));
  document.querySelectorAll("section").forEach(s => s.classList.toggle("active", s.id === activeTab));
}));
load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/data":
            conn = db()
            payload = api_payload(conn)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def serve(args):
    init_db()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving Artonis Artist Price MVP at http://{args.host}:{args.port}")
    server.serve_forever()


def download_and_import(args):
    conn = db()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / Path(args.drive_path).name
        remote = args.remote.rstrip(":") + ":" + args.drive_path.strip("/")
        run_rclone(["copyto", remote, str(local)] + rclone_drive_flags(args), timeout=240)
        if args.kind == "metadata":
            count = import_metadata_file(conn, local, args.drive_path)
        else:
            exhibition_path = args.exhibition_path.rstrip("/") + "/" if args.exhibition_path else ""
            count = import_price_file(conn, local, args.drive_path, exhibition_path)
        refresh_artist_stats(conn)
        print(f"Imported {count} rows from {args.drive_path}.")


def import_local(args):
    conn = db()
    if args.kind == "metadata":
        count = import_metadata_file(conn, args.file)
    else:
        count = import_price_file(conn, args.file, exhibition_drive_path=args.exhibition_path)
    refresh_artist_stats(conn)
    print(f"Imported {count} rows from {args.file}.")


def main():
    parser = argparse.ArgumentParser(description="Artonis artist price intelligence MVP")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan-drive", help="Scan exhibition folders and source files from Google Drive via rclone")
    scan.add_argument("--remote", default=DEFAULT_REMOTE)
    scan.add_argument("--base", default="")
    scan.add_argument("--team-drive", default="")
    scan.add_argument("--limit", type=int, default=0)
    scan.add_argument("--reset", action="store_true")
    scan.set_defaults(func=scan_drive)

    imp = sub.add_parser("import-local", help="Import local Excel/CSV metadata or price file")
    imp.add_argument("file")
    imp.add_argument("--kind", choices=("metadata", "price"), default="metadata")
    imp.add_argument("--exhibition-path", default="")
    imp.set_defaults(func=import_local)

    down = sub.add_parser("import-drive-file", help="Download and import an Excel/CSV from Drive")
    down.add_argument("drive_path")
    down.add_argument("--remote", default=DEFAULT_REMOTE)
    down.add_argument("--team-drive", default="")
    down.add_argument("--kind", choices=("metadata", "price"), default="metadata")
    down.add_argument("--exhibition-path", default="")
    down.set_defaults(func=download_and_import)

    web = sub.add_parser("serve", help="Run the local web dashboard")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.set_defaults(func=serve)

    log = sub.add_parser("crawl-log", help="Show recent crawl_runs entries")
    log.add_argument("--source", help="Filter by source (e.g. aguttes, bonhams)")
    log.add_argument("--limit", type=int, default=30, help="Max rows to show")
    log.set_defaults(func=show_crawl_log)

    args = parser.parse_args()
    args.func(args)


def show_crawl_log(args):
    conn = db()
    where = ""
    params = []
    if args.source:
        where = "where source = ?"
        params.append(args.source)
    rows = conn.execute(
        f"""select source, target_slug, started_at, lots_scanned, lots_inserted,
                  sale_date_min, sale_date_max, status, note
             from crawl_runs {where}
             order by id desc
             limit ?""",
        (*params, args.limit),
    ).fetchall()
    if not rows:
        print("No crawl_runs entries.")
        return
    # Group summary
    print(f"{'Source':14s} {'Target/Slug':52s} {'Date range':24s} {'Scanned':>8s} {'Inserted':>9s} Status")
    print("-" * 130)
    for r in rows:
        rng = f"{r['sale_date_min'] or '?'}→{r['sale_date_max'] or '?'}"
        slug = (r["target_slug"] or "")[:50]
        print(f"{r['source']:14s} {slug:52s} {rng:24s} {r['lots_scanned'] or 0:>8d} {r['lots_inserted'] or 0:>9d} {r['status']}")
        if r["note"] and r["status"] == "error":
            print(f"  err: {r['note'][:120]}")


if __name__ == "__main__":
    main()
