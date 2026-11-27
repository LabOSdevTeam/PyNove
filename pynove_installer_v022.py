#!/usr/bin/env python3
"""PyNove Setup — standalone PyQt6 installer (Linux / Windows).

Run: python pynove_installer.py
Requires PyQt6 for the setup GUI. Downloads native Linux/Windows x64
release assets from LabOSdevTeam/PyNove. Local archives are optional.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import tarfile
import urllib.request
import urllib.error
import urllib.parse
import socket
import time
import http.client
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path, PurePosixPath

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QByteArray
from PyQt6.QtGui import QCloseEvent, QFont, QPainter, QPen, QColor, QPolygonF, QIcon, QPixmap
from PyQt6.QtCore import QPointF
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QStyle, QStyleOptionButton, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

APP = 'PyNove'
SETUP_VERSION = '0.2.2'
REPOSITORY = 'LabOSdevTeam/PyNove'
RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases'
LOGO_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 24 24">
  <path d="M12 2.2 21.8 12 12 21.8 2.2 12Z" fill="#292b2e" stroke="#a2c7f4" stroke-width="1.65" stroke-linejoin="round"/>
  <path d="M12 6.7 17.3 12 12 17.3 6.7 12Z" fill="#547eb1" stroke="#547eb1" stroke-width="0.6" stroke-linejoin="round"/>
</svg>"""

MARKER = '.pynove-install.json'
HERE = Path(__file__).resolve().parent
IGNORE = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', 'node_modules'}


def default_target() -> Path:
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'PyNove'
    return Path.home() / '.local' / 'opt' / 'PyNove'


def logo_pixmap(size=256):
    renderer = QSvgRenderer(QByteArray(LOGO_SVG))
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pix


def platform_key():
    system = platform.system().lower()
    arch = platform.machine().lower()
    if system not in ('linux', 'windows') or arch not in ('amd64', 'x86_64', 'x64'):
        raise RuntimeError(f'No supported release format for {system} / {arch}.')
    return system, 'x64'


def release_choices(releases, system, arch):
    suffix = 'tar.gz' if system == 'linux' else 'zip'
    pattern = re.compile(r'PyNove-(.+)-' + re.escape(system + '-' + arch) + r'-rls\.' + re.escape(suffix) + r'$', re.I)
    choices = []
    for release in releases:
        if release.get('draft'):
            continue
        for asset in release.get('assets', []):
            match = pattern.fullmatch(asset.get('name', ''))
            if not match or asset.get('state', 'uploaded') != 'uploaded':
                continue
            version = match.group(1)
            tag = release.get('tag_name', '')
            # The published asset filename gives the version; use the actual asset URL.
            if not asset.get('browser_download_url', '').startswith(f'https://github.com/{REPOSITORY}/releases/download/'):
                continue
            choices.append(dict(version=version, tag=tag, name=asset['name'],
                url=asset['browser_download_url'], size=asset.get('size', 0),
                digest=asset.get('digest') or '', prerelease=bool(release.get('prerelease')),
                published=release.get('published_at') or ''))
    def key(item):
        parts = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(.*)', item['version'])
        nums = tuple(map(int, parts.groups()[:3])) if parts else (0,0,0)
        stable = not item['prerelease'] and (not parts or not parts.group(4))
        return (stable, nums, item['published'])
    return sorted(choices, key=key, reverse=True)


def request(url):
    return urllib.request.Request(url, headers={'User-Agent': 'PyNove-Setup/'+SETUP_VERSION,
        'Accept': 'application/vnd.github+json' if url.startswith(RELEASE_API) else 'application/octet-stream'})


class NetworkFailure(RuntimeError):
    pass


def network_error(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code}: {exc.reason}', exc.code in (408, 429, 500, 502, 503, 504)
    if isinstance(reason, socket.gaierror):
        return 'DNS lookup failed. Check your internet connection, DNS or proxy.', True
    if isinstance(reason, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
        return f'Connection interrupted: {reason}', True
    return str(reason), False


class TrackingRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, url):
        self.host = urllib.parse.urlsplit(url).hostname

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.host = urllib.parse.urlsplit(newurl).hostname
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def network_retry(url, timeout, consume, report=lambda _: None):
    # Reopen the original GitHub URL on each attempt, so signed redirects are fresh.
    for attempt in range(1, 4):
        redirect = TrackingRedirect(url)
        try:
            opener = urllib.request.build_opener(redirect)
            with opener.open(request(url), timeout=timeout) as response:
                return consume(response)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            message, retry = network_error(exc)
            proxy = urllib.request.getproxies().get('https')
            proxy_host = urllib.parse.urlsplit(proxy if '://' in proxy else '//'+proxy).hostname if proxy else None
            detail = f'{redirect.host}: {message}' + (f' (HTTPS proxy: {proxy_host})' if proxy_host else '')
            if not retry or attempt == 3:
                raise NetworkFailure(detail + ' You can also select a local release archive.') from exc
            report(f'{detail} Retrying ({attempt + 1}/3)…')
            time.sleep(2 ** (attempt - 1))


class ReleaseWorker(QThread):
    result = pyqtSignal(object, str)

    def run(self):
        try:
            system, arch = platform_key()
            releases = []
            for page in range(1, 101):
                batch = network_retry(f'{RELEASE_API}?per_page=100&page={page}', 20, json.load)
                if not isinstance(batch, list):
                    raise RuntimeError('Unexpected GitHub response.')
                releases.extend(batch)
                if len(batch) < 100:
                    break
            self.result.emit(release_choices(releases, system, arch), '')
        except Exception as exc:
            self.result.emit([], str(exc))


def is_archive(path):
    return path.is_file() and (zipfile.is_zipfile(path) or tarfile.is_tarfile(path))


def validate(source: Path, target: Path) -> None:
    if not source.exists() or not (source.is_dir() or is_archive(source)):
        raise ValueError('Select a valid PyNove folder, ZIP or TAR.GZ archive.')
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise ValueError('Choose a dedicated PyNove installation folder.')
    if source.is_dir() and (target == source or source in target.parents or target in source.parents):
        raise ValueError('The source and installation folders must not contain each other.')
    if source.is_file() and target in source.parents:
        raise ValueError('Keep the ZIP outside the installation directory.')
    if target.exists():
        if not target.is_dir():
            raise ValueError('The installation path is not a directory.')
        if any(target.iterdir()) and not (target / MARKER).is_file():
            raise ValueError('This directory is not empty and was not installed by PyNove Setup. Choose another folder.')


def source_files(source: Path):
    for root, dirs, files in os.walk(source, followlinks=False):
        base = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in IGNORE and not (base / d).is_symlink())
        for name in sorted(files):
            f = base / name
            if name in (MARKER, 'pynove_installer.py', 'pynove_installer_v02.py') or f.is_symlink() or not f.is_file():
                continue
            yield f, f.relative_to(source)


def find_entry(root: Path) -> Path:
    """Find the bundled native launcher, not Python files in its dependencies."""
    candidates = []
    for p in root.rglob('*'):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(x.lower() in ('_internal', 'lib', 'libs', 'site-packages', '.venv', 'assets', 'resources') for x in rel.parts[:-1]):
            continue
        name = p.name.lower()
        stem = p.stem.lower()
        named = 'pynove' in name or stem in ('editor', 'main', 'launcher', 'app')
        if not named:
            continue
        with p.open('rb') as f:
            magic = f.read(4)
        if os.name == 'nt':
            valid = p.suffix.lower() == '.exe' and magic.startswith(b'MZ')
        else:
            valid = magic.startswith((b'\x7fELF', b'#!')) or (p.suffix.lower() == '.sh' and name.startswith('pynove'))
        if valid:
            score = 0 if name in ('pynove', 'pynove.exe') else 1 if 'pynove' in name else 2
            candidates.append((score, len(rel.parts), str(rel), rel))
    if not candidates:
        raise ValueError('No PyNove executable detected in the package. Check the release archive layout.')
    return min(candidates)[-1]


def write_launcher(target: Path, entry: Path) -> Path:
    # Keep the original executable and its adjacent bundled libraries together.
    binary = target / entry
    if os.name != 'nt':
        binary.chmod(binary.stat().st_mode | 0o111)
    return binary


def shortcut(target: Path, launcher: Path) -> str:
    if os.name == 'nt':
        # PowerShell resolves the real Desktop path (including OneDrive redirection).
        env = dict(os.environ, PYNOVE_LAUNCHER=str(launcher), PYNOVE_TARGET=str(launcher.parent))
        ps = ("$w=New-Object -ComObject WScript.Shell; "
              "$p=Join-Path ([Environment]::GetFolderPath('Desktop')) 'PyNove.lnk'; "
              "$s=$w.CreateShortcut($p); $s.TargetPath=$env:PYNOVE_LAUNCHER; "
              "$s.WorkingDirectory=$env:PYNOVE_TARGET; $s.Save()")
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
                       env=env, check=True, capture_output=True)
        return 'Desktop shortcut created.'
    app_dir = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'applications'
    app_dir.mkdir(parents=True, exist_ok=True)
    # Desktop Entry Exec quoting is not shell quoting; escape reserved characters.
    escaped = str(launcher).replace('\\', '\\\\\\\\').replace('"', '\\\\"').replace('$', '\\\\$').replace('`', '\\\\`').replace('%', '%%')
    (app_dir / 'pynove.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=PyNove\nComment=Visual novel editor\n'
        f'Exec="{escaped}"\nPath={launcher.parent}\nIcon={target / ".pynove-icon.svg"}\nTerminal=false\nCategories=Development;Graphics;\n', encoding='utf-8')
    return 'Application menu shortcut created.'


class InstallWorker(QThread):
    progress = pyqtSignal(int, str)
    log = pyqtSignal(str)
    result = pyqtSignal(bool, str, str)

    def __init__(self, source, target: Path, link: bool):
        super().__init__()
        self.source, self.target, self.link = source, target, link
        self.version = source['version'] if isinstance(source, dict) else 'local'

    def download(self, directory):
        asset = self.source
        output = directory / asset['name']
        def consume(response):
            sha = hashlib.sha256()
            total = asset.get('size', 0) or int(response.headers.get('Content-Length', 0))
            received = 0
            # Restart from byte zero after an interrupted transfer.
            with output.open('wb') as out:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    received += len(chunk)
                    pct = 5 + int(50 * received / total) if total else 5
                    self.progress.emit(min(pct, 55), f'Downloading {self.version} — {received / 1048576:.1f} / {total / 1048576:.1f} MiB')
            if total and received != total:
                raise ConnectionError(f'Download interrupted ({received} of {total} bytes).')
            return sha.hexdigest()

        def retry_notice(message):
            self.log.emit(message)
            self.progress.emit(2, message)

        actual_digest = network_retry(asset['url'], 60, consume, retry_notice)
        digest = asset.get('digest', '')
        if digest and digest.lower() != 'sha256:' + actual_digest:
            raise ValueError('SHA-256 verification failed. The archive was not installed.')
        self.log.emit('Download complete. ' + ('SHA-256 verified.' if digest else 'No checksum published by GitHub.'))
        return output

    def unpack(self, source, stage):
        if source.is_dir():
            files = list(source_files(source))
            for i, (src, rel) in enumerate(files, 1):
                out = stage / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, out)
                self.progress.emit(56 + int(25 * i / max(1, len(files))), f'Copying {rel.as_posix()}')
        elif zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as z:
                items = z.infolist()
                for i, item in enumerate(items, 1):
                    p = PurePosixPath(item.filename.replace('\\', '/'))
                    if p.is_absolute() or '..' in p.parts or ':' in str(p) or stat.S_ISLNK(item.external_attr >> 16):
                        raise ValueError(f'Unsafe ZIP entry: {item.filename}')
                    if not p.parts or p.name == MARKER:
                        continue
                    out = stage.joinpath(*p.parts)
                    if item.is_dir():
                        out.mkdir(parents=True, exist_ok=True)
                    else:
                        out.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(item) as src, out.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
                        mode = (item.external_attr >> 16) & 0o777
                        if mode and os.name != 'nt':
                            out.chmod(mode | 0o600)
                    self.progress.emit(56 + int(25 * i / max(1, len(items))), f'Extracting {p.as_posix()}')
        else:
            # Python's data filter blocks external links, path escapes and special files.
            if not hasattr(tarfile, 'data_filter'):
                raise RuntimeError('This TAR installer requires Python with tarfile.data_filter (Python 3.12+ or a security-backported release).')
            with tarfile.open(source, 'r:*') as tar:
                members = tar.getmembers()
                for i, member in enumerate(members, 1):
                    p = PurePosixPath(member.name)
                    if p.is_absolute() or '..' in p.parts or '\\' in member.name or ':' in member.name:
                        raise ValueError(f'Unsafe TAR entry: {member.name}')
                    if p.name == MARKER:
                        continue
                    tar.extract(member, stage, filter='data')
                    self.progress.emit(56 + int(25 * i / max(1, len(members))), f'Extracting {member.name}')
        # Flatten one packaging wrapper, but never move a symlink outside the archive root.
        children = list(stage.iterdir())
        if source.is_file() and len(children) == 1 and children[0].is_dir() and not children[0].is_symlink():
            wrapper = children[0]
            # Preserve the wrapper when it contains links to its own root or parent.
            if not any(p.is_symlink() for p in wrapper.rglob('*')):
                with tempfile.TemporaryDirectory(dir=stage.parent, prefix='.pynove-unpack-') as tmp:
                    moved = Path(tmp) / 'content'
                    wrapper.rename(moved)
                    for child in moved.iterdir():
                        child.rename(stage / child.name)

    def run(self):
        stage = backup = None
        download_dir = None
        installed = False
        committed = False
        warnings = []
        try:
            if isinstance(self.source, dict):
                download_dir = tempfile.TemporaryDirectory(prefix='pynove-download-')
                self.progress.emit(2, 'Downloading release…')
                self.log.emit(self.source['url'])
                source = self.download(Path(download_dir.name))
            else:
                source = self.source
            validate(source, self.target)
            self.target.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.pynove-stage-', dir=self.target.parent))
            self.progress.emit(56, 'Preparing installation…')
            self.log.emit(f'Source: {source}\nDestination: {self.target}')
            self.unpack(source, stage)
            entry = find_entry(stage)
            self.log.emit(f'Editor entry point: {entry}')
            self.progress.emit(83, 'Installing application files…')
            if self.target.exists():
                backup = Path(tempfile.mkdtemp(prefix='.pynove-backup-', dir=self.target.parent)) / 'previous'
                self.target.rename(backup)
            stage.rename(self.target)
            installed = True
            stage = None
            self.progress.emit(92, 'Creating launcher…')
            launcher = write_launcher(self.target, entry)
            icon_path = self.target / '.pynove-icon.svg'
            if icon_path.is_symlink():
                icon_path.unlink()
            icon_path.write_bytes(LOGO_SVG)
            if (self.target / MARKER).is_symlink():
                (self.target / MARKER).unlink()
            (self.target / MARKER).write_text(json.dumps({'setup_version': SETUP_VERSION, 'version': self.version, 'entry': entry.as_posix()}, indent=2), encoding='utf-8')
            committed = True
            if self.link:
                try:
                    self.log.emit(shortcut(self.target, launcher))
                except Exception as exc:
                    warnings.append(f'Could not create the shortcut: {exc}')
            if backup:
                try:
                    shutil.rmtree(backup.parent)
                except OSError as exc:
                    warnings.append(f'Previous installation backup remains at {backup}: {exc}')
            self.progress.emit(100, 'Installation complete')
            self.result.emit(True, str(launcher), '\n'.join(warnings))
        except Exception as exc:
            self.log.emit(traceback.format_exc())
            errors = [str(exc)]
            if not committed:
                try:
                    if installed and self.target.exists():
                        shutil.rmtree(self.target)
                    if backup and backup.exists():
                        backup.rename(self.target)
                        backup.parent.rmdir()
                except Exception as rollback:
                    errors.append(f'Rollback failed: {rollback}. Backup: {backup}')
            self.result.emit(False, '', '\n'.join(errors))
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if download_dir:
                download_dir.cleanup()


QSS = '''
QWidget { background: #202124; color: #e1e2e5; font-size: 13px; }
QWidget#window { background: #202124; }
QFrame#rail { background: #191a1d; border-right: 1px solid #303136; }
QFrame#header, QFrame#footer { background: #28292d; }
QLabel { background: transparent; }
QLabel#brand { font-size: 21px; font-weight: 700; color: #f0f1f3; }
QLabel#eyebrow { color: #7da9ef; font-size: 11px; font-weight: 700; }
QLabel#heading { font-size: 30px; font-weight: 650; color: #f4f5f7; }
QLabel#muted { color: #a1a3aa; }
QLabel#small { font-size: 11px; color: #858890; }
QLabel#field { font-weight: 600; color: #d8d9dd; }
QLabel#step { padding: 11px 12px; border-radius: 5px; color: #81848d; }
QLabel#step[active="true"] { background: #303b4e; color: #b9d3ff; }
QLabel#step[done="true"] { color: #acb4c2; }
QFrame#panel { background: #292a2e; border: 1px solid #36383e; border-radius: 6px; }
QFrame#panel QWidget { background: transparent; }
QLineEdit { background: #191a1d; border: 1px solid #41434a; border-radius: 4px; padding: 8px 10px; selection-background-color: #426ba5; }
QComboBox { background: #191a1d; border: 1px solid #41434a; border-radius: 4px; padding: 8px 10px; }
QComboBox QAbstractItemView { background: #28292d; color: #e1e2e5; selection-background-color: #396bb2; }
QLineEdit:focus { border: 1px solid #6b9de9; }
QPushButton { background: #393b41; border: 1px solid #484b52; border-radius: 4px; padding: 8px 15px; font-weight: 600; }
QPushButton:hover { background: #464951; }
QPushButton:pressed { background: #303238; }
QPushButton:disabled { color: #72747a; background: #2d2e32; border-color: #333439; }
QPushButton#primary { color: #ffffff; background: #396bb2; border-color: #5083c8; }
QPushButton#primary:hover { background: #467dc9; }
QPushButton#primary:disabled { background: #2e3b50; color: #8994a6; border-color: #394457; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 3px; background: #1b1c20; border: 1px solid #53565d; }
QCheckBox::indicator:checked { background: #5385cc; border-color: #85a9df;  }
QCheckBox:disabled { color: #777980; }
QProgressBar { background: #191a1d; border: 1px solid #3c3e44; border-radius: 4px; min-height: 9px; max-height: 9px; }
QProgressBar::chunk { background: #6393db; border-radius: 3px; }
QPlainTextEdit { background: #17181b; border: 1px solid #36383e; border-radius: 4px; padding: 8px; color: #aeb5c1; font-family: monospace; font-size: 11px; }
QToolTip { background: #35373d; color: #f0f1f3; border: 1px solid #555; }
'''


def label(text: str, name: str = '', wrap: bool = False) -> QLabel:
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def button(text: str, callback, primary=False) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setObjectName('primary')
    b.clicked.connect(callback)
    return b


class BrandMark(QLabel):
    def __init__(self, size=44):
        super().__init__()
        self.setFixedSize(size, size)
        self.setPixmap(logo_pixmap(size))


class CheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, opt, self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor('#ffffff' if self.isEnabled() else '#a0a4aa'), 2.0,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        p.drawPolyline(QPolygonF([QPointF(x+w*.22,y+h*.51), QPointF(x+w*.43,y+h*.72), QPointF(x+w*.78,y+h*.28)]))
        p.end()


class Setup(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName('window')
        self.setWindowTitle('PyNove Setup')
        self.setWindowIcon(QIcon(logo_pixmap()))
        self.resize(860, 575)
        self.setMinimumSize(780, 530)
        self.worker = None
        self.release_worker = None
        self.launcher = ''
        self.success = False
        self.steps = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame(objectName='header')
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 10, 18, 10)
        h.addWidget(BrandMark(28))
        h.addWidget(label('PyNove  /  Setup', 'field'))
        h.addStretch()
        h.addWidget(label('INSTALLER  ' + SETUP_VERSION, 'small'))
        root.addWidget(header)
        center = QHBoxLayout()
        center.setSpacing(0)
        rail = QFrame(objectName='rail')
        rail.setFixedWidth(205)
        r = QVBoxLayout(rail)
        r.setContentsMargins(18, 24, 18, 18)
        r.setSpacing(6)
        r.addWidget(label('INSTALLATION', 'eyebrow'))
        r.addSpacing(12)
        for i, name in enumerate(('Welcome', 'Setup options', 'Installation', 'Finish'), 1):
            s = label(f'{i:02d}    {name}', 'step')
            self.steps.append(s)
            r.addWidget(s)
        r.addStretch()
        r.addWidget(label('VISUAL NOVEL ENGINE', 'small'))
        r.addWidget(label('Made for storytellers.', 'muted', True))
        center.addWidget(rail)
        self.stack = QStackedWidget()
        center.addWidget(self.stack, 1)
        root.addLayout(center, 1)
        footer = QFrame(objectName='footer')
        f = QHBoxLayout(footer)
        f.setContentsMargins(18, 12, 18, 12)
        self.footer_note = label('PyNove Setup', 'small')
        f.addWidget(self.footer_note)
        f.addStretch()
        self.cancel = button('Cancel', self.close)
        self.back = button('Back', self.go_back)
        self.next = button('Continue', self.go_next, True)
        self.next.setMinimumWidth(112)
        for b in (self.cancel, self.back, self.next):
            f.addWidget(b)
        root.addWidget(footer)
        self.welcome_page()
        self.options_page()
        self.progress_page()
        self.finish_page()
        self.show_page(0)

    def page(self, eyebrow, heading, description):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(32, 27, 32, 22)
        layout.setSpacing(10)
        layout.addWidget(label(eyebrow, 'eyebrow'))
        layout.addWidget(label(heading, 'heading'))
        layout.addWidget(label(description, 'muted', True))
        layout.addSpacing(8)
        self.stack.addWidget(w)
        return layout

    def welcome_page(self):
        l = self.page('GET STARTED', 'Welcome to PyNove', 'A visual novel creation engine built around a visual editor and Python.')
        panel = QFrame(objectName='panel')
        p = QVBoxLayout(panel)
        p.setContentsMargins(18, 19, 18, 19)
        row = QHBoxLayout()
        row.addWidget(BrandMark(58))
        brand = QVBoxLayout()
        brand.addWidget(label('PyNove', 'brand'))
        brand.addWidget(label('Your story. Your rules.', 'muted'))
        row.addLayout(brand)
        row.addStretch()
        p.addLayout(row)
        p.addSpacing(15)
        p.addWidget(label('STORY   /   UI LAYOUT   /   ANIMATION   /   CODE', 'eyebrow', True))
        p.addWidget(label('Setup downloads a prebuilt PyNove release for your operating system and creates a launcher. No separate Python environment is needed for the bundled editor.', 'muted', True))
        l.addWidget(panel)
        l.addStretch()
        l.addWidget(label('You can change the installation location on the next screen. No administrator permissions are required for the default location.', 'small', True))

    def options_page(self):
        l = self.page('CONFIGURATION', 'Setup options', 'Choose a PyNove version and where it should be installed.')
        grid = QGridLayout()
        grid.setHorizontalSpacing(7)
        grid.setVerticalSpacing(7)
        self.version = QComboBox()
        self.version.addItem('Checking GitHub releases…', None)
        self.refresh = button('Refresh', self.load_releases)
        self.release_status = label(f'GitHub / {REPOSITORY}', 'small', True)
        self.source = QLineEdit()
        self.source.setPlaceholderText('Optional local ZIP / TAR.GZ package or folder')
        self.source.textChanged.connect(self.source_changed)
        self.target = QLineEdit(str(default_target()))
        grid.addWidget(label('PyNove version', 'field'), 0, 0, 1, 3)
        grid.addWidget(self.version, 1, 0, 1, 2)
        grid.addWidget(self.refresh, 1, 2)
        grid.addWidget(self.release_status, 2, 0, 1, 3)
        grid.addWidget(label('Local package (optional, overrides GitHub)', 'field'), 3, 0, 1, 3)
        grid.addWidget(self.source, 4, 0)
        grid.addWidget(button('Folder…', self.pick_folder), 4, 1)
        grid.addWidget(button('Archive…', self.pick_zip), 4, 2)
        grid.addWidget(label('Install location', 'field'), 5, 0, 1, 3)
        grid.addWidget(self.target, 6, 0, 1, 2)
        grid.addWidget(button('Browse…', self.pick_target), 6, 2)
        l.addLayout(grid)
        self.link = CheckBox('Create ' + ('a desktop shortcut' if os.name == 'nt' else 'an application menu shortcut'))
        self.link.setChecked(True)
        l.addWidget(self.link)
        l.addStretch()
        self.version.currentIndexChanged.connect(self.source_changed)
        self.load_releases()

    def source_changed(self, *_):
        if self.stack.currentIndex() == 1:
            self.next.setEnabled(bool(self.source.text().strip()) or bool(self.version.currentData()))

    def load_releases(self):
        if self.release_worker is not None and self.release_worker.isRunning():
            return
        self.refresh.setEnabled(False)
        self.version.clear()
        self.version.addItem('Checking GitHub releases…', None)
        self.release_status.setText(f'Connecting to {REPOSITORY}…')
        self.release_worker = ReleaseWorker()
        self.release_worker.result.connect(self.releases_ready)
        self.release_worker.finished.connect(lambda: self.refresh.setEnabled(True))
        self.release_worker.start()

    def releases_ready(self, choices, error):
        self.version.clear()
        if error or not choices:
            self.version.addItem('No compatible releases available', None)
            self.release_status.setText('GitHub: ' + (error or 'No matching release packages found.'))
        else:
            for i, asset in enumerate(choices):
                name = asset['version'] + ('  (pre-release)' if asset['prerelease'] else '')
                if i == 0:
                    name += '  · Recommended'
                name += f"  —  {asset['size'] / 1048576:.1f} MiB"
                self.version.addItem(name, asset)
            system, arch = platform_key()
            self.release_status.setText(f'{REPOSITORY}  ·  {system} / {arch}  ·  {len(choices)} package(s)')
        self.source_changed()

    def progress_page(self):
        l = self.page('INSTALLATION', 'Installing PyNove', 'Downloading, verifying and extracting the selected release.')
        self.task = label('Preparing…', 'field', True)
        l.addWidget(self.task)
        row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.percent = label('0%', 'muted')
        row.addWidget(self.bar, 1)
        row.addWidget(self.percent)
        l.addLayout(row)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(5000)
        l.addWidget(self.logs, 1)

    def finish_page(self):
        l = self.page('COMPLETE', 'Installation complete', 'PyNove is ready to launch.')
        self.finish_heading = l.itemAt(1).widget()
        self.finish_desc = l.itemAt(2).widget()
        p = QFrame(objectName='panel')
        layout = QVBoxLayout(p)
        layout.setContentsMargins(16, 16, 16, 16)
        self.finish_path = label('', 'field', True)
        self.finish_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.finish_details = label('', 'muted', True)
        layout.addWidget(self.finish_path)
        layout.addWidget(self.finish_details)
        l.addWidget(p)
        self.launch = CheckBox('Launch PyNove after closing Setup')
        self.launch.setChecked(True)
        l.addWidget(self.launch)
        l.addStretch()

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, 'Select PyNove source folder', self.source.text() or str(HERE))
        if d:
            self.source.setText(d)

    def pick_zip(self):
        f, _ = QFileDialog.getOpenFileName(self, 'Select PyNove release archive', str(HERE), 'Release archives (*.zip *.tar.gz *.tgz *.tar)')
        if f:
            self.source.setText(f)

    def pick_target(self):
        # Select the parent, then append PyNove to avoid overwriting its contents.
        d = QFileDialog.getExistingDirectory(self, 'Select parent folder for PyNove', str(Path.home()))
        if d:
            p = Path(d)
            self.target.setText(str(p if p.name.lower() == 'pynove' else p / 'PyNove'))

    def show_page(self, index):
        self.stack.setCurrentIndex(index)
        for i, s in enumerate(self.steps):
            s.setProperty('active', i == index)
            s.setProperty('done', i < index)
            s.style().unpolish(s)
            s.style().polish(s)
        self.back.setVisible(index in (1, 3))
        self.back.setEnabled(index == 1 or (index == 3 and not self.success))
        self.cancel.setEnabled(index != 2)
        self.cancel.setVisible(index != 3)
        self.next.setEnabled(index != 2)
        if index == 1:
            self.source_changed()
        self.next.setText(('Continue', 'Install', 'Installing…', 'Finish' if self.success else 'Close')[index])
        self.footer_note.setText(('Welcome', 'Ready to configure', 'Please keep Setup open', 'Setup finished')[index])

    def go_back(self):
        self.show_page(1 if self.stack.currentIndex() == 3 else 0)

    def go_next(self):
        i = self.stack.currentIndex()
        if i == 0:
            self.show_page(1)
        elif i == 1:
            self.start_install()
        elif i == 3:
            if self.success and self.launch.isChecked():
                try:
                    p = Path(self.launcher)
                    if os.name == 'nt':
                        subprocess.Popen([str(p)], cwd=p.parent)
                    else:
                        subprocess.Popen([str(p)], cwd=p.parent, start_new_session=True)
                except Exception as exc:
                    QMessageBox.warning(self, 'Launch failed', str(exc))
                    return
            self.close()

    def start_install(self):
        try:
            if not self.target.text().strip():
                raise ValueError('Choose an installation location.')
            target = Path(self.target.text()).expanduser().resolve()
            if self.source.text().strip():
                source = Path(self.source.text()).expanduser().resolve()
                validate(source, target)
            else:
                source = self.version.currentData()
                if not source:
                    raise ValueError('Select an available release or a local package.')
                if target == Path(target.anchor) or target == Path.home().resolve():
                    raise ValueError('Choose a dedicated PyNove installation folder.')
                if target.exists() and (not target.is_dir() or (any(target.iterdir()) and not (target / MARKER).is_file())):
                    raise ValueError('Choose an empty folder or an existing PyNove installation.')
        except Exception as exc:
            QMessageBox.warning(self, 'Check setup options', str(exc))
            return
        if (target / MARKER).is_file():
            answer = QMessageBox.question(self, 'Replace PyNove installation',
                'The previous installation directory will be replaced, including any projects or settings saved inside it. Back those up first. Continue?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.success = False
        self.logs.clear()
        self.bar.setValue(0)
        self.percent.setText('0%')
        self.show_page(2)
        self.worker = InstallWorker(source, target, self.link.isChecked())
        self.worker.progress.connect(self.update_progress)
        self.worker.log.connect(self.logs.appendPlainText)
        self.worker.result.connect(self.store_result)
        self.worker.finished.connect(self.install_finished)
        self.worker.start()

    def update_progress(self, value, message):
        self.bar.setValue(value)
        self.percent.setText(f'{value}%')
        self.task.setText(message)

    def store_result(self, success, launcher, details):
        self.success, self.launcher = success, launcher
        self.finish_heading.setText('Installation complete' if success else 'Installation failed')
        self.finish_desc.setText('PyNove has been installed on your computer.' if success else 'Check the setup options and try again.')
        self.finish_path.setText(str(Path(launcher).parent) if success else 'Setup could not complete')
        self.finish_details.setText(details or 'The editor launcher is ready.')
        self.finish_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.launch.setVisible(success)

    def install_finished(self):
        self.show_page(3)

    def closeEvent(self, event: QCloseEvent):
        if (self.worker is not None and self.worker.isRunning()) or (self.release_worker is not None and self.release_worker.isRunning()):
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('PyNove Setup')
    app.setWindowIcon(QIcon(logo_pixmap()))
    app.setStyle('Fusion')
    app.setFont(QFont('Inter' if 'Inter' in __import__('PyQt6.QtGui', fromlist=['QFontDatabase']).QFontDatabase.families() else 'Sans Serif', 10))
    app.setStyleSheet(QSS)
    w = Setup()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
