# Working on my_immich

A single-file Python app that imports camera SD cards (or any folder) into
Immich by running immich-go. See README.md for what users see.

## Commands

```bash
python3 -m unittest discover -s tests -v   # run tests (no network, no real immich-go)
python3 my_immich.py --no-browser          # run the page at http://127.0.0.1:8765
bash install.sh                            # rebuild ~/Applications/my_immich.app (macOS)
```

For manual testing without a card or a server, point the app at fakes:

```bash
MY_IMMICH_VOLUMES=/tmp/fake/Volumes MY_IMMICH_CONFIG_DIR=/tmp/fake/config \
  python3 my_immich.py --no-browser --port 8791
```

## Layout of my_immich.py

Top to bottom:

1. Constants: `DEFAULTS` (every setting and its default), `ENUMS` (allowed
   values for the dropdowns).
2. Config: `clean_config`, `load_config`, `save_config`, `import_old_script`.
3. Environment: `find_immich_go`, `list_cards`, `list_drives`, `scan_folder`,
   `places` and `browse` (the folder chooser), `test_connection`.
4. Job: `build_command` (settings → immich-go arguments), `Job` (runs the
   child process and buffers its log), `eject_for`.
5. HTTP: `Handler` with the JSON API under `/api/…`.
6. `PAGE`: the whole web page (HTML, CSS, JS) as one raw string. It is an app
   shell: sidebar navigation, Import, Settings and Setup views (Setup is the
   first-run checklist, driven by `renderSetup()`), and a
   `<dialog>` folder chooser. Colours and radii are CSS variables at the top;
   amber is the single accent and the SD-card shape is the one decorative motif.
   System fonts only, so it works offline. Keep light and dark in step.
7. `run_tray` (rumps menu-bar app) and `main`.

## Rules to keep

- **Standard library only** in `my_immich.py`, except the lazy `import rumps`
  inside `run_tray`. The page must work with a bare `python3`.
- **Adding a setting** touches four places: `DEFAULTS` (and `ENUMS` if it is a
  dropdown), `build_command`, the form in `PAGE` (plus the `FIELDS`/`CHECKS`
  and `LABELS` lists in its script), and the README settings table. Add a test
  in `tests/test_my_immich.py`.
- **Check flags against immich-go's docs** before adding one:
  <https://github.com/simulot/immich-go/blob/main/docs/commands/upload.md>.
  Flags go after `from-folder` and before the source path.
- **Security:** keep the server bound to `127.0.0.1`; every `/api/` route
  except `/api/hello` must pass `_auth()` (token + Host check). Never log or
  return the API key unmasked — use `mask()`.
- **Requests to Immich go through `immich_get`**, which sends `USER_AGENT`.
  Python's default `Python-urllib` agent is refused with 403 by Cloudflare and
  similar proxies, so a bare `urlopen` works on the LAN and fails on a public
  domain.
- **Config file** is written with mode 600 and holds the API key. Never write
  it inside the project folder; `.gitignore` excludes `config.json` as a
  backstop.
- **Tray mode restarts itself** (`relaunch_for_tray`): Cocoa treats a file path in the
  process arguments as a document to open, so `python my_immich.py --tray` made macOS
  open my_immich.py in Xcode. Tray mode re-executes Python with `-c` and passes its
  options in `MY_IMMICH_*` environment variables. Do not add file paths to that argv.
- Card detection means "a directory under the volumes root that contains
  `DCIM`"; the import source for a card is its `DCIM` folder.
- **Keep the README screenshots current.** When the page's look changes, retake
  the affected images in `docs/` (`progress.png`, `import.png`, `choose-folder.png`,
  `settings.png`, `setup.png`) in the same change. Use demo data only: a made-up
  server name, a placeholder user and invented folder names, never a real library,
  address or API key. They are 1240 px wide at 2x.
- Do not commit or push unless asked.

## Progress display

Everything on the progress panel comes from one `/api/log` snapshot, rendered by
`showProgress(j)` and `drawGeek(j, t)` in the page. Where the numbers come from:

- **Status line.** With `--no-ui`, immich-go rewrites one line every 500 ms using a
  bare carriage return: `Immich read N%, Assets found: N, Upload errors: N, Uploaded N`.
  `Job._take` parses it with `PROGRESS_RE` into `job.progress` and keeps it out of the
  log (the last one is logged once). Source:
  <https://github.com/simulot/immich-go/blob/main/app/upload/noui.go>.
- **Per-file events.** `Job._run` adds `--log-file=<temp file>` and `_follow_log`
  tails it. Each log line's message is one of the strings in `EVENTS` (from
  `internal/fileevent/fileevents.go` in immich-go) with a `file=` attribute;
  `_event` turns them into `counters`, the `feed`, and `bytes_up` (the size of
  each uploaded file, looked up under the source folder). If immich-go answers
  "unknown flag" for `--log-file`, the run restarts once without it. If the user
  put their own `--log-file` in Extra arguments, ours is not added.
- **Per-second samples.** `_sample` records `points` (files, bytes, interface
  bytes sent) for the chart, plus `process_stats` (`ps`/`pgrep`), `net_counters`
  (`netstat -ibn` on macOS, `/proc/net/dev` on Linux) and `server_stats` (Immich's
  `/api/server/ping`, `/api/assets/statistics`, `/api/server/storage`,
  `/api/server/version`). Every one of these is optional and is simply left off the
  panel when it cannot be read.
- `Job._count_source` scans the source in a thread for totals and the contact
  sheet sample. `make_thumb` shells out to `sips` on macOS; `/api/thumb` takes the
  token as a query parameter because `<img>` cannot send headers.

If immich-go changes its status line or event wording, the panel degrades (waiting
bar, no counters) rather than breaking; update `PROGRESS_RE`/`EVENTS` and the tests
named `test_status_line_...` and `test_events_from_the_log_file_...`.

## Icons

- `assets/menubar.png` (44×44, black on transparent) is the menu-bar icon, loaded
  by `run_tray` as a template image so macOS recolours it for light and dark menu
  bars. If the file is missing the tray falls back to the text "MI".
- `assets/icon.png` (1024×1024) is the app icon; `install.sh` turns it into
  `AppIcon.icns` with `sips` and `iconutil`.
- Both are rendered from the `.svg` files beside them. Re-render the PNGs after
  editing an SVG. The browser-tab icon is an inline SVG in `PAGE`.

## Not yet verified on macOS

`run_tray`, notifications, the `.app` bundle from `install.sh`, the Dock-icon
suppression and `diskutil eject` were written without a Mac to run them on.
Treat reports about them as likely real bugs.

## Ideas not built

- Verify every file on the card exists in Immich, then offer to wipe it.
- Auto-start an import when a known card is inserted.
- An "organise last import" step that proposes albums for a tagged batch.
- Per-camera profiles.
