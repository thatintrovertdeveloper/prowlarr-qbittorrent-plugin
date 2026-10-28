# PROJECT KNOWLEDGE BASE

**Generated:** 2025-10-29

## OVERVIEW
Project: **prowlarr-qbittorrent-plugin** — a single-file search plugin that lets qBittorrent query a
[Prowlarr](https://github.com/prowlarr/prowlarr) instance (400+ indexers) instead of individual trackers.

Stack: Python 3 (stdlib only; `socks` comes from qBittorrent's own `nova3` directory), qBittorrent
"nova3" search-engine plugin API. Dev-only tooling: pytest, ruff, mypy. No packaging, no CI.

Status: upstream README states the repo is **no longer maintained** (use Jackett instead). Treat as
feature-frozen; keep changes minimal and backward compatible with existing installs.

## STRUCTURE
```
prowlarr.py         # THE plugin: config loading + search + torrent download
helpers.py          # vendored qBittorrent helpers, verbatim upstream 1.61
novaprinter.py      # vendored qBittorrent output serializer, verbatim upstream 1.56
prowlarr.json       # user config, created at runtime next to the plugin (gitignored, do not commit)
prowlarr-logo-lq.png# plugin icon shown in qBittorrent
tests/              # pytest suite (unit + fake-Prowlarr HTTP integration)
tests/conftest.py   # puts repo on sys.path and points PROWLARR_CONFIG at a temp dir
pyproject.toml      # pytest/ruff/black/mypy config only (not a distributable package)
README.md           # user-facing install, config, categories, development docs
```
* `helpers.py` and `novaprinter.py` are unmodified copies of the files shipped by qBittorrent. They are
  kept here only so the plugin is usable standalone/during development; at runtime qBittorrent's own
  copies are used. **Do not edit or "modernize" them** — they must stay byte-identical to upstream.
* Only `prowlarr.py` is project code.

## COMMANDS
| Action | Command |
|--------|---------|
| Install | none — drop `prowlarr.py` + `prowlarr.json` into qBittorrent's `nova3/engines/` dir |
| Test   | `python3 -m pytest` (coverage: `python3 -m pytest --cov=prowlarr --cov-report=term-missing`) |
| Lint   | `python3 -m ruff check prowlarr.py tests/` (format: `python3 -m ruff format prowlarr.py tests/`) |
| Types  | `python3 -m mypy --strict prowlarr.py` |
| Build  | none (interpreted, single file) |
| Run    | `python3 prowlarr.py [category] <search terms>` (defaults to `all` / `ubuntu`; needs a live Prowlarr + valid `api_key`) |

## CODING STANDARDS
* **Language**: Python 3.11+ syntax, PEP 8. Runtime imports must stay stdlib-only (`socks` only
  indirectly, via the vendored `helpers.py`); qBittorrent cannot install PyPI packages for plugins.
* **Types**: full annotations, `mypy --strict` clean. Use `X | None`, `Final`, `collections.abc`
  types. Third-party/typed-dict types from vendored modules are only imported under
  `TYPE_CHECKING` so old qBittorrent `novaprinter` versions (pre-5.3) still import the plugin.
* **Style**: single quotes, 4-space indent, 100-column lines, snake_case functions, Google-style
  docstrings, `# VERSION: x.y` header at top of the plugin, `logging` to stderr (never `print` to
  stdout except the result rows).
* **Output contract**: one pipe-separated line per result, printed to **fd 1** via
  `novaprinter.prettyPrinter({...})` with keys `link`, `name`, `size`, `seeds`, `leech`,
  `engine_url`, `desc_link`, `pub_date`.
  * `novaprinter` >= 1.56 percent-encodes `name` itself and raises `ValueError` if `|` appears in
    any other field. The plugin therefore sanitises control characters in every field and
    percent-encodes `|` in every field **except** `name` (old printers replace `|` in `name` with a
    space, so leaving it raw works for both generations).
* **Error surfacing**: no exceptions to the host. `handle_error()` prints a synthetic result row
  whose name contains the reason, the config path and the search term, because qBittorrent only
  displays result rows. Its `desc_link` is the README configuration anchor.
* **Config**: `load_configuration()` runs at import; values are validated/normalised into
  `CONFIG_DATA` and then baked into class attributes (`url`, `api_key`, `indexer_ids`, `timeout`).
  Edits to `prowlarr.json` require a plugin reload. Missing file → defaults written with mode
  `0600`. Invalid values fall back to safe defaults and set `CONFIG_DATA['malformed']` to the
  reason, so the class body can never raise at import time.

## WHERE TO LOOK
* **Source**: `prowlarr.py` (`search`, `download_torrent`, `_fetch`, `_build_result`,
  `load_configuration`, `supported_categories`)
* **Tests**: `tests/test_prowlarr.py`
* **Docs**: `README.md` (install paths for Windows/Linux/Flatpak/OSX, config schema, category map,
  remote-setup notes, dev commands)
* **Upstream contract**: <https://github.com/qbittorrent/search-plugins/wiki/How-to-write-a-search-plugin>

## NOTES / GOTCHAS
* **`engine_url` is load-bearing**: it must stay the Prowlarr base URL. qBittorrent passes it back
  when downloading; changing it silently breaks torrent downloads.
* **Query encoding**: `nova2.py` passes `urllib.parse.quote(...)` output (e.g. `Ubuntu+Linux`), so
  `search()` does `unquote()` then rebuilds the URL with `urllib.parse.urlencode`. Never go back to
  string concatenation — `&`, `#`, `?`, `+` would alter the request (query injection).
* **API key**: sent as the `X-Api-Key` header (supported by Prowlarr) rather than `?apikey=`, so it
  is not logged in access logs. `_api_key_for()` only attaches it when the host matches the
  configured Prowlarr URL, because `downloadUrl` can point directly at a third-party indexer.
* **Category map is Newznab-based**: `all: None` (omit the parameter), `games: (1000, 4000)`,
  `books: (7000,)`, `software: (4000,)`. Repeated `categories=` parameters are valid (Prowlarr's
  `SearchResource.Categories` is a list).
* **`indexerIds=-2`** means "all configured indexers" in Prowlarr; configurable via `indexer_ids`.
* **Magnet-in-.torrent hack**: `download_torrent` prints `"<magnet> <download_url>"` when the
  request URL is already a magnet, when the body is a magnet, or when a redirect `Location` is a
  magnet. `_fetch` reads `HTTPError.headers['Location']` because `HTTPError.url` behaviour for
  non-http redirect schemes differs between Python versions. Magnet URLs return immediately — the
  old code printed the magnet and then fetched again (double output).
* **No timeouts previously**: `_fetch` now always passes `timeout` (config key, default 30s).
* **`desc_link` fallback chain**: `infoUrl` → `guid` → `''`.
* **`pub_date`** comes from Prowlarr's `publishDate` (ISO-8601, may have 7 fractional digits);
  unparsable values become `-1` (older `prettyPrinter` ignores the extra key).
* **Seeds/leech/size `None` → `-1`**, matching qBittorrent's "unknown" convention.
* **Results are sorted by seeders** before printing, as the plugin docs recommend.
* **Releases without a usable link are skipped** instead of emitting a row whose download fails.
* **Network errors** are reported with the HTTP status and JSON/HTML message (e.g. `401` →
  "invalid or missing API key"); `get_response` no longer swallows every exception silently.
* **Config edge cases**: `prowlarr.json` that is valid JSON but not an object, non-http `url`,
  non-boolean `tracker_first`, or a missing key yields a readable error row, not an import crash.
* **`PROWLARR_CONFIG`** env var overrides the config path (used by the test suite; `conftest.py`
  points it at a temp dir so tests never create `prowlarr.json` in the repo).
* `helpers.py`/`novaprinter.py` carry `# VERSION:` comments; leave them verbatim.
