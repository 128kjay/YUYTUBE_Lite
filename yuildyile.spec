# yuytubelite.spec
# Build with: pyinstaller yuytubelite.spec

from PyInstaller.utils.hooks import collect_all

# Collect everything from these packages
packages_to_collect = [
    "PyQt6",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
]

datas, binaries, hiddenimports = [], [], []
for pkg in packages_to_collect:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

block_cipher = None

a = Analysis(
    ['yuytubelite.py'], 
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YUYTubeLite',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no console window
    icon=None,              
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='YUYTubeLite v0.4'
)
