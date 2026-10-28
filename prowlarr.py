# VERSION: 2.1
# prowlarr.py
# AUTHORS: swannie-eire (https://github.com/swannie-eire)
# CONTRIBUTORS:
#               Diego de las Heras (ngosang@hotmail.es)
#               ukharley
#               hannsen (github.com/hannsen)
"""qBittorrent search plugin that queries a Prowlarr instance.

Prowlarr aggregates 400+ torrent indexers behind a single Newznab-like API, so
this plugin replaces the per-tracker search plugins shipped with qBittorrent.

The plugin follows the qBittorrent search plugin contract:

* ``url``, ``name`` and ``supported_categories`` are static class attributes.
* ``search(query, category)`` prints one pipe-delimited result per line to
  stdout through ``novaprinter.prettyPrinter``.
* ``download_torrent(url)`` prints the ``<path> <url>`` pair expected by
  qBittorrent, or ``<magnet> <magnet>`` for magnet-only releases.
* Nothing except result rows is written to stdout; diagnostics go to stderr
  through ``logging``.
"""

from __future__ import annotations

__version__ = '2.1'

import json
import logging
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
try:
    from datetime import UTC  # noqa: F401  # Python 3.11+
except ImportError:  # Python < 3.11
    from datetime import timezone as _timezone

    UTC = _timezone.utc
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from helpers import download_file
from novaprinter import prettyPrinter

if TYPE_CHECKING:
    from novaprinter import SearchResults

_logger = logging.getLogger(__name__)

CONFIG_FILE: Final = 'prowlarr.json'
HELP_URL: Final = 'https://github.com/swannie-eire/prowlarr-qbittorrent-plugin#configuration-file'
USER_AGENT: Final = (
    'prowlarr-qbittorrent-plugin/2.0 (+https://github.com/swannie-eire/prowlarr-qbittorrent-plugin)'
)
DEFAULT_TIMEOUT: Final = 30.0
MIN_TIMEOUT: Final = 1.0
MAX_TIMEOUT: Final = 300.0
REDIRECT_CODES: Final = frozenset({301, 302, 303, 307, 308})

#: Values written to a freshly created configuration file.  ``indexer_ids``,
#: ``timeout`` and ``sort_by_seeds`` are optional and documented in the README.
DEFAULT_CONFIG: Final[dict[str, Any]] = {
    'api_key': 'YOUR_API_KEY_HERE',
    'tracker_first': False,
    'url': 'http://127.0.0.1:9696',
}
DEFAULT_INDEXER_IDS: Final[tuple[int, ...]] = (-2,)

#: Control characters (including CR/LF) would let a malicious indexer inject
#: extra result rows, so every field is sanitised before it reaches stdout.
_CONTROL_CHARS: Final = re.compile(r'[\x00-\x1f\x7f]+')
_HTML_TAG: Final = re.compile(r'<[^>]+>')


class ProwlarrError(RuntimeError):
    """Raised when a Prowlarr request cannot be completed."""


def _clean(value: Any, *, replace_pipe: bool = False) -> str:
    """Return ``value`` as a single-line string safe for the result format.

    Args:
        value: Any value from the Prowlarr response.
        replace_pipe: Percent-encode ``|``. Required for every field except
            ``name`` because ``|`` is the field delimiter and newer versions of
            ``prettyPrinter`` reject it outright.

    Returns:
        The sanitised text with control characters collapsed into spaces.
    """
    text = _CONTROL_CHARS.sub(' ', str(value)).strip()
    if replace_pipe:
        text = text.replace('|', '%7C')
    return text


def _normalise_url(value: Any) -> str | None:
    """Validate and normalise a Prowlarr base URL.

    Args:
        value: Candidate URL from the configuration file.

    Returns:
        The URL without a trailing slash, or ``None`` when it is not a usable
        ``http``/``https`` URL.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or _CONTROL_CHARS.search(candidate):
        return None
    parts = urllib.parse.urlsplit(candidate)
    if parts.scheme not in {'http', 'https'} or not parts.netloc:
        return None
    return candidate.rstrip('/')


def _parse_indexer_ids(value: Any) -> list[int] | None:
    """Parse the optional ``indexer_ids`` configuration value.

    Args:
        value: A single integer or a list of integers. ``-2`` means "all
            configured indexers" in Prowlarr.

    Returns:
        A non-empty list of integers, or ``None`` when the value is invalid.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple)) and value:
        identifiers: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                return None
            identifiers.append(item)
        return identifiers
    return None


def _default_config_path() -> Path:
    """Return the configuration path, honouring the ``PROWLARR_CONFIG`` override."""
    override = os.environ.get('PROWLARR_CONFIG')
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / CONFIG_FILE


CONFIG_PATH: Final[Path] = _default_config_path()


def load_configuration(config_path: Path | None = None) -> dict[str, Any]:
    """Load, validate and normalise the plugin configuration.

    A missing file is created next to the plugin with mode ``0600`` because it
    is expected to hold an API key.  Invalid values fall back to safe defaults
    and set ``malformed`` to a human-readable reason; ``search`` turns that into
    a single explanatory result row instead of raising into qBittorrent.

    Args:
        config_path: Optional path to read. Defaults to :data:`CONFIG_PATH`.

    Returns:
        The normalised configuration dictionary.
    """
    path = CONFIG_PATH if config_path is None else Path(config_path)
    data: dict[str, Any] = {
        'api_key': DEFAULT_CONFIG['api_key'],
        'tracker_first': bool(DEFAULT_CONFIG['tracker_first']),
        'url': str(DEFAULT_CONFIG['url']),
        'timeout': DEFAULT_TIMEOUT,
        'indexer_ids': list(DEFAULT_INDEXER_IDS),
    }

    try:
        raw_text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, 'w', encoding='utf-8') as config_file:
                json.dump(DEFAULT_CONFIG, config_file, indent=4, sort_keys=True)
        except OSError as exc:
            _logger.warning("Could not create configuration file '%s': %s", path, exc)
        return data
    except OSError as exc:
        data['malformed'] = f'cannot read configuration file: {exc}'
        return data

    if os.name == 'posix':
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0o600
        if mode & 0o077:
            _logger.warning(
                "Configuration file '%s' is accessible by other users (mode %o); "
                "run 'chmod 600 %s' to protect the API key.",
                path,
                mode,
                path,
            )

    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        data['malformed'] = f'invalid JSON in configuration file: {exc}'
        return data

    if not isinstance(raw, dict):
        data['malformed'] = 'configuration file must contain a JSON object'
        return data

    problems: list[str] = []

    api_key = raw.get('api_key')
    if isinstance(api_key, str) and api_key.strip():
        data['api_key'] = api_key.strip()
    else:
        problems.append('"api_key" is missing or empty')

    normalised_url = _normalise_url(raw.get('url'))
    if normalised_url is None:
        problems.append('"url" must be an http(s) URL, e.g. "http://127.0.0.1:9696"')
    else:
        data['url'] = normalised_url

    tracker_first = raw.get('tracker_first', False)
    if isinstance(tracker_first, bool):
        data['tracker_first'] = tracker_first
    else:
        problems.append('"tracker_first" must be true or false')

    timeout = raw.get('timeout', DEFAULT_TIMEOUT)
    if (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and MIN_TIMEOUT <= timeout <= MAX_TIMEOUT
    ):
        data['timeout'] = float(timeout)
    elif 'timeout' in raw:
        _logger.warning(
            'Invalid "timeout" value %r in %s; using %gs.',
            raw['timeout'],
            path,
            DEFAULT_TIMEOUT,
        )

    indexer_ids = _parse_indexer_ids(raw.get('indexer_ids', list(DEFAULT_INDEXER_IDS)))
    if indexer_ids is None:
        _logger.warning(
            'Invalid "indexer_ids" value %r in %s; using all indexers.',
            raw.get('indexer_ids'),
            path,
        )
    else:
        data['indexer_ids'] = indexer_ids

    if problems:
        data['malformed'] = '; '.join(problems)
    return data


CONFIG_DATA: dict[str, Any] = load_configuration()


class prowlarr:
    """qBittorrent search engine plugin backed by the Prowlarr API."""

    name = 'prowlarr'
    url = str(CONFIG_DATA['url'])
    api_key = str(CONFIG_DATA['api_key'])
    tracker_first: bool = bool(CONFIG_DATA['tracker_first'])
    indexer_ids: list[int] = list(CONFIG_DATA['indexer_ids'])
    timeout: float = float(CONFIG_DATA['timeout'])

    # Prowlarr uses Newznab category ids:
    # https://github.com/Prowlarr/Prowlarr/blob/develop/src/NzbDrone.Core/Indexers/NewznabStandardCategory.cs
    # ``None`` means "send no categories parameter" (all categories).
    supported_categories: dict[str, tuple[int, ...] | None] = {
        'all': None,
        'anime': (5070,),  # TV/Anime
        'books': (7000,),  # Books
        'games': (1000, 4000),  # Console + PC
        'movies': (2000,),  # Movies
        'music': (3000,),  # Audio
        'software': (4000,),  # PC
        'tv': (5000,),  # TV
    }

    def search(self, what: str, cat: str = 'all') -> None:
        """Search Prowlarr and print the matching releases.

        ``what`` is percent-encoded by ``nova2.py`` (e.g. ``Ubuntu+Linux``), so
        it is decoded here and re-encoded through ``urllib.parse.urlencode`` to
        keep ``&``, ``#``, ``?`` and ``+`` inside the query instead of letting
        them alter the request.

        Args:
            what: The search terms.
            cat: One of the keys of :attr:`supported_categories`.
        """
        query = urllib.parse.unquote(what or '').strip()
        category = self.supported_categories.get((cat or 'all').lower())

        malformed = CONFIG_DATA.get('malformed')
        if malformed:
            self.handle_error(f'malformed configuration file ({malformed})', query)
            return

        if not self.api_key or self.api_key == DEFAULT_CONFIG['api_key']:
            self.handle_error('api key error', query)
            return

        endpoint = f'{self.url}/api/v1/search'
        try:
            body = self._fetch(
                f'{endpoint}?{self._search_query(query, category)}',
                api_key=self.api_key,
            )
        except ProwlarrError as exc:
            self.handle_error(str(exc), query)
            return

        try:
            payload = json.loads(body.decode('utf-8', 'replace'))
        except ValueError:
            self.handle_error('invalid JSON response from Prowlarr', query)
            return

        if isinstance(payload, dict):
            message = payload.get('message') or payload.get('error') or 'unexpected API response'
            self.handle_error(_clean(message), query)
            return
        if not isinstance(payload, list):
            self.handle_error('unexpected API response from Prowlarr', query)
            return

        results = [
            result
            for item in payload
            if isinstance(item, Mapping)
            if (result := self._build_result(item)) is not None
        ]
        # qBittorrent recommends returning the most seeded releases first.
        results.sort(key=lambda item: item['seeds'], reverse=True)
        for result in results:
            prettyPrinter(cast('SearchResults', result))

    def download_torrent(self, url: str) -> None:
        """Download a ``.torrent`` file or pass through a magnet link.

        Some indexers return a magnet link from their download endpoint (either
        as the body or as a ``302`` redirect). qBittorrent expects
        ``<path> <url>`` on stdout, and ``<magnet> <magnet>`` for magnets.

        Args:
            url: The download URL taken from a search result.
        """
        if not isinstance(url, str) or not url.strip():
            _logger.error('download_torrent called without a URL')
            return
        url = _CONTROL_CHARS.sub('', url).strip()

        if url.startswith('magnet:'):
            print(f'{url} {url}')
            return

        try:
            body = self._fetch(url, api_key=self._api_key_for(url))
        except ProwlarrError as exc:
            _logger.error("Cannot download torrent '%s': %s", url, exc)
            return

        magnet = _CONTROL_CHARS.sub('', body.decode('utf-8', 'replace')).strip()
        if magnet.startswith('magnet:'):
            print(f'{magnet} {url}')
            return

        try:
            print(download_file(url))
        except Exception as exc:  # noqa: BLE001 - never raise into qBittorrent
            _logger.error("Cannot download torrent '%s': %s", url, exc)

    def _search_query(self, query: str, categories: tuple[int, ...] | None) -> str:
        """Build the URL-encoded query string for ``/api/v1/search``.

        Args:
            query: Decoded search terms.
            categories: Newznab category ids, or ``None`` for all categories.

        Returns:
            The encoded query string (without a leading ``?``).
        """
        params: list[tuple[str, str]] = [('query', query), ('type', 'search')]
        params.extend(('indexerIds', str(indexer_id)) for indexer_id in self.indexer_ids)
        if categories:
            params.extend(('categories', str(category)) for category in categories)
        return urllib.parse.urlencode(params)

    def _build_result(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        """Convert one Prowlarr release into a ``prettyPrinter`` dictionary.

        Args:
            item: A single element of the Prowlarr search response.

        Returns:
            The result dictionary, or ``None`` when the release cannot be
            downloaded (qBittorrent would otherwise show a dead row).
        """
        title = _clean(item.get('title') or item.get('sortTitle') or '')
        if not title:
            return None

        link = item.get('downloadUrl') or item.get('magnetUrl')
        if not isinstance(link, str) or not link.strip():
            return None
        link = _clean(link, replace_pipe=True)
        if not link.startswith(('http://', 'https://', 'magnet:')):
            return None

        tracker = _clean(item.get('indexer') or 'unknown')
        name = f'[{tracker}] {title}' if self.tracker_first else f'{title} [{tracker}]'

        desc_link = item.get('infoUrl') or item.get('guid') or ''
        return {
            'link': link,
            'name': name,
            'size': self._format_size(item.get('size')),
            'seeds': self._format_count(item.get('seeders')),
            'leech': self._format_count(item.get('leechers')),
            'engine_url': _clean(self.url, replace_pipe=True),
            'desc_link': _clean(desc_link, replace_pipe=True),
            'pub_date': _parse_publish_date(item.get('publishDate')),
        }

    @staticmethod
    def _format_size(value: Any) -> str:
        """Return the release size in bytes as a string, or ``'-1'`` if unknown."""
        if value is None or isinstance(value, bool):
            return '-1'
        if isinstance(value, (int, float)):
            return str(int(value))
        text = _clean(value, replace_pipe=True)
        return text or '-1'

    @staticmethod
    def _format_count(value: Any) -> int:
        """Return a seed/leech count, using ``-1`` for unknown values."""
        if isinstance(value, bool):
            return -1
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return -1

    def _fetch(self, url: str, *, api_key: str | None = None) -> bytes:
        """Perform an HTTP GET request.

        Args:
            url: The absolute URL to request.
            api_key: Optional Prowlarr API key sent as ``X-Api-Key`` instead of
                in the query string, so it does not end up in server access
                logs or ``Referer`` headers.

        Returns:
            The raw response body.

        Raises:
            ProwlarrError: If the connection fails or the server reports an
                error, with a message suitable for the error result row.
        """
        headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json, */*'}
        if api_key:
            headers['X-Api-Key'] = api_key

        try:
            request = urllib.request.Request(url, headers=headers)
        except ValueError as exc:
            raise ProwlarrError(f'invalid URL: {exc}') from exc
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return cast(bytes, response.read())
        except urllib.error.HTTPError as exc:
            # Some indexers redirect downloads to a magnet link.  urllib can
            # only follow http(s)/ftp redirects, so inspect Location instead of
            # relying on ``exc.url``, whose behaviour varies between versions.
            location = exc.headers.get('Location', '') if exc.headers is not None else ''
            if exc.code in REDIRECT_CODES and location.startswith('magnet:'):
                return location.encode('utf-8')
            raise ProwlarrError(self._describe_http_error(exc)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ProwlarrError(f'timed out after {self.timeout:g}s') from exc
            raise ProwlarrError(f'connection failed: {exc.reason}') from exc
        except TimeoutError as exc:
            raise ProwlarrError(f'timed out after {self.timeout:g}s') from exc

    @staticmethod
    def _describe_http_error(error: urllib.error.HTTPError) -> str:
        """Return a short, single-line description of an HTTP error."""
        if error.code in {401, 403}:
            return f'invalid or missing API key (HTTP {error.code})'

        try:
            body = error.read().decode('utf-8', 'replace')
        except (OSError, ValueError):
            return f'HTTP {error.code}'

        message = ''
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            message = str(payload.get('message') or payload.get('error') or '')
        if not message:
            message = _clean(_HTML_TAG.sub(' ', body))[:200]

        return f'HTTP {error.code}: {message}' if message else f'HTTP {error.code}'

    def _api_key_for(self, url: str) -> str | None:
        """Return the API key only when ``url`` points at the configured Prowlarr host.

        Download URLs can point directly at third-party indexers; sending the
        API key to them would leak the credential.
        """
        try:
            target = urllib.parse.urlsplit(url)
            base = urllib.parse.urlsplit(self.url)
        except ValueError:
            return None
        if target.scheme in {'http', 'https'} and (target.scheme, target.netloc) == (
            base.scheme,
            base.netloc,
        ):
            return self.api_key
        return None

    def handle_error(self, error_msg: str, what: str) -> None:
        """Print a single synthetic result row describing a failure.

        qBittorrent only displays result rows, so errors are reported the same
        way; the description link opens the plugin documentation.

        Args:
            error_msg: Human-readable failure reason.
            what: The search terms, repeated so the row is identifiable when
                qBittorrent shows "Torrent names only".
        """
        message = (
            f'Prowlarr: {_clean(error_msg, replace_pipe=True)}! Right-click this row '
            "and select 'Open description page' to open help. "
            f"Configuration file: '{CONFIG_PATH}' Search: '{_clean(what, replace_pipe=True)}'"
        )
        prettyPrinter(
            {
                'link': _clean(self.url, replace_pipe=True),
                'name': message,
                'size': -1,
                'seeds': -1,
                'leech': -1,
                'engine_url': _clean(self.url, replace_pipe=True),
                'desc_link': HELP_URL,
                'pub_date': -1,
            }
        )


def _parse_publish_date(value: Any) -> int:
    """Convert a Prowlarr ``publishDate`` value into a Unix timestamp.

    Args:
        value: ISO-8601 string, numeric timestamp, or ``None``.

    Returns:
        The Unix timestamp, or ``-1`` when the value cannot be parsed.
    """
    if isinstance(value, bool):
        return -1
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else -1
    if not isinstance(value, str) or not value.strip():
        return -1

    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = f'{text[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return -1
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def main(argv: list[str] | None = None) -> int:
    """Run a search from the command line for development.

    Usage: ``python3 prowlarr.py [category] <search terms>``.

    Args:
        argv: Command-line arguments without the program name.

    Returns:
        Process exit code.
    """
    logging.basicConfig(format='%(levelname)s: %(message)s', stream=sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)

    category = 'all'
    if args and args[0].lower() in prowlarr.supported_categories:
        category = args.pop(0).lower()

    query = ' '.join(args) or 'ubuntu'
    prowlarr().search(query, category)
    return 0


if __name__ == '__main__':
    sys.exit(main())
