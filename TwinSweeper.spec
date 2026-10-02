# -*- mode: python ; coding: utf-8 -*-
import os
import sys

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('flet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Bundled runtime assets: the window icon must resolve inside _MEIPASS
# when --onefile unpacks (see main._window_icon_path).
datas += [('assets/icon.ico', 'assets')]

# --- Version resource (release plan 5.3) --------------------------------------
# Single source of truth: app_info.APP_VERSION. The spec is plain Python, so
# it imports the identity module and regenerates version_info.txt on every
# build - the EXE Properties (Details) can never drift from the app version.
# Do not edit version_info.txt by hand: bump APP_VERSION and rebuild.
# SPECPATH is absolute and points at this spec's directory, so the import
# works no matter which cwd PyInstaller was started from
# (BUILD.bat, CI, absolute spec path).
sys.path.insert(0, SPECPATH)
from app_info import APP_NAME, APP_VERSION


def _version_parts(version: str) -> tuple[int, ...]:
    # '3.7.0' -> (3, 7, 0, 0): fixed file info requires four components.
    parts = [int(p) for p in version.split('.')]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


_ver = _version_parts(APP_VERSION)  # (3, 7, 0, 0)
_ver_str = '.'.join(str(p) for p in _ver)  # '3.7.0.0'

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_ver,
        prodvers=_ver,
        mask=0x3f,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable('040904B0', [
                StringStruct('CompanyName', 'Harbuzilia'),
                StringStruct('FileDescription', 'TwinSweeper — поиск и безопасное удаление дубликатов'),
                StringStruct('FileVersion', _ver_str),
                StringStruct('InternalName', APP_NAME),
                StringStruct('LegalCopyright', '© Harbuzilia'),
                StringStruct('OriginalFilename', 'TwinSweeper.exe'),
                StringStruct('ProductName', APP_NAME),
                StringStruct('ProductVersion', _ver_str),
            ]),
        ]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ],
)

# str(VSVersionInfo) is exactly the evaluable text format that PyInstaller's
# load_version_info_from_text_file() parses back. LF line endings keep the
# regenerated file byte-identical between builds (no git churn).
with open(os.path.join(SPECPATH, 'version_info.txt'), 'w', encoding='utf-8', newline='\n') as fp:
    fp.write(str(version_info))


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TwinSweeper',
    icon='assets/icon.ico',
    # Regenerated from app_info.APP_VERSION on every build (see above);
    # relative paths are anchored to the spec directory by PyInstaller.
    version='version_info.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off for public releases (owner decision, release plan item 5):
    # compressed unsigned EXEs trigger far more antivirus false positives;
    # revisit only if the binary gets code-signed.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
