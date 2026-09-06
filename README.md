# Steam Yoink

**Lift a working non-Steam game out of Steam and run it standalone — no Steam client in the loop.**

Steam Yoink is a small PySide6 desktop app. You get a non-Steam game working
in Steam the easy way (Steam's runtime + Proton auto-config do the hard part),
then Steam Yoink **copies the working prefix and Proton container out of Steam**,
writes a correct `launch.sh`, and installs a desktop entry. The result runs
under [`umu-run`](https://github.com/Open-Wine-Components/umu-launcher) with the
same "just works" behaviour Steam gave you — Steam no longer needs to be running
or even installed.

---

## Why this exists

Proton "just works" in Steam not because of the Wine build, but because of
everything Steam wraps around it: the Steam Linux Runtime container plus a pile
of auto-configuration. `umu-run` reproduces that exact container outside Steam —
it's essentially Steam's `sniper` runtime with the entry point renamed — so a
prefix that works in Steam works identically under umu.

The catch is the plumbing: finding the right prefix in `compatdata`, copying it
**without following the `dosdevices/z:` → `/` symlink** (a naive copy will try to
duplicate your entire filesystem), matching the Proton version, and getting the
shell quoting right in a launch script. Steam Yoink does all of that for you.

## The intended workflow

This tool is step 2. **Do step 1 in Steam first.**

1. **Install & verify in Steam.** Add the game (or its launcher — Battle.net,
   EA App, etc.) to Steam as a non-Steam game, pick a Proton version in its
   properties, and get it fully working. This is the easiest place to reach a
   known-good state.
2. **Yoink it out.** Open Steam Yoink. It copies the working prefix and Proton
   out of Steam, generates `launch.sh`, and installs a menu entry. Launch it
   from your desktop like any native app.

## Features

- Guided wizard: name → prefix → Proton → executable → desktop entry.
- **Symlink-safe prefix copy** — recreates `dosdevices` links instead of
  following them (there's a test for exactly the `z:` → `/` case).
- Auto-discovers Steam prefixes (sorted most-recently-used first) and installed
  Proton builds across native and Flatpak Steam locations.
- Generates a **path-relative `launch.sh`** — rename or move a game folder and
  the script still works untouched.
- Installs, validates, and refreshes `.desktop` entries.
- Manage multiple games: edit, rename, change the launch exe, reinstall the
  entry, or remove (entry only, or entry + files).

## Requirements

| Need | Package (Arch) | Notes |
| --- | --- | --- |
| Python 3 | `python` | |
| Qt bindings | `pyside6` | `extra` repo |
| The launcher | `umu-launcher` | `multilib` repo — **enable `[multilib]`** |
| Desktop tooling *(optional)* | `desktop-file-utils` | validate + refresh entries (pulled in by umu-launcher anyway) |
| Folder opening *(optional)* | `xdg-utils` | the app's "Open folder" button |

`umu-run` downloads the Steam runtime it needs on first launch (into
`~/.local/share/umu`). Steam itself is **not** a runtime dependency.

## Installation

### Arch Linux (PKGBUILD)

```bash
git clone https://github.com/dvrlabs/steam-yoink.git
cd steam-yoink
makepkg -si
```

`makepkg -si` builds the package and pulls `python`, `pyside6`, and
`umu-launcher` via pacman. The included `PKGBUILD` is a VCS (`-git`) build that
tracks `main`; once you cut tagged releases you can switch it to a versioned
source tarball with real checksums.

### Any distro (run directly)

```bash
# Debian/Ubuntu: sudo apt install python3-pyside6 umu-launcher
# Fedora:        sudo dnf install python3-pyside6 umu-launcher
# or, portably:
pip install --user PySide6

python3 steam_yoink.py
```

## Usage

1. On first run, pick a **Games directory** — each game gets its own subfolder
   here. This is saved to `~/.config/steam-yoink/config.json`.
2. **Add game** → give it a name.
3. **Pick the prefix.** Launch your game once from Steam, then come back: the
   most-recently-used prefix sorts to the top. (Manual browse is there too.)
4. **Pick Proton** — ideally the same version the prefix was built with. Choose
   whether to copy it in (self-contained) or reference it in place.
5. **Pick the `.exe`** inside the prefix to launch.
6. **Desktop entry** — name, icon, categories.
7. Done. Find it in your application menu.

## What it creates

```
<Games>/<Game>/
├── pfx/          copied Wine prefix (symlinks preserved)
├── proton/       copied Proton container (optional — can reference in place)
└── launch.sh     path-relative, so renaming the folder won't break it
```

The `.desktop` entry is installed to `~/.local/share/applications/`.

### The generated `launch.sh`

Paths resolve from the script's own location, and everything is quoted, so it
survives spaces and folder renames:

```bash
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GAMEID="0"
WINEPREFIX="$SCRIPT_DIR/pfx"
PROTONPATH="$SCRIPT_DIR/proton"
EXE="$WINEPREFIX/drive_c/Program Files (x86)/Battle.net/Battle.net Launcher.exe"

exec env \
  GAMEID="$GAMEID" \
  WINEPREFIX="$WINEPREFIX" \
  PROTONPATH="$PROTONPATH" \
  umu-run "$EXE" "$@"
```

## Notes & limitations

- **Non-Steam games show as numeric app IDs**, not friendly names. Steam stores
  shortcut names in a binary `shortcuts.vdf` that's fiddly to parse reliably, so
  the "launch it, then it's at the top" flow is the intended way to find yours.
- **Proton version matters.** A prefix built by one Proton and run under a
  different one triggers a prefix upgrade on first launch, which occasionally
  breaks things. Match it where you can.
- Debugging a launch? Run `steam-yoink` games with `UMU_LOG=debug ./launch.sh`
  to see what Proton and the runtime are doing.

## Contributing

Issues and PRs welcome. The whole app is a single file (`steam_yoink.py`) with
the pure logic (copy, script/desktop generation, discovery) kept free of Qt so
it's easy to test.

## License

MIT — see [LICENSE](LICENSE).
