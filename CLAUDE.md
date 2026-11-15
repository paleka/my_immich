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
3. Environment: `find_immich_go`, `list_cards`, `scan_folder`, `browse`,
   `test_connection`.
4. Job: `build_command` (settings → immich-go arguments), `Job` (runs the
   child process and buffers its log), `eject_for`.
5. HTTP: `Handler` with the JSON API under `/api/…`.
6. `PAGE`: the whole web page (HTML, CSS, JS) as one string.
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
- **Config file** is written with mode 600 and holds the API key. Never write
  it inside the project folder; `.gitignore` excludes `config.json` as a
  backstop.
- **Tray mode restarts itself** (`relaunch_for_tray`): Cocoa treats a file path in the
  process arguments as a document to open, so `python my_immich.py --tray` made macOS
  open my_immich.py in Xcode. Tray mode re-executes Python with `-c` and passes its
  options in `MY_IMMICH_*` environment variables. Do not add file paths to that argv.
- Card detection means "a directory under the volumes root that contains
  `DCIM`"; the import source for a card is its `DCIM` folder.
- Do not commit or push unless asked.

## Not yet verified on macOS

`run_tray`, notifications, the `.app` bundle from `install.sh`, the Dock-icon
suppression and `diskutil eject` were written without a Mac to run them on.
Treat reports about them as likely real bugs.

## Ideas not built

- Verify every file on the card exists in Immich, then offer to wipe it.
- Auto-start an import when a known card is inserted.
- An "organise last import" step that proposes albums for a tagged batch.
- Per-camera profiles.
