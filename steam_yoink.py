#!/usr/bin/env python3
"""
umu-game-setup — lift a working non-Steam game out of Steam and run it standalone.

Workflow this tool automates (see Help -> About in the app):
  1. Pick a Games directory (stored in config after first run).
  2. Create a named Game folder under it (e.g. "Epic Games").
  3. Copy a Wine prefix (pfx) out of Steam's compatdata into that folder.
  4. Copy the matching Proton container out of Steam.
  5. Generate a launch.sh (correctly quoted, path-relative so the folder is
     rename-safe) that invokes umu-run.
  6. Generate and install a .desktop entry into ~/.local/share/applications.
  7. Manage / edit / rename / remove games afterwards.

Requires: PySide6, and umu-run on PATH at run time. desktop-file-utils
(desktop-file-validate, update-desktop-database) is used opportunistically if
present.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QObject, QSize
from PySide6.QtGui import QAction, QFont, QPalette, QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QMessageBox,
    QDialog,
    QDialogButtonBox,
    QWizard,
    QWizardPage,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QStackedWidget,
    QGroupBox,
    QAbstractItemView,
    QSizePolicy,
)

APP_NAME = "steam-yoink"
APP_TITLE = "Steam-Yoink"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
APPLICATIONS_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    / "applications"
)
PROTON_SUBDIR = "proton"  # normalized name we copy Proton into, inside each game folder


# --------------------------------------------------------------------------- #
#  Pure logic (no Qt) — kept importable/testable without a display             #
# --------------------------------------------------------------------------- #


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def steam_roots() -> list[Path]:
    """Candidate Steam library roots that actually exist."""
    home = Path.home()
    candidates = [
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".steam" / "root",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",  # flatpak
    ]
    seen, roots = set(), []
    for c in candidates:
        try:
            rp = c.resolve()
        except OSError:
            continue
        if rp in seen or not c.is_dir():
            continue
        seen.add(rp)
        roots.append(c)
    return roots


def find_prefixes() -> list[dict]:
    """All <root>/steamapps/compatdata/<appid>/pfx directories."""
    out = []
    for root in steam_roots():
        compatdata = root / "steamapps" / "compatdata"
        if not compatdata.is_dir():
            continue
        for entry in sorted(compatdata.iterdir()):
            pfx = entry / "pfx"
            if pfx.is_dir():
                try:
                    mtime = pfx.stat().st_mtime
                except OSError:
                    mtime = 0
                out.append(
                    {
                        "appid": entry.name,
                        "pfx": str(pfx),
                        "compatdata": str(entry),
                        "mtime": mtime,
                        "root": str(root),
                    }
                )
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def _is_proton_dir(p: Path) -> bool:
    return (p / "proton").is_file() and (
        (p / "files").is_dir() or (p / "dist").is_dir()
    )


def find_protons() -> list[dict]:
    """Directories that look like a usable Proton (have a `proton` script)."""
    out, seen = [], set()
    for root in steam_roots():
        search = [
            root / "steamapps" / "common",
            root / "compatibilitytools.d",
        ]
        # GE and friends commonly live here too:
        search.append(
            Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d"
        )
        for base in search:
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if entry.is_dir() and _is_proton_dir(entry):
                    rp = str(entry.resolve())
                    if rp in seen:
                        continue
                    seen.add(rp)
                    out.append({"name": entry.name, "path": str(entry)})
    return out


def count_entries(src: str | os.PathLike) -> int:
    total, stack = 0, [os.fspath(src)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    total += 1
                    if e.is_dir(follow_symlinks=False) and not e.is_symlink():
                        stack.append(e.path)
        except (PermissionError, FileNotFoundError):
            pass
    return total


def copy_tree_symlink_safe(src, dst, on_progress=None, should_cancel=None) -> int:
    """
    Recursive copy that RECREATES symlinks instead of following them.

    This is the critical safety property for Wine prefixes: dosdevices contains
    links like `z:` -> `/`. A naive follow-the-link copy would try to duplicate
    the entire filesystem. We never descend through a symlinked directory.
    """
    src, dst = os.fspath(src), os.fspath(dst)
    os.makedirs(dst, exist_ok=True)
    counter = [0]

    def _recurse(s, d):
        with os.scandir(s) as it:
            for e in it:
                if should_cancel and should_cancel():
                    raise InterruptedError("Canceled by user")
                target = os.path.join(d, e.name)
                if e.is_symlink():
                    link = os.readlink(e.path)
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(link, target)
                elif e.is_dir(follow_symlinks=False):
                    os.makedirs(target, exist_ok=True)
                    _recurse(e.path, target)
                else:
                    shutil.copy2(e.path, target, follow_symlinks=False)
                counter[0] += 1
                if on_progress:
                    on_progress(counter[0])

    _recurse(src, dst)
    return counter[0]


def render_launch_sh(
    gameid: str, proton_path_expr: str, exe_rel: str, extra_args: str = ""
) -> str:
    """
    proton_path_expr is either '"$SCRIPT_DIR/proton"' (copied) or a quoted
    absolute path (referenced). exe_rel is relative to the prefix root.
    """
    gameid = gameid.strip() or "0"
    extra = f" {extra_args.strip()}" if extra_args.strip() else ""
    return f"""#!/usr/bin/env bash
# Generated by {APP_TITLE}. Safe to edit by hand.
# Paths resolve relative to this script, so the game folder can be renamed or
# moved without editing this file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

GAMEID="{gameid}"
WINEPREFIX="$SCRIPT_DIR/pfx"
PROTONPATH={proton_path_expr}
EXE="$WINEPREFIX/{exe_rel}"

exec env \\
  GAMEID="$GAMEID" \\
  WINEPREFIX="$WINEPREFIX" \\
  PROTONPATH="$PROTONPATH" \\
  umu-run "$EXE"{extra} "$@"
"""


def render_desktop(
    display_name: str,
    launch_sh: str,
    game_dir: str,
    icon: str = "",
    comment: str = "",
    categories: str = "Game;",
    terminal: bool = False,
) -> str:
    cats = categories.strip()
    if cats and not cats.endswith(";"):
        cats += ";"
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={display_name}",
    ]
    if comment.strip():
        lines.append(f"Comment={comment.strip()}")
    # Exec is not run through a shell; quote the path so spaces survive.
    lines.append(f'Exec="{launch_sh}"')
    lines.append(f"Path={game_dir}")
    if icon.strip():
        lines.append(f"Icon={icon.strip()}")
    lines.append(f"Terminal={'true' if terminal else 'false'}")
    lines.append(f"Categories={cats or 'Game;'}")
    lines.append("StartupNotify=true")
    return "\n".join(lines) + "\n"


def desktop_filename(folder: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in folder.lower())
    return f"umu-{safe}.desktop"


def which(cmd: str) -> str | None:
    return shutil.which(cmd)


def validate_desktop(path: Path) -> tuple[bool, str]:
    tool = which("desktop-file-validate")
    if not tool:
        return True, "desktop-file-validate not found (skipped validation)."
    r = subprocess.run([tool, str(path)], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def refresh_desktop_db() -> str:
    tool = which("update-desktop-database")
    if not tool:
        return "update-desktop-database not found (skipped cache refresh)."
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([tool, str(APPLICATIONS_DIR)], capture_output=True, text=True)
    return "Desktop database refreshed."


# --------------------------------------------------------------------------- #
#  Config / registry                                                          #
# --------------------------------------------------------------------------- #


@dataclass
class Game:
    folder: str  # directory name under games_dir
    display_name: str
    exe_rel: str  # relative to pfx root
    gameid: str = "0"
    extra_args: str = ""
    proton_copied: bool = True  # True: <folder>/proton ; False: reference abs path
    proton_ref: str = ""  # absolute path when not copied
    icon: str = ""
    comment: str = ""
    categories: str = "Game;"
    terminal: bool = False


class Config:
    def __init__(self):
        self.games_dir: str = ""
        self.games: list[Game] = []
        self.load()

    def load(self):
        if CONFIG_PATH.is_file():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                self.games_dir = data.get("games_dir", "")
                self.games = [Game(**g) for g in data.get("games", [])]
            except (json.JSONDecodeError, TypeError):
                pass

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(
                {"games_dir": self.games_dir, "games": [asdict(g) for g in self.games]},
                indent=2,
            )
        )

    def game_dir(self, g: Game) -> Path:
        return Path(self.games_dir) / g.folder

    def by_folder(self, folder: str) -> Game | None:
        return next((g for g in self.games if g.folder == folder), None)


# --------------------------------------------------------------------------- #
#  Background install worker                                                   #
# --------------------------------------------------------------------------- #


class InstallWorker(QThread):
    progress = Signal(int, int)  # done, total
    status = Signal(str)
    finished_ok = Signal(str)  # desktop path
    failed = Signal(str)

    def __init__(
        self,
        cfg: Config,
        game: Game,
        pfx_src: str,
        proton_src: str | None,
        extra_files_src: str | None,
    ):
        super().__init__()
        self.cfg, self.game = cfg, game
        self.pfx_src = pfx_src
        self.proton_src = proton_src
        self.extra_files_src = extra_files_src
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _cancelled(self):
        return self._cancel

    def run(self):
        try:
            gdir = self.cfg.game_dir(self.game)
            gdir.mkdir(parents=True, exist_ok=True)

            # 1. count for a determinate progress bar
            self.status.emit("Scanning source files…")
            total = count_entries(self.pfx_src)
            if self.proton_src:
                total += count_entries(self.proton_src)
            if self.extra_files_src:
                total += count_entries(self.extra_files_src)
            total = max(total, 1)
            done = 0

            def bump(local_done, base):
                self.progress.emit(base + local_done, total)

            # 2. copy prefix (symlink-safe)
            self.status.emit("Copying Wine prefix (preserving symlinks)…")
            n = copy_tree_symlink_safe(
                self.pfx_src,
                gdir / "pfx",
                on_progress=lambda c: bump(c, done),
                should_cancel=self._cancelled,
            )
            done += n

            # 3. copy Proton if requested
            if self.proton_src:
                self.status.emit("Copying Proton container…")
                n = copy_tree_symlink_safe(
                    self.proton_src,
                    gdir / PROTON_SUBDIR,
                    on_progress=lambda c: bump(c, done),
                    should_cancel=self._cancelled,
                )
                done += n

            # 4. optional external game files
            if self.extra_files_src:
                self.status.emit("Copying game files…")
                n = copy_tree_symlink_safe(
                    self.extra_files_src,
                    gdir / "gamefiles",
                    on_progress=lambda c: bump(c, done),
                    should_cancel=self._cancelled,
                )
                done += n

            # 5. write launch.sh + .desktop
            self.status.emit("Writing launch script and desktop entry…")
            write_game_files(self.cfg, self.game)

            ok, msg = validate_desktop(
                APPLICATIONS_DIR / desktop_filename(self.game.folder)
            )
            refresh_desktop_db()
            desktop_path = str(APPLICATIONS_DIR / desktop_filename(self.game.folder))
            if not ok:
                self.status.emit(
                    "Installed (desktop-file-validate reported: " + msg + ")"
                )
            self.finished_ok.emit(desktop_path)
        except InterruptedError:
            # best-effort cleanup of a partial copy
            try:
                shutil.rmtree(self.cfg.game_dir(self.game), ignore_errors=True)
            except OSError:
                pass
            self.failed.emit("Cancelled. Partial files were removed.")
        except Exception as exc:  # noqa: BLE001  (surface any failure to the UI)
            self.failed.emit(str(exc))


def proton_path_expr(game: Game) -> str:
    if game.proton_copied:
        return '"$SCRIPT_DIR/proton"'
    return f'"{game.proton_ref}"'


def write_game_files(cfg: Config, game: Game):
    """(Re)write launch.sh and the installed .desktop entry from game metadata."""
    gdir = cfg.game_dir(game)
    gdir.mkdir(parents=True, exist_ok=True)

    launch = gdir / "launch.sh"
    launch.write_text(
        render_launch_sh(
            game.gameid,
            proton_path_expr(game),
            game.exe_rel,
            game.extra_args,
        )
    )
    launch.chmod(launch.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    desktop = APPLICATIONS_DIR / desktop_filename(game.folder)
    desktop.write_text(
        render_desktop(
            game.display_name,
            str(launch),
            str(gdir),
            icon=game.icon,
            comment=game.comment,
            categories=game.categories,
            terminal=game.terminal,
        )
    )


def remove_game(cfg: Config, game: Game, delete_files: bool):
    desktop = APPLICATIONS_DIR / desktop_filename(game.folder)
    if desktop.exists():
        desktop.unlink()
    refresh_desktop_db()
    if delete_files:
        shutil.rmtree(cfg.game_dir(game), ignore_errors=True)
    cfg.games = [g for g in cfg.games if g.folder != game.folder]
    cfg.save()


# --------------------------------------------------------------------------- #
#  Styling                                                                     #
# --------------------------------------------------------------------------- #


def build_tokens(dark: bool) -> dict:
    """Two hand-tuned palettes, each with adequate contrast on its background.

    We follow the system light/dark preference rather than forcing one theme, so
    the app looks at home on any desktop (COSMIC, GNOME, KDE) in either mode.
    """
    if dark:
        return {
            "BG": "#16181d",
            "SURFACE": "#1e2128",
            "SURFACE_2": "#262a33",
            "BORDER": "#373d47",
            "TEXT": "#e8eaed",
            "MUTED": "#a3aab5",
            "ACCENT": "#4db8c6",
            "ACCENT_HOVER": "#63c6d3",
            "ON_ACCENT": "#0c1013",
            "DANGER": "#e56a6f",
            "ON_DANGER": "#160c0c",
        }
    return {
        "BG": "#f4f5f7",
        "SURFACE": "#ffffff",
        "SURFACE_2": "#e9ebef",
        "BORDER": "#cfd4db",
        "TEXT": "#1b1e24",
        "MUTED": "#59606b",
        "ACCENT": "#0a7480",
        "ACCENT_HOVER": "#08606b",
        "ON_ACCENT": "#ffffff",
        "DANGER": "#c62f36",
        "ON_DANGER": "#ffffff",
    }


def stylesheet(t: dict) -> str:
    return f"""
* {{ font-size: 14px; }}
QMainWindow, QDialog, QWizard {{ background: {t["BG"]}; color: {t["TEXT"]}; }}
QWidget {{ color: {t["TEXT"]}; }}
QLabel[muted="true"] {{ color: {t["MUTED"]}; }}
QLabel[h1="true"] {{ font-size: 20px; font-weight: 600; }}
QLabel[h2="true"] {{ font-size: 15px; font-weight: 600; }}

QLineEdit, QComboBox, QPlainTextEdit, QListWidget, QTableWidget {{
    background: {t["SURFACE"]}; border: 1px solid {t["BORDER"]};
    border-radius: 8px; padding: 7px 9px;
    selection-background-color: {t["ACCENT"]}; selection-color: {t["ON_ACCENT"]};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {t["ACCENT"]}; }}
QPlainTextEdit {{ font-family: monospace; }}

QPushButton {{
    background: {t["SURFACE_2"]}; border: 1px solid {t["BORDER"]};
    border-radius: 8px; padding: 8px 14px;
}}
QPushButton:hover {{ border: 1px solid {t["ACCENT"]}; }}
QPushButton:disabled {{ color: {t["MUTED"]}; }}
QPushButton[accent="true"] {{
    background: {t["ACCENT"]}; color: {t["ON_ACCENT"]}; border: none; font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background: {t["ACCENT_HOVER"]}; }}
QPushButton[danger="true"] {{ border: 1px solid {t["DANGER"]}; color: {t["DANGER"]}; }}
QPushButton[danger="true"]:hover {{ background: {t["DANGER"]}; color: {t["ON_DANGER"]}; }}

QListWidget {{ padding: 4px; }}
QListWidget::item {{ padding: 10px 10px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {t["ACCENT"]}; color: {t["ON_ACCENT"]}; }}
QListWidget::item:hover {{ background: {t["SURFACE_2"]}; }}

QTableWidget {{ gridline-color: {t["BORDER"]}; }}
QHeaderView::section {{
    background: {t["SURFACE_2"]}; color: {t["MUTED"]}; border: none;
    border-bottom: 1px solid {t["BORDER"]}; padding: 8px;
}}
QTableWidget::item:selected {{ background: {t["ACCENT"]}; color: {t["ON_ACCENT"]}; }}

QProgressBar {{
    background: {t["SURFACE"]}; border: 1px solid {t["BORDER"]};
    border-radius: 8px; text-align: center; height: 20px;
}}
QProgressBar::chunk {{ background: {t["ACCENT"]}; border-radius: 7px; }}

QGroupBox {{
    border: 1px solid {t["BORDER"]}; border-radius: 10px;
    margin-top: 14px; padding: 12px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; color: {t["MUTED"]}; }}

QMenuBar {{ background: {t["BG"]}; }}
QMenuBar::item:selected {{ background: {t["SURFACE_2"]}; }}
QMenu {{ background: {t["SURFACE"]}; border: 1px solid {t["BORDER"]}; }}
QMenu::item:selected {{ background: {t["ACCENT"]}; color: {t["ON_ACCENT"]}; }}
QCheckBox {{ spacing: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; }}
QScrollBar::handle:vertical {{ background: {t["BORDER"]}; border-radius: 6px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def build_palette(t: dict) -> QPalette:
    """A QPalette so native bits not covered by the stylesheet (dialog chrome,
    the wizard banner, message boxes) also match the chosen theme."""

    def c(x):
        return QColor(x)

    p = QPalette()
    p.setColor(QPalette.Window, c(t["BG"]))
    p.setColor(QPalette.WindowText, c(t["TEXT"]))
    p.setColor(QPalette.Base, c(t["SURFACE"]))
    p.setColor(QPalette.AlternateBase, c(t["SURFACE_2"]))
    p.setColor(QPalette.Text, c(t["TEXT"]))
    p.setColor(QPalette.Button, c(t["SURFACE_2"]))
    p.setColor(QPalette.ButtonText, c(t["TEXT"]))
    p.setColor(QPalette.BrightText, c(t["ON_DANGER"]))
    p.setColor(QPalette.Highlight, c(t["ACCENT"]))
    p.setColor(QPalette.HighlightedText, c(t["ON_ACCENT"]))
    p.setColor(QPalette.ToolTipBase, c(t["SURFACE"]))
    p.setColor(QPalette.ToolTipText, c(t["TEXT"]))
    p.setColor(QPalette.PlaceholderText, c(t["MUTED"]))
    p.setColor(QPalette.Link, c(t["ACCENT"]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c(t["MUTED"]))
    return p


def detect_dark(app) -> bool:
    """True if the system prefers dark. Falls back to palette lightness on
    Qt versions or platforms that don't report a colour scheme."""
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except (AttributeError, TypeError):
        pass
    return app.palette().color(QPalette.Window).lightness() < 128


def apply_theme(app):
    t = build_tokens(detect_dark(app))
    app.setPalette(build_palette(t))
    app.setStyleSheet(stylesheet(t))


def make_label(text, *, h1=False, h2=False, muted=False, wrap=False) -> QLabel:
    lbl = QLabel(text)
    if h1:
        lbl.setProperty("h1", True)
    if h2:
        lbl.setProperty("h2", True)
    if muted:
        lbl.setProperty("muted", True)
    lbl.setWordWrap(wrap)
    return lbl


def accent_button(text) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("accent", True)
    b.setCursor(Qt.PointingHandCursor)
    return b


# --------------------------------------------------------------------------- #
#  Reusable pickers                                                            #
# --------------------------------------------------------------------------- #


class PrefixPicker(QWidget):
    """Table of discovered Steam prefixes + manual browse."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(
            make_label(
                "Pick the prefix Steam built for your game. Launch it once from "
                "Steam first — the most recently used prefix sorts to the top.",
                muted=True,
                wrap=True,
            )
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["App ID", "Last used", "Path"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Rescan")
        self.browse_btn = QPushButton("Browse manually…")
        self.chosen = QLineEdit()
        self.chosen.setPlaceholderText("No prefix selected")
        self.chosen.setReadOnly(True)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.browse_btn)
        row.addWidget(self.chosen, 1)
        lay.addLayout(row)

        self.refresh_btn.clicked.connect(self.populate)
        self.browse_btn.clicked.connect(self._browse)
        self.table.itemSelectionChanged.connect(self._from_table)
        self.populate()

    def populate(self):
        import datetime

        rows = find_prefixes()
        self.table.setRowCount(0)
        for d in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            when = "—"
            if d["mtime"]:
                when = datetime.datetime.fromtimestamp(d["mtime"]).strftime(
                    "%Y-%m-%d %H:%M"
                )
            self.table.setItem(r, 0, QTableWidgetItem(d["appid"]))
            self.table.setItem(r, 1, QTableWidgetItem(when))
            self.table.setItem(r, 2, QTableWidgetItem(d["pfx"]))

    def _from_table(self):
        items = self.table.selectedItems()
        if items:
            self.chosen.setText(self.table.item(items[0].row(), 2).text())

    def _browse(self):
        start = str(steam_roots()[0]) if steam_roots() else str(Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "Pick a Wine prefix (the pfx folder)", start
        )
        if d:
            self.chosen.setText(d)

    def value(self) -> str:
        return self.chosen.text().strip()


class ProtonPicker(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(
            make_label(
                "Pick the Proton the prefix was built with. A mismatched version "
                "triggers a prefix upgrade on first launch and can cause issues.",
                muted=True,
                wrap=True,
            )
        )

        self.list = QListWidget()
        lay.addWidget(self.list)
        for p in find_protons():
            it = QListWidgetItem(f"{p['name']}")
            it.setData(Qt.UserRole, p["path"])
            it.setToolTip(p["path"])
            self.list.addItem(it)

        row = QHBoxLayout()
        self.browse_btn = QPushButton("Browse manually…")
        self.copy_chk = QCheckBox("Copy Proton into the game folder (self-contained)")
        self.copy_chk.setChecked(True)
        self.copy_chk.setToolTip(
            "On: duplicates Proton next to the prefix (more disk, fully portable).\n"
            "Off: references the Proton where it already lives."
        )
        row.addWidget(self.browse_btn)
        row.addStretch(1)
        row.addWidget(self.copy_chk)
        lay.addLayout(row)

        self.manual = QLineEdit()
        self.manual.setPlaceholderText("…or a Proton path you browsed to")
        self.manual.setReadOnly(True)
        lay.addWidget(self.manual)
        self.browse_btn.clicked.connect(self._browse)

    def _browse(self):
        start = (
            str(steam_roots()[0] / "steamapps" / "common")
            if steam_roots()
            else str(Path.home())
        )
        d = QFileDialog.getExistingDirectory(self, "Pick a Proton directory", start)
        if d:
            self.manual.setText(d)
            self.list.clearSelection()

    def value(self) -> tuple[str, bool]:
        """(proton_path, copy?)"""
        if self.manual.text().strip():
            return self.manual.text().strip(), self.copy_chk.isChecked()
        items = self.list.selectedItems()
        if items:
            return items[0].data(Qt.UserRole), self.copy_chk.isChecked()
        return "", self.copy_chk.isChecked()


# --------------------------------------------------------------------------- #
#  New-game wizard                                                             #
# --------------------------------------------------------------------------- #


class NewGameWizard(QWizard):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Add a game")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoDefaultButton, False)
        self.setMinimumSize(720, 560)

        self.addPage(self._name_page())
        self.addPage(self._prefix_page())
        self.addPage(self._proton_page())
        self.addPage(self._exe_page())
        self.addPage(self._desktop_page())

    # --- pages -------------------------------------------------------------- #
    def _name_page(self):
        page = QWizardPage()
        page.setTitle("Name the game")
        page.setSubTitle(
            "This becomes the folder under your Games directory and "
            "the name shown in your app menu."
        )
        lay = QFormLayout(page)
        self.display_edit = QLineEdit()
        self.display_edit.setPlaceholderText("Epic Games Launcher")
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Epic Games")
        lay.addRow("Display name", self.display_edit)
        lay.addRow("Folder name", self.folder_edit)
        hint = make_label("", muted=True, wrap=True)
        lay.addRow("", hint)

        def sync():
            if not self.folder_edit.isModified():
                self.folder_edit.setText(self.display_edit.text())
            f = self.folder_edit.text().strip()
            if f and self.cfg.by_folder(f):
                hint.setText(f"A game folder named “{f}” already exists.")
            else:
                hint.setText("")

        self.display_edit.textChanged.connect(sync)

        def complete():
            f = self.folder_edit.text().strip()
            return (
                bool(self.display_edit.text().strip())
                and bool(f)
                and not self.cfg.by_folder(f)
            )

        page.isComplete = complete  # type: ignore
        self.display_edit.textChanged.connect(page.completeChanged)
        self.folder_edit.textChanged.connect(page.completeChanged)
        return page

    def _prefix_page(self):
        page = QWizardPage()
        page.setTitle("Choose the Wine prefix")
        page.setSubTitle("Copied out of Steam into your game folder as pfx/.")
        lay = QVBoxLayout(page)
        self.prefix_picker = PrefixPicker()
        lay.addWidget(self.prefix_picker)
        page.isComplete = lambda: bool(self.prefix_picker.value())  # type: ignore
        self.prefix_picker.chosen.textChanged.connect(page.completeChanged)
        return page

    def _proton_page(self):
        page = QWizardPage()
        page.setTitle("Choose Proton")
        page.setSubTitle("The compatibility layer umu-run will use.")
        lay = QVBoxLayout(page)
        self.proton_picker = ProtonPicker()
        lay.addWidget(self.proton_picker)
        page.isComplete = lambda: bool(self.proton_picker.value()[0])  # type: ignore
        self.proton_picker.list.itemSelectionChanged.connect(page.completeChanged)
        self.proton_picker.manual.textChanged.connect(page.completeChanged)
        return page

    def _exe_page(self):
        page = QWizardPage()
        page.setTitle("Choose the launch executable")
        page.setSubTitle("The .exe inside the prefix that umu-run should start.")
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText(
            "drive_c/Program Files (x86)/Epic Games/Launcher/…/EpicGamesLauncher.exe"
        )
        self.exe_edit.setReadOnly(True)
        browse = QPushButton("Browse in prefix…")
        row.addWidget(self.exe_edit, 1)
        row.addWidget(browse)
        lay.addLayout(row)

        adv = QGroupBox("Optional")
        form = QFormLayout(adv)
        self.gameid_edit = QLineEdit("0")
        self.gameid_edit.setToolTip(
            "umu GAMEID — leave 0 unless the game has a known umu fix id."
        )
        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText("extra launch arguments (rare)")
        form.addRow("GAMEID", self.gameid_edit)
        form.addRow("Launch args", self.args_edit)
        lay.addWidget(adv)
        lay.addStretch(1)

        def browse_exe():
            base = Path(self.prefix_picker.value())
            start = str(base / "drive_c") if (base / "drive_c").is_dir() else str(base)
            f, _ = QFileDialog.getOpenFileName(
                self,
                "Pick the game .exe",
                start,
                "Windows executables (*.exe);;All files (*)",
            )
            if f:
                try:
                    rel = str(Path(f).relative_to(base))
                except ValueError:
                    QMessageBox.warning(
                        self,
                        "Outside the prefix",
                        "That file isn't inside the chosen prefix.",
                    )
                    return
                self.exe_edit.setText(rel)

        browse.clicked.connect(browse_exe)

        page.isComplete = lambda: bool(self.exe_edit.text().strip())  # type: ignore
        self.exe_edit.textChanged.connect(page.completeChanged)
        return page

    def _desktop_page(self):
        page = QWizardPage()
        page.setTitle("Desktop entry")
        page.setSubTitle("How the launcher appears in your desktop environment.")
        form = QFormLayout(page)
        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("Short description (optional)")
        self.cats_edit = QLineEdit("Game;")
        self.term_chk = QCheckBox("Run in a terminal (useful for debugging)")
        row = QHBoxLayout()
        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("Path to a PNG/SVG icon (optional)")
        icon_btn = QPushButton("Browse…")
        row.addWidget(self.icon_edit, 1)
        row.addWidget(icon_btn)
        icon_btn.clicked.connect(self._pick_icon)
        form.addRow("Comment", self.comment_edit)
        form.addRow("Icon", row)
        form.addRow("Categories", self.cats_edit)
        form.addRow("", self.term_chk)
        return page

    def _pick_icon(self):
        f, _ = QFileDialog.getOpenFileName(
            self,
            "Pick an icon",
            str(Path.home()),
            "Images (*.png *.svg *.ico *.xpm);;All files (*)",
        )
        if f:
            self.icon_edit.setText(f)

    # --- result ------------------------------------------------------------- #
    def build_game(self) -> tuple[Game, str, str | None]:
        proton_path, copy = self.proton_picker.value()
        game = Game(
            folder=self.folder_edit.text().strip(),
            display_name=self.display_edit.text().strip(),
            exe_rel=self.exe_edit.text().strip(),
            gameid=self.gameid_edit.text().strip() or "0",
            extra_args=self.args_edit.text().strip(),
            proton_copied=copy,
            proton_ref="" if copy else proton_path,
            icon=self.icon_edit.text().strip(),
            comment=self.comment_edit.text().strip(),
            categories=self.cats_edit.text().strip() or "Game;",
            terminal=self.term_chk.isChecked(),
        )
        proton_src = proton_path if copy else None
        return game, self.prefix_picker.value(), proton_src


# --------------------------------------------------------------------------- #
#  Progress dialog                                                            #
# --------------------------------------------------------------------------- #


class ProgressDialog(QDialog):
    def __init__(self, worker: InstallWorker, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.desktop_path = None
        self.error = None
        self.setWindowTitle("Installing")
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.addWidget(make_label("Setting up your game", h2=True))
        self.status = make_label("Starting…", muted=True, wrap=True)
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("danger", True)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)

        worker.progress.connect(self._progress)
        worker.status.connect(self.status.setText)
        worker.finished_ok.connect(self._ok)
        worker.failed.connect(self._fail)
        self.cancel_btn.clicked.connect(worker.cancel)
        worker.start()

    def _progress(self, done, total):
        self.bar.setValue(int(done * 100 / max(total, 1)))

    def _ok(self, path):
        self.desktop_path = path
        self.accept()

    def _fail(self, msg):
        self.error = msg
        self.reject()

    def closeEvent(self, e):
        if self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
#  Edit dialog                                                                 #
# --------------------------------------------------------------------------- #


class EditGameDialog(QDialog):
    def __init__(self, cfg: Config, game: Game, parent=None):
        super().__init__(parent)
        self.cfg, self.game = cfg, game
        self.setWindowTitle(f"Edit — {game.display_name}")
        self.setMinimumWidth(560)
        form = QFormLayout(self)

        self.display_edit = QLineEdit(game.display_name)
        self.exe_edit = QLineEdit(game.exe_rel)
        exe_row = QHBoxLayout()
        exe_row.addWidget(self.exe_edit, 1)
        exe_btn = QPushButton("Browse…")
        exe_row.addWidget(exe_btn)
        exe_btn.clicked.connect(self._browse_exe)
        self.gameid_edit = QLineEdit(game.gameid)
        self.args_edit = QLineEdit(game.extra_args)
        self.comment_edit = QLineEdit(game.comment)
        self.cats_edit = QLineEdit(game.categories)
        self.icon_edit = QLineEdit(game.icon)
        icon_row = QHBoxLayout()
        icon_row.addWidget(self.icon_edit, 1)
        icon_btn = QPushButton("Browse…")
        icon_row.addWidget(icon_btn)
        icon_btn.clicked.connect(self._browse_icon)
        self.term_chk = QCheckBox("Run in a terminal")
        self.term_chk.setChecked(game.terminal)

        form.addRow("Display name", self.display_edit)
        form.addRow("Launch exe", exe_row)
        form.addRow("GAMEID", self.gameid_edit)
        form.addRow("Launch args", self.args_edit)
        form.addRow("Comment", self.comment_edit)
        form.addRow("Categories", self.cats_edit)
        form.addRow("Icon", icon_row)
        form.addRow("", self.term_chk)

        prev = make_label("launch.sh preview", muted=True)
        form.addRow(prev)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(150)
        form.addRow(self.preview)
        for w in (self.exe_edit, self.gameid_edit, self.args_edit):
            w.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse_exe(self):
        base = self.cfg.game_dir(self.game) / "pfx"
        start = str(base / "drive_c") if (base / "drive_c").is_dir() else str(base)
        f, _ = QFileDialog.getOpenFileName(
            self,
            "Pick the game .exe",
            start,
            "Windows executables (*.exe);;All files (*)",
        )
        if f:
            try:
                self.exe_edit.setText(str(Path(f).relative_to(base)))
            except ValueError:
                QMessageBox.warning(
                    self,
                    "Outside the prefix",
                    "That file isn't inside this game's prefix.",
                )

    def _browse_icon(self):
        f, _ = QFileDialog.getOpenFileName(
            self,
            "Pick an icon",
            str(Path.home()),
            "Images (*.png *.svg *.ico *.xpm);;All files (*)",
        )
        if f:
            self.icon_edit.setText(f)

    def _refresh_preview(self):
        self.preview.setPlainText(
            render_launch_sh(
                self.gameid_edit.text(),
                proton_path_expr(self.game),
                self.exe_edit.text(),
                self.args_edit.text(),
            )
        )

    def apply(self):
        self.game.display_name = (
            self.display_edit.text().strip() or self.game.display_name
        )
        self.game.exe_rel = self.exe_edit.text().strip()
        self.game.gameid = self.gameid_edit.text().strip() or "0"
        self.game.extra_args = self.args_edit.text().strip()
        self.game.comment = self.comment_edit.text().strip()
        self.game.categories = self.cats_edit.text().strip() or "Game;"
        self.game.icon = self.icon_edit.text().strip()
        self.game.terminal = self.term_chk.isChecked()
        write_game_files(self.cfg, self.game)
        validate_desktop(APPLICATIONS_DIR / desktop_filename(self.game.folder))
        refresh_desktop_db()
        self.cfg.save()


# --------------------------------------------------------------------------- #
#  Main window                                                                 #
# --------------------------------------------------------------------------- #

ABOUT_TEXT = f"""<h2>{APP_TITLE}</h2>
<p>This tool lifts a <b>non-Steam</b> game out of Steam so it runs on its own,
launched by <code>umu-run</code> with no Steam client in the loop.</p>

<p><b>The intended workflow — do this first:</b></p>
<ol>
<li><b>Install &amp; test under Steam.</b> Add the game (or its launcher) to Steam
as a non-Steam game, set a Proton version in its properties, and get it fully
working. Steam's runtime container and auto-configuration are what make it
&ldquo;just work,&rdquo; so this is the easiest place to reach a known-good state.</li>
<li><b>Then lift it out.</b> Come back here. This tool copies the working prefix
and the Proton container out of Steam, writes a correctly-quoted
<code>launch.sh</code>, and installs a desktop entry — so you get the same
&ldquo;just works&rdquo; behaviour outside Steam.</li>
</ol>

<p><b>What it creates</b>, per game, under your Games directory:</p>
<pre>&lt;Games&gt;/&lt;Game&gt;/
├── pfx/          copied Wine prefix (symlinks preserved)
├── proton/       copied Proton container (optional)
└── launch.sh     path-relative, so renaming the folder won't break it</pre>

<p>The desktop entry is installed to
<code>~/.local/share/applications</code>. <code>umu-run</code> downloads the
Steam runtime it needs on first launch; Steam itself is not required.</p>
"""


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(APP_TITLE)
        self.resize(940, 620)
        self._build_menu()

        splitter = QSplitter()
        self.setCentralWidget(splitter)

        # left: game list + actions
        left = QWidget()
        lv = QVBoxLayout(left)
        header = QHBoxLayout()
        header.addWidget(make_label("Games", h2=True))
        header.addStretch(1)
        self.add_btn = accent_button("＋ Add game")
        header.addWidget(self.add_btn)
        lv.addLayout(header)
        self.list = QListWidget()
        lv.addWidget(self.list, 1)
        splitter.addWidget(left)

        # right: detail
        self.detail = QWidget()
        self.detail_lay = QVBoxLayout(self.detail)
        splitter.addWidget(self.detail)
        splitter.setSizes([300, 640])

        self.add_btn.clicked.connect(self.add_game)
        self.list.itemSelectionChanged.connect(self.show_detail)

        self.refresh_list()
        self.show_detail()

    # --- menu --------------------------------------------------------------- #
    def _build_menu(self):
        m = self.menuBar()
        filem = m.addMenu("&File")
        act_dir = QAction("Set Games directory…", self)
        act_dir.triggered.connect(self.change_games_dir)
        filem.addAction(act_dir)
        filem.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.close)
        filem.addAction(act_quit)

        helpm = m.addMenu("&Help")
        act_about = QAction("About", self)
        act_about.triggered.connect(self.show_about)
        helpm.addAction(act_about)
        act_qt = QAction("About Qt", self)
        act_qt.triggered.connect(QApplication.instance().aboutQt)
        helpm.addAction(act_qt)

    def show_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("About")
        dlg.setMinimumSize(560, 520)
        lay = QVBoxLayout(dlg)
        body = QLabel(ABOUT_TEXT)
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setAlignment(Qt.AlignTop)
        lay.addWidget(body, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        lay.addWidget(bb)
        dlg.exec()

    # --- games dir ---------------------------------------------------------- #
    def ensure_games_dir(self) -> bool:
        if self.cfg.games_dir and Path(self.cfg.games_dir).is_dir():
            return True
        return self.change_games_dir(first_run=True)

    def change_games_dir(self, first_run=False) -> bool:
        if first_run:
            QMessageBox.information(
                self,
                "Choose a Games directory",
                "Pick a folder where your standalone games will live. "
                "Each game gets its own subfolder here.",
            )
        start = self.cfg.games_dir or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose Games directory", start)
        if not d:
            return False
        self.cfg.games_dir = d
        self.cfg.save()
        self.refresh_list()
        self.show_detail()
        return True

    # --- list --------------------------------------------------------------- #
    def refresh_list(self):
        self.list.clear()
        for g in self.cfg.games:
            item = QListWidgetItem(g.display_name)
            item.setData(Qt.UserRole, g.folder)
            self.list.addItem(item)

    def selected_game(self) -> Game | None:
        items = self.list.selectedItems()
        if not items:
            return None
        return self.cfg.by_folder(items[0].data(Qt.UserRole))

    def _clear_detail(self):
        while self.detail_lay.count():
            item = self.detail_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def show_detail(self):
        self._clear_detail()
        g = self.selected_game()
        if not g:
            self.detail_lay.addStretch(1)
            msg = make_label(
                "No game selected."
                if self.cfg.games
                else "No games yet.\nAdd one to copy a working setup out of Steam.",
                muted=True,
            )
            msg.setAlignment(Qt.AlignCenter)
            self.detail_lay.addWidget(msg)
            if not self.cfg.games_dir:
                b = accent_button("Choose Games directory…")
                b.clicked.connect(lambda: self.change_games_dir())
                b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                wrap = QHBoxLayout()
                wrap.addStretch(1)
                wrap.addWidget(b)
                wrap.addStretch(1)
                self.detail_lay.addLayout(wrap)
            self.detail_lay.addStretch(1)
            return

        gdir = self.cfg.game_dir(g)
        self.detail_lay.addWidget(make_label(g.display_name, h1=True))
        self.detail_lay.addWidget(make_label(str(gdir), muted=True))

        info = QGroupBox("Configuration")
        form = QFormLayout(info)
        form.addRow("Executable", make_label(g.exe_rel or "—", muted=True))
        form.addRow("GAMEID", make_label(g.gameid, muted=True))
        proton_desc = (
            "copied (proton/)" if g.proton_copied else f"referenced: {g.proton_ref}"
        )
        form.addRow("Proton", make_label(proton_desc, muted=True))
        desktop = APPLICATIONS_DIR / desktop_filename(g.folder)
        form.addRow(
            "Desktop entry",
            make_label("installed" if desktop.exists() else "missing", muted=True),
        )
        self.detail_lay.addWidget(info)

        # actions
        grid = QHBoxLayout()
        edit = QPushButton("Edit…")
        rename = QPushButton("Rename folder…")
        reinstall = QPushButton("Reinstall desktop entry")
        open_btn = QPushButton("Open folder")
        remove = QPushButton("Remove…")
        remove.setProperty("danger", True)
        for b in (edit, rename, reinstall, open_btn, remove):
            grid.addWidget(b)
        self.detail_lay.addLayout(grid)
        self.detail_lay.addStretch(1)

        edit.clicked.connect(lambda: self.edit_game(g))
        rename.clicked.connect(lambda: self.rename_game(g))
        reinstall.clicked.connect(lambda: self.reinstall(g))
        open_btn.clicked.connect(lambda: self.open_folder(gdir))
        remove.clicked.connect(lambda: self.remove_game(g))

    # --- actions ------------------------------------------------------------ #
    def add_game(self):
        if not self.ensure_games_dir():
            return
        wiz = NewGameWizard(self.cfg, self)
        if wiz.exec() != QDialog.Accepted:
            return
        game, pfx_src, proton_src = wiz.build_game()
        worker = InstallWorker(self.cfg, game, pfx_src, proton_src, None)
        dlg = ProgressDialog(worker, self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg.games.append(game)
            self.cfg.save()
            self.refresh_list()
            QMessageBox.information(
                self,
                "Installed",
                f"“{game.display_name}” is installed.\n\n"
                "Find it in your desktop environment's application menu.",
            )
            # select the new game
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole) == game.folder:
                    self.list.setCurrentRow(i)
                    break
        elif dlg.error:
            QMessageBox.critical(self, "Install failed", dlg.error)

    def edit_game(self, g: Game):
        dlg = EditGameDialog(self.cfg, g, self)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply()
            self.refresh_list()
            self.show_detail()

    def rename_game(self, g: Game):
        from PySide6.QtWidgets import QInputDialog

        new, ok = QInputDialog.getText(
            self, "Rename folder", "New folder name:", text=g.folder
        )
        new = new.strip()
        if not ok or not new or new == g.folder:
            return
        if self.cfg.by_folder(new):
            QMessageBox.warning(self, "Name in use", f"“{new}” already exists.")
            return
        old_dir = self.cfg.game_dir(g)
        new_dir = Path(self.cfg.games_dir) / new
        try:
            old_dir.rename(new_dir)
        except OSError as e:
            QMessageBox.critical(self, "Rename failed", str(e))
            return
        # remove old desktop file, update metadata, rewrite (launch.sh is path-relative)
        old_desktop = APPLICATIONS_DIR / desktop_filename(g.folder)
        if old_desktop.exists():
            old_desktop.unlink()
        g.folder = new
        write_game_files(self.cfg, g)
        refresh_desktop_db()
        self.cfg.save()
        self.refresh_list()
        self.show_detail()

    def reinstall(self, g: Game):
        write_game_files(self.cfg, g)
        ok, msg = validate_desktop(APPLICATIONS_DIR / desktop_filename(g.folder))
        refresh_desktop_db()
        self.show_detail()
        QMessageBox.information(
            self,
            "Reinstalled",
            "Desktop entry rewritten." + ("" if ok else f"\n\n{msg}"),
        )

    def open_folder(self, path: Path):
        opener = which("xdg-open")
        if opener:
            subprocess.Popen([opener, str(path)])

    def remove_game(self, g: Game):
        box = QMessageBox(self)
        box.setWindowTitle("Remove game")
        box.setText(f"Remove “{g.display_name}”?")
        box.setInformativeText(
            "The desktop entry will be removed. "
            "Do you also want to delete the copied game files?"
        )
        del_btn = box.addButton("Remove + delete files", QMessageBox.DestructiveRole)
        entry_btn = box.addButton("Remove entry only", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == box.button(QMessageBox.Cancel):
            return
        remove_game(self.cfg, g, delete_files=(clicked == del_btn))
        self.refresh_list()
        self.show_detail()


# --------------------------------------------------------------------------- #
#  Entry point                                                                 #
# --------------------------------------------------------------------------- #


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")  # Fusion honours our QPalette on every desktop
    apply_theme(app)
    # follow the system if it flips light <-> dark while we're open
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _s: apply_theme(app))
    except (AttributeError, TypeError):
        pass

    cfg = Config()
    win = MainWindow(cfg)
    win.show()
    # first-run prompt (after the window paints)
    if not cfg.games_dir:
        win.ensure_games_dir()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
