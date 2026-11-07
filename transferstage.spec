# PyInstaller spec: one-folder build of TransferStageControl.
#
#   pip install -e .[build]
#   pyinstaller transferstage.spec --noconfirm
#
# Output: dist\TransferStageControl\TransferStageControl.exe, with config.yaml next to it
# (logs\ is created there too). Copy the whole dist\TransferStageControl folder to the target PC.
#
# The target PC still needs:
#   * 64-bit Thorlabs Kinesis (the Kinesis .NET DLLs are loaded at run time from
#     devices.kinesis_path, they are NOT bundled), and the .NET Framework that Kinesis installs;
#   * NI-VISA (or another VISA library) with the DMM alias;
#   * the USB-serial driver for the B&K 1685B.
# The simulators need none of these.

import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

hiddenimports = (
    collect_submodules("transferstage")  # drivers are imported lazily (build_devices)
    + collect_submodules("pyqtgraph", filter=lambda m: "examples" not in m and "opengl" not in m)
    + ["clr", "clr_loader", "pythonnet", "pyvisa.ctwrapper", "serial.tools.list_ports"]
)
datas = (
    collect_data_files("pythonnet")  # Python.Runtime.dll
    + collect_data_files("clr_loader")  # ClrLoader.dll / native helpers
    + collect_data_files("pyqtgraph", excludes=["**/examples/**"])
)

a = Analysis(
    [str(ROOT / "scripts" / "pyinstaller_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "pytestqt", "matplotlib", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TransferStageControl",
    console=False,  # errors are shown in message boxes and written to logs\app.log
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="TransferStageControl", upx=False)

# config.yaml lives next to the .exe (transferstage.config.PROJECT_ROOT in a frozen build).
_dist = Path(DISTPATH) / "TransferStageControl"
shutil.copy2(ROOT / "config.yaml", _dist / "config.yaml")
for doc in ("README.md",):
    shutil.copy2(ROOT / doc, _dist / doc)
shutil.copytree(ROOT / "docs", _dist / "docs", dirs_exist_ok=True)
