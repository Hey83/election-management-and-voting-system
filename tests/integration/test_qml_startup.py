import os
import subprocess
import sys
from pathlib import Path


def test_desktop_qml_loads_to_visible_sign_in_form(tmp_path: Path) -> None:
    script = """
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle

from election.desktop import __main__ as desktop_entry
from election.desktop.controller import DesktopController

QQuickStyle.setStyle('Material')
application = QGuiApplication([])
server = 'https://election.test:8443'
controller = DesktopController(server)
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty('controller', controller)
engine.load(QUrl.fromLocalFile(str(Path(desktop_entry.__file__).with_name('qml') / 'Main.qml')))
(window,) = engine.rootObjects()
assert window.property('visible') is True
assert window.property('signedIn') is False
for name, value in {'serverAddress': server, 'username': '', 'password': ''}.items():
    field = window.findChild(QQuickItem, name)
    assert field is not None and field.isVisible() and field.isEnabled(), name
    assert field.property('text') == value, name
window.close()
controller.shutdown()
"""
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "ELECTION_CLIENT_STATE_DIR": str(tmp_path / "commands"),
            "PYTHONPATH": str(source),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
