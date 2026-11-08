#!/usr/bin/env python3
"""Subplot: build library.json and coverage.json from Plex's nightly database copy (<prefix>-scan.service; no network, no
secrets, no media access). ARCHITECTURE.md "scan.py". The live Plex database is never opened: the backup script's .nightly copy is
copied into state first. Libraries named in rules.exclude_libraries or common.EXCLUDE_LIBRARIES are excluded in SQL, so
their rows never exist anywhere in state."""
import os, re, shutil, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

MAX_AGE_S = 26 * 3600
MAX_ROWS = 100000
GUID_RES = {"tvdb": re.compile(r"(?:thetvdb|tvdb)://(\d+)"), "imdb": re.compile(r"imdb://(tt\d+)"),
            "tmdb": re.compile(r"(?:themoviedb|tmdb)://(\d+)"), "anidb": re.compile(r"anidb(?:://|-)(\d+)")}   # new agents and the legacy/HAMA forms
IMAGE_CODECS = {"pgs", "hdmv_pgs_subtitle", "dvd_subtitle", "vobsub", "dvb_subtitle", "dvbsub", "xsub"}
ENGLISH = re.compile(r"^(en|eng)(-|$)", re.I)


def map_path(plex_path, rules):
    """Container path from Plex -> host path through rules.path_map, or None when no prefix matches. Longest prefix wins."""
    if not isinstance(plex_path, str) or not plex_path.startswith("/"):
        return None
    best = None
    for prefix, host in rules.get("path_map", {}).items():
        if plex_path == prefix or plex_path.startswith(prefix.rstrip("/") + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, host)
    if best is None:
        return None
    return os.path.join(best[1], plex_path[len(best[0]):].lstrip("/"))


def parse_guids(values):
    """{'tvdb': '123', 'imdb': 'tt..', 'tmdb': '..', 'anidb': '..'} from any number of guid strings (metadata_items.guid and
    the tags/external guids); unknown schemes ignored."""
    out = {}
    for v in values:
        if not isinstance(v, str):
            continue
        for name, rx in GUID_RES.items():
            m = rx.search(v)
            if m and name not in out:
                out[name] = m.group(1)
    return out


def lang_code(value):
    """Plex stores 'eng', 'en', 'es-419', '' or NULL. Normalize to the spec's codes: en, es, es-419, ja, ... ('' -> None)."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    table = {"eng": "en", "spa": "es", "jpn": "ja", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de", "ita": "it",
             "por": "pt", "zho": "zh", "chi": "zh", "kor": "ko", "rus": "ru", "pol": "pl", "und": None, "unk": None}
    if v in table:
        return table[v]
    return v if C.LANG.fullmatch(v) else None


def scan(db_path, rules):
    """(library, coverage) as ARCHITECTURE 'scan.py' steps 2-3. library = {"at", "items": {key: row}}; coverage = {"at", "stale",
    "libraries": {name: {items, en, es, none, image_only, generatable}}, "generatable_total"}.
    Implementation notes for the crew job: open sqlite3 read-only (`file:...?mode=ro`, uri=True); excluded libraries are
    filtered with `ls.name NOT IN (...)` built from rules['exclude_libraries'] (which already includes C.EXCLUDE_LIBRARIES);
    episodes: metadata_items it (metadata_type=4) join seasons s on it.parent_id=s.id join shows sh on s.parent_id=sh.id;
    movies: metadata_type=1; media_items mi on mi.metadata_item_id=it.id; media_parts mp on mp.media_item_id=mi.id (first
    part per item by mp.id); media_streams ms on ms.media_part_id=mp.id (stream_type_id 2 audio, 3 subtitles; a subtitle
    with a non-empty ms.url is a sidecar; a codec in IMAGE_CODECS is 'image', else 'text'); views from metadata_item_views
    v join accounts a on a.id=v.account_id where v.viewed_at > now - priority.watched_days days (count per a.name, for the
    show: sum over its episodes); ids from parse_guids([it.guid, sh.guid]) plus rows of the `taggings`/`tags` guid tags
    when present (ignore when the table is absent). `need`: en missing -> ['en']; es-419 added when the show (or movie) was
    viewed by any user in languages.secondary_for_users and neither 'es' nor 'es-419' is present. A sidecar whose first
    line carries our marker counts as ours (open the host file through C.open_regular, read 200 bytes, C.our_marker).
    Items whose mapped host path fails C.lexical_media_path (no filesystem access) get status 'unmapped', path None, need []. Marks with burned_in
    keep that status (coverage counts them under 'generatable' only when not burned_in)."""
    now = C.now()
    languages = rules.get("languages") or {}
    secondary_set = {u for u in (languages.get("secondary_for_users") or []) if isinstance(u, str)}
    try:
        watched_days = int((rules.get("priority") or {}).get("watched_days", 30))
    except (TypeError, ValueError):
        watched_days = 30
    excluded = list(rules.get("exclude_libraries") or getattr(C, "EXCLUDE_LIBRARIES", ()))

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        if excluded:
            placeholders = ",".join("?" * len(excluded))
            lib_filter = " AND ls.name NOT IN (%s)" % placeholders
            sec_filter = " WHERE name NOT IN (%s)" % placeholders
        else:
            lib_filter = ""
            sec_filter = ""

        sections = cur.execute("SELECT id, name FROM library_sections" + sec_filter, excluded).fetchall()
        lib_names = {int(sid): name for sid, name in sections}
        coverage_libs = {name: {"items": 0, "en": 0, "es": 0, "none": 0, "image_only": 0, "generatable": 0}
                         for name in lib_names.values()}

        episodes = cur.execute(
            "SELECT it.id, it.library_section_id, it.title, it.\"index\", it.year, it.added_at, it.guid, "
            "s.id, s.\"index\", sh.id, sh.guid, sh.title "
            "FROM metadata_items it "
            "JOIN metadata_items s ON s.id = it.parent_id "
            "JOIN metadata_items sh ON sh.id = s.parent_id "
            "JOIN library_sections ls ON ls.id = it.library_section_id "
            "WHERE it.metadata_type = 4" + lib_filter + " ORDER BY it.id", excluded).fetchall()
        movies = cur.execute(
            "SELECT it.id, it.library_section_id, it.title, it.\"index\", it.year, it.added_at, it.guid "
            "FROM metadata_items it "
            "JOIN library_sections ls ON ls.id = it.library_section_id "
            "WHERE it.metadata_type = 1" + lib_filter + " ORDER BY it.id", excluded).fetchall()

        media = {}
        for mid, duration, file_ in cur.execute(
                "SELECT it.id, mi.duration, mp.file FROM metadata_items it "
                "JOIN media_items mi ON mi.id = "
                "(SELECT MIN(mi2.id) FROM media_items mi2 WHERE mi2.metadata_item_id = it.id) "
                "LEFT JOIN media_parts mp ON mp.id = "
                "(SELECT MIN(mp2.id) FROM media_parts mp2 WHERE mp2.media_item_id = mi.id) "
                "WHERE it.metadata_type IN (1, 4)"):
            media[int(mid)] = (duration, file_)

        stream_cols = {row[1] for row in cur.execute("PRAGMA table_info(media_streams)")}
        channels_expr = "ms.channels" if "channels" in stream_cols else "NULL"

        def sidecar_is_ours(url):
            if not isinstance(url, str) or not url.strip():
                return False
            host = map_path(url, rules)                 # unmapped stays unmapped: never probe a container path as a host path
            if not host:
                return False
            fd = None
            try:
                fd = C.open_regular(host, C.MAX_SUB_BYTES)
                if fd is None:
                    return False
                head = os.read(fd, 200)
            except OSError:
                return False
            finally:
                if fd is not None:
                    os.close(fd)
            if not head:
                return False
            return C.our_marker(head.decode("utf-8", "replace")) is not None

        streams = {}
        for mid, stype, codec, language, url, channels in cur.execute(
                "SELECT it.id, ms.stream_type_id, ms.codec, ms.language, ms.url, " + channels_expr + " "
                "FROM metadata_items it "
                "JOIN media_items mi ON mi.id = "
                "(SELECT MIN(mi2.id) FROM media_items mi2 WHERE mi2.metadata_item_id = it.id) "
                "JOIN media_parts mp ON mp.id = "
                "(SELECT MIN(mp2.id) FROM media_parts mp2 WHERE mp2.media_item_id = mi.id) "
                "JOIN media_streams ms ON ms.media_part_id = mp.id "
                "WHERE it.metadata_type IN (1, 4) AND ms.stream_type_id IN (2, 3)"):
            audio, subs = streams.setdefault(int(mid), ([], []))
            lang = lang_code(language)
            if stype == 2:
                audio.append({"lang": lang, "codec": codec, "channels": channels})
            else:
                if isinstance(url, str) and url.strip():
                    kind = "sidecar"
                    ours = sidecar_is_ours(url)
                else:
                    kind = "image" if isinstance(codec, str) and codec.lower() in IMAGE_CODECS else "text"
                    ours = False
                subs.append({"lang": lang, "codec": codec, "kind": kind, "ours": ours})

        cutoff = time.time() - watched_days * 86400
        views_by_item = {}
        for mid, name, count in cur.execute(
                "SELECT it.id, a.name, COUNT(*) FROM metadata_item_views v "        # Plex keys views by guid, not item id
                "JOIN metadata_items it ON it.guid = v.guid "
                "JOIN accounts a ON a.id = v.account_id WHERE v.viewed_at >= ? "
                "GROUP BY it.id, a.name", (cutoff,)):
            if isinstance(name, str):
                views_by_item.setdefault(int(mid), {})[name] = int(count)

        tag_guids = {}
        try:
            for mid, tag in cur.execute(
                    "SELECT tg.metadata_item_id, t.tag FROM taggings tg JOIN tags t ON t.id = tg.tag_id"):
                if isinstance(tag, str) and any(rx.search(tag) for rx in GUID_RES.values()):
                    tag_guids.setdefault(int(mid), []).append(tag)
        except sqlite3.Error:
            tag_guids = {}

        ep_show = {int(row[0]): (int(row[9]) if row[9] is not None else None) for row in episodes}
        show_viewers = {}
        for mid, by_user in views_by_item.items():
            sid = ep_show.get(mid)
            if sid is not None:
                show_viewers.setdefault(sid, set()).update(by_user)

        items = {}

        for row in episodes:
            (it_id, section_id, title, episode, year, added_at, guid,
             season_id, season_num, show_id, show_guid, show_title) = row
            it_id = int(it_id)
            section_id = int(section_id) if section_id is not None else None
            library = lib_names.get(section_id) if section_id is not None else None
            if library is None:
                continue
            key = C.item_key("ep", it_id)
            audio, subs = streams.get(it_id, ([], []))
            duration, plex_path = media.get(it_id, (None, None))
            host = map_path(plex_path, rules) if isinstance(plex_path, str) and plex_path else None
            path = C.lexical_media_path(host, rules) if host else None      # no media here: lexical only; sync/gen/place verify the file
            if not path:
                status = "unmapped"
                path = None
            else:
                status = "ok"
            sub_langs = {s["lang"] for s in subs if s["lang"]}
            en_present = any(l == "en" or l.startswith("en-") for l in sub_langs)      # Plex tags 'en-us', 'en-gb' too
            es_present = "es" in sub_langs or "es-419" in sub_langs
            item_views = dict(views_by_item.get(it_id, {}))
            show_plex_id = int(show_id) if show_id is not None else None
            secondary_watched = (show_plex_id is not None
                                 and bool(secondary_set & show_viewers.get(show_plex_id, set())))
            need = []
            if not en_present:
                need.append("en")
            if not es_present and secondary_watched:
                need.append("es-419")
            if status != "ok":
                need = []
            generatable = False
            if not en_present and any(a["lang"] and ENGLISH.match(a["lang"]) for a in audio):
                generatable = not bool((C.marks(key) or {}).get("burned_in"))
            items[key] = {
                "key": key, "plex_id": it_id, "kind": "ep", "library": library,
                "section_id": section_id, "show_plex_id": show_plex_id, "show": show_title,
                "title": title, "season": season_num, "episode": episode, "year": year,
                "ids": parse_guids([guid, show_guid] + tag_guids.get(it_id, [])),
                "plex_path": plex_path, "path": path, "status": status,
                "duration_ms": duration, "audio": audio, "subs": subs, "views": item_views,
                "added_at": int(added_at) if added_at is not None else None, "need": need,
            }
            cov = coverage_libs[library]
            cov["items"] += 1
            if en_present:
                cov["en"] += 1
            if es_present:
                cov["es"] += 1
            if not subs:
                cov["none"] += 1
            elif all(s["kind"] == "image" for s in subs):
                cov["image_only"] += 1
            if generatable:
                cov["generatable"] += 1

        for row in movies:
            it_id, section_id, title, _index, year, added_at, guid = row
            it_id = int(it_id)
            section_id = int(section_id) if section_id is not None else None
            library = lib_names.get(section_id) if section_id is not None else None
            if library is None:
                continue
            key = C.item_key("mv", it_id)
            audio, subs = streams.get(it_id, ([], []))
            duration, plex_path = media.get(it_id, (None, None))
            host = map_path(plex_path, rules) if isinstance(plex_path, str) and plex_path else None
            path = C.lexical_media_path(host, rules) if host else None      # no media here: lexical only; sync/gen/place verify the file
            if not path:
                status = "unmapped"
                path = None
            else:
                status = "ok"
            sub_langs = {s["lang"] for s in subs if s["lang"]}
            en_present = any(l == "en" or l.startswith("en-") for l in sub_langs)      # Plex tags 'en-us', 'en-gb' too
            es_present = "es" in sub_langs or "es-419" in sub_langs
            item_views = dict(views_by_item.get(it_id, {}))
            secondary_watched = bool(secondary_set & set(item_views))
            need = []
            if not en_present:
                need.append("en")
            if not es_present and secondary_watched:
                need.append("es-419")
            if status != "ok":
                need = []
            generatable = False
            if not en_present and any(a["lang"] and ENGLISH.match(a["lang"]) for a in audio):
                generatable = not bool((C.marks(key) or {}).get("burned_in"))
            items[key] = {
                "key": key, "plex_id": it_id, "kind": "mv", "library": library,
                "section_id": section_id, "show_plex_id": None, "show": None,
                "title": title, "season": None, "episode": None, "year": year,
                "ids": parse_guids([guid] + tag_guids.get(it_id, [])),
                "plex_path": plex_path, "path": path, "status": status,
                "duration_ms": duration, "audio": audio, "subs": subs, "views": item_views,
                "added_at": int(added_at) if added_at is not None else None, "need": need,
            }
            cov = coverage_libs[library]
            cov["items"] += 1
            if en_present:
                cov["en"] += 1
            if es_present:
                cov["es"] += 1
            if not subs:
                cov["none"] += 1
            elif all(s["kind"] == "image" for s in subs):
                cov["image_only"] += 1
            if generatable:
                cov["generatable"] += 1

        library = {"at": now, "items": items}
        coverage = {"at": now, "stale": False, "libraries": coverage_libs,
                    "generatable_total": sum(c["generatable"] for c in coverage_libs.values())}
        return library, coverage
    finally:
        con.close()


def _mark_stale():
    """Flag the existing coverage as stale, keeping its other fields so the page still has numbers to show."""
    cov = C.read_json(C.COVERAGE, 1 << 20)
    cov = cov if isinstance(cov, dict) else {"at": None, "libraries": {}, "generatable_total": 0}
    cov["stale"] = True
    C.write_json(C.COVERAGE, cov)


def main():
    if sys.argv[1:] == ["--board-index"]:                                  # rebuild only the board's copy from library.json
        library = C.read_json(C.LIBRARY, 200 * 1024 * 1024)
        if not isinstance(library, dict):
            C.log("no library.json yet; run a scan first")
            return 1
        C.write_board_index(library)
        return 0
    rules = C.load_rules()
    if rules is None:
        C.log("no valid rules; nothing scanned")
        return 0
    try:
        st = os.stat(C.PLEX_DB_NIGHTLY)
    except OSError:
        C.log("plex nightly copy missing; marking coverage stale")
        _mark_stale()
        return 0
    if time.time() - st.st_mtime > MAX_AGE_S:
        C.log("plex nightly copy is older than 26 h; marking coverage stale")
        _mark_stale()
        return 0
    C.ensure_dir(C.STATE)
    shutil.copyfile(C.PLEX_DB_NIGHTLY, C.PLEX_DB_COPY + ".tmp")
    os.replace(C.PLEX_DB_COPY + ".tmp", C.PLEX_DB_COPY)
    library, coverage = scan(C.PLEX_DB_COPY, rules)
    if len(library.get("items", {})) > MAX_ROWS:
        C.log("library over the row cap; refusing to write")
        _mark_stale()
        return 0
    C.write_json(C.LIBRARY, library)
    C.write_board_index(library)
    C.write_json(C.COVERAGE, coverage)
    C.append_log(C.ACTION_LOG, f"scan items={len(library.get('items', {}))} generatable={coverage.get('generatable_total')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
