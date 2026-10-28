### THIS REPO IS NO LONGER MAINTAINED.
### For a supported plugin please use Jackett.



**qBittorrent** comes with a few search plugins. Although these are enough for most users, if you wish to add more search engines, you can download **Prowlarr** configure the **prowlarr qBittorrent plugin** (essentially, set the API key).

**[prowlarr](https://github.com/prowlarr/prowlarr)** is a server program that provides support for more than 400 torrent sites (public and private). You may download the plugin at [this address](https://raw.githubusercontent.com/swannie-eire/prowlarr-qbittorrent-plugin/main/prowlarr.py).

### Requirements
* Prowlarr 1.x or newer.
* qBittorrent 5.x (the bundled Python must be 3.11 or newer; the plugin is tested against the
  [official search plugin files](https://github.com/qbittorrent/qBittorrent/tree/master/src/searchengine/nova3)).
* No extra Python packages. Only the standard library plus the `socks` module that ships with
  qBittorrent are used.

### Disable the prowlarr plugin
By default, the prowlarr plugin is enabled in qBittorrent. If you want to disable it, follow these steps:
1. In the `Search tab`, click the `Search plugins...` button (bottom-right)
2. Right click on the `prowlarr` plugin
3. Uncheck the `Enabled` checkbox
4. Close the modal window

### Configuration file
The prowlarr plugin uses an external configuration file. This allows to update the plugin without losing the settings.

The file `prowlarr.json` should be located in the qBittorrent search engines folder:
* Windows: `%localappdata%\qBittorrent\nova3\engines\`
* Linux: `~/.local/share/data/qBittorrent/nova3/engines/`, or `~/.local/share/qBittorrent/nova3/engines/`, or `~/.var/app/org.qbittorrent.qBittorrent/data/qBittorrent/nova3/engines` if using Flatpak
* OS X: `~/Library/Application Support/qBittorrent/nova3/engines/`

Note: If the file doesn't exist, the plugin creates it with the default values and permissions `0600`.

Note 2: The `PROWLARR_CONFIG` environment variable can point at a different configuration file, which is
useful for testing or for container setups.

Note 3: Remember to [start prowlarr](https://github.com/prowlarr/prowlarr) first. :)

Note 4: If running qBittorrent headless and using the web page on a remote server, prowlarr needs to be configured to allow remote calls and its IP. Eg:

```json
$ cat config/data/qBittorrent/nova3/engines/prowlarr.json
{
    "api_key": "YOUR_API_KEY_HERE",
    "tracker_first": false,
    "url": "http://yourserverip:9696"
}
```

### Configuration properties
| Property | Default value | Description |
|---|---|---|
| api_key | YOUR_API_KEY_HERE | Prowlarr API key (Settings > General > Security) |
| tracker_first | false | (false/true) add the tracker name to the beginning of the search result |
| url | http://127.0.0.1:9696 | Prowlarr URL, with or without the trailing slash |
| timeout | 30 | Optional. Request timeout in seconds (1-300) |
| indexer_ids | [-2] | Optional. Prowlarr indexer IDs to search; `-2` means all configured indexers |

The API key is sent in the `X-Api-Key` header, not in the URL, so it does not appear in Prowlarr
access logs or in `Referer` headers. It is never sent to third-party indexer hosts.

A malformed configuration, a missing API key or a Prowlarr error is reported as a single search result
row that explains the problem; open its description page for the configuration help.

### Categories
Category filters use the Newznab IDs that Prowlarr exposes:

| qBittorrent category | Prowlarr categories |
|---|---|
| all | (no filter) |
| anime | 5070 (TV/Anime) |
| books | 7000 (Books) |
| games | 1000 (Console) + 4000 (PC) |
| movies | 2000 (Movies) |
| music | 3000 (Audio) |
| software | 4000 (PC) |
| tv | 5000 (TV) |

Results are sorted by seeders, most seeded first, as recommended by the plugin documentation.

### Development
```shell
python3 -m pytest                                  # unit + HTTP integration tests
python3 -m ruff check prowlarr.py tests/           # lint
python3 -m mypy --strict prowlarr.py               # type check
python3 prowlarr.py movies ubuntu linux            # run a search (needs a live Prowlarr)
```

`helpers.py` and `novaprinter.py` are unmodified copies of the files bundled with qBittorrent; at
runtime qBittorrent uses its own copies.

API Key in prowlarr web UI:

![](https://i.imgur.com/ePMq68M.png)

### Screenshot
![](https://i.imgur.com/a6WPJC8.png)
