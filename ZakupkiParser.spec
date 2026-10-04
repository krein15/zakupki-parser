# PyInstaller build: python -m PyInstaller ZakupkiParser.spec --noconfirm
#
# The result is dist/ZakupkiParser/ZakupkiParser.exe: the window without arguments, the command line with them
# (Task Scheduler starts it with "monitor <profiles>"). A folder build starts much faster than a single file.
#
# pymorphy3 finds its Russian dictionary through the entry point of the pymorphy3-dicts-ru package, so the package's
# metadata goes into the build together with the dictionary files.

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = [
    ("assets/icon.ico", "assets"),
    ("zkparser/templates/*.toml", "zkparser/templates"),  # niche templates of the "Новый профиль…" menu
    ("zkparser/certs/russian_trusted_root_ca.pem", "zkparser/certs"),
]
datas += collect_data_files("customtkinter")  # themes and fonts
datas += collect_data_files("pymorphy3_dicts_ru")
datas += copy_metadata("pymorphy3_dicts_ru")

analysis = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["pymorphy3_dicts_ru", "lxml.etree", "lxml.html", "openpyxl"],
    excludes=["matplotlib", "numpy", "pandas", "pytest", "tkinter.test", "test"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="ZakupkiParser",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/icon.ico",
    version_info=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="ZakupkiParser",
)
