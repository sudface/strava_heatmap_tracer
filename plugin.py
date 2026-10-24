"""Modeless QGIS dialog for the bundled heatmap-to-GPX command-line program."""

import os
from pathlib import Path
import shutil

from qgis.PyQt.QtCore import QProcess, QProcessEnvironment, QTimer
from qgis.PyQt.QtWidgets import (
    QAction, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsVectorLayer


class StravaHeatmapTracer:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        self.action = QAction("Trace Strava heatmap…", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addPluginToMenu("&Strava Heatmap Tracer", self.action)

    def unload(self):
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog = None
        if self.action is not None:
            self.iface.removePluginMenu("&Strava Heatmap Tracer", self.action)
            self.action.deleteLater()
            self.action = None

    def show_dialog(self):
        if self.dialog is None:
            self.dialog = TraceDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()


class TraceDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.process = None
        self.was_cancelled = False
        self.current_output = ""
        self.setWindowTitle("Strava Heatmap Tracer")
        self.setMinimumWidth(620)
        self.resize(680, 670)

        root = QVBoxLayout(self)
        info = QLabel("Trace visible heatmap lines into GPX. Bounds are WGS84 degrees; distances are metres.")
        info.setWordWrap(True)
        root.addWidget(info)

        bounds = QGroupBox("Bounding box (latitude / longitude)")
        grid = QGridLayout(bounds)
        self.south = self._coordinate(-33.56914940407827, -85.0, 85.0)
        self.west = self._coordinate(151.1217400527635, -180.0, 180.0)
        self.north = self._coordinate(-33.316046167288036, -85.0, 85.0)
        self.east = self._coordinate(151.5441820221408, -180.0, 180.0)
        for row, label, field in (
            (0, "South", self.south), (0, "West", self.west),
            (1, "North", self.north), (1, "East", self.east),
        ):
            col = 0 if label in ("South", "North") else 2
            grid.addWidget(QLabel(label), row, col)
            grid.addWidget(field, row, col + 1)
        extent_button = QPushButton("Use current map extent")
        extent_button.clicked.connect(self.use_canvas_extent)
        grid.addWidget(extent_button, 2, 0, 1, 4)
        root.addWidget(bounds)

        params = QGroupBox("Trace settings")
        form = QFormLayout(params)
        self.cookie = QLineEdit()
        self.cookie.setPlaceholderText("Paste the Cookie request header value (optional if tiles load without it)")
        self.cookie.setEchoMode(QLineEdit.Password)
        self.cookie.setToolTip("Not saved; cleared after starting the extraction.")
        cookie_row = QWidget()
        cookie_layout = QHBoxLayout(cookie_row)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.addWidget(self.cookie)
        self.show_cookie = QCheckBox("Show")
        self.show_cookie.toggled.connect(
            lambda show: self.cookie.setEchoMode(QLineEdit.Normal if show else QLineEdit.Password)
        )
        cookie_layout.addWidget(self.show_cookie)
        form.addRow("Cookie header", cookie_row)

        self.zoom = self._integer(14, 11, 16)
        self.threshold = self._integer(45, 1, 255)
        self.channel = QComboBox()
        self.channel.addItems(("visible", "alpha", "luminance"))
        self.min_length = self._decimal(20, 0, 10000)
        self.simplify = self._decimal(4, 0, 1000)
        self.workers = self._integer(2, 1, 6)
        for label, field in (
            ("Tile zoom", self.zoom),
            ("Minimum brightness (1–255)", self.threshold),
            ("Image channel", self.channel),
            ("Minimum segment length (m)", self.min_length),
            ("Simplification tolerance (m)", self.simplify),
            ("Concurrent downloads", self.workers),
        ):
            form.addRow(label, field)
        root.addWidget(params)

        paths = QGroupBox("Files and interpreter")
        files = QFormLayout(paths)
        default_folder = str(Path.home() / "Documents")
        self.cache = QLineEdit(os.path.join(default_folder, "strava_tile_cache"))
        self.output = QLineEdit(os.path.join(default_folder, "strava_heatmap_traces.gpx"))
        self.python = QLineEdit(shutil.which("python") or shutil.which("python3") or "python")
        files.addRow("Tile cache", self._browse_row(self.cache, "folder"))
        files.addRow("Output GPX", self._browse_row(self.output, "save"))
        files.addRow("Python executable", self._browse_row(self.python, "open"))
        root.addWidget(paths)

        self.preview = QCheckBox("Write a red-line preview PNG beside the GPX")
        self.add_layer = QCheckBox("Add the finished GPX tracks to this project")
        self.add_layer.setChecked(True)
        root.addWidget(self.preview)
        root.addWidget(self.add_layer)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        root.addWidget(self.log, 1)
        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run extraction")
        self.run_button.clicked.connect(self.run_extraction)
        self.cancel_button = QPushButton("Cancel run")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_run)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        root.addLayout(buttons)

    @staticmethod
    def _coordinate(value, minimum, maximum):
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(8)
        spin.setSingleStep(0.001)
        spin.setValue(value)
        return spin

    @staticmethod
    def _decimal(value, minimum, maximum):
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setValue(value)
        return spin

    @staticmethod
    def _integer(value, minimum, maximum):
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    def _browse_row(self, field, kind):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field)
        button = QPushButton("Browse…")

        def browse():
            if kind == "folder":
                selected = QFileDialog.getExistingDirectory(self, "Tile cache folder", field.text())
            elif kind == "save":
                selected, _ = QFileDialog.getSaveFileName(
                    self, "Save GPX", field.text(), "GPX tracks (*.gpx)"
                )
            else:
                selected, _ = QFileDialog.getOpenFileName(
                    self, "Select Python executable", field.text(), "Programs (*.exe *);;All files (*)"
                )
            if selected:
                field.setText(selected)

        button.clicked.connect(browse)
        layout.addWidget(button)
        return row

    def use_canvas_extent(self):
        try:
            canvas = self.iface.mapCanvas()
            to_wgs84 = QgsCoordinateTransform(
                canvas.mapSettings().destinationCrs(),
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance(),
            )
            box = to_wgs84.transformBoundingBox(canvas.extent())
            self.south.setValue(box.yMinimum())
            self.west.setValue(box.xMinimum())
            self.north.setValue(box.yMaximum())
            self.east.setValue(box.xMaximum())
        except Exception as exc:
            QMessageBox.warning(self, "Could not use map extent", str(exc))

    def _append_log(self, content):
        chunk = bytes(content).decode("utf-8", "replace").strip()
        if chunk:
            self.log.appendPlainText(chunk)

    def run_extraction(self):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            return
        south, west, north, east = (
            self.south.value(), self.west.value(), self.north.value(), self.east.value()
        )
        if not (south < north and west < east):
            QMessageBox.warning(self, "Invalid bounds", "South must be below north; west must be left of east.")
            return
        output = os.path.abspath(os.path.expanduser(self.output.text().strip()))
        cache = os.path.abspath(os.path.expanduser(self.cache.text().strip()))
        python = self.python.text().strip().strip('"')
        if not self.output.text().strip() or not self.cache.text().strip() or not python:
            QMessageBox.warning(self, "Missing path", "Choose a Python executable, cache folder and output GPX.")
            return
        if not output.lower().endswith(".gpx"):
            output += ".gpx"
        script = os.path.join(os.path.dirname(__file__), "extractor.py")
        arguments = [
            script, "--bbox", str(south), str(west), str(north), str(east),
            "--output", output, "--cache", cache,
            "--zoom", str(self.zoom.value()),
            "--threshold", str(self.threshold.value()),
            "--channel", self.channel.currentText(),
            "--min-length-m", str(self.min_length.value()),
            "--simplify-m", str(self.simplify.value()),
            "--workers", str(self.workers.value()),
        ]
        if self.preview.isChecked():
            arguments += ["--preview", str(Path(output).with_name(Path(output).stem + "_preview.png"))]
        process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("STRAVA_COOKIE", self.cookie.text())
        process.setProcessEnvironment(env)
        process.setProgram(python)
        process.setArguments(arguments)
        process.readyReadStandardOutput.connect(
            lambda: self._append_log(process.readAllStandardOutput())
        )
        process.readyReadStandardError.connect(
            lambda: self._append_log(process.readAllStandardError())
        )
        process.errorOccurred.connect(lambda error: self._process_error(process, error))
        process.finished.connect(lambda code, status: self._finished(process, code, status))
        self.process = process
        self.was_cancelled = False
        self.current_output = output
        self.log.clear()
        self.log.appendPlainText("Starting extraction. Cached tiles will be reused.")
        self.cookie.clear()
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        process.start()

    def _process_error(self, process, error):
        if process is self.process:
            self.log.appendPlainText("Process error: " + process.errorString())
            if error == QProcess.FailedToStart:
                self._finished(process, -1, QProcess.CrashExit)

    def _finished(self, process, code, status):
        if process is not self.process:
            return
        self._append_log(process.readAllStandardOutput())
        self._append_log(process.readAllStandardError())
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        # Remove the short-lived cookie environment from the QProcess object.
        process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        self.process = None
        process.deleteLater()
        if self.was_cancelled:
            self.log.appendPlainText("Cancelled. Downloaded tiles remain in the cache.")
            return
        if status != QProcess.NormalExit or code != 0:
            self.log.appendPlainText(
                "Extraction failed. Check the log, cookie, and that this Python has numpy, scipy and pillow."
            )
            return
        self.log.appendPlainText("Finished: " + self.current_output)
        if self.add_layer.isChecked():
            uri = self.current_output + "?type=track"
            for layer in list(QgsProject.instance().mapLayers().values()):
                if isinstance(layer, QgsVectorLayer) and layer.source() == uri:
                    QgsProject.instance().removeMapLayer(layer.id())
            layer = QgsVectorLayer(uri, Path(self.current_output).stem, "gpx")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            else:
                self.log.appendPlainText("GPX was written but QGIS could not load its track layer.")

    def cancel_run(self):
        if self.process is None or self.process.state() == QProcess.NotRunning:
            return
        self.was_cancelled = True
        process = self.process
        process.terminate()
        QTimer.singleShot(3000, lambda: self._kill_if_running(process))
        self.log.appendPlainText("Stopping extraction…")

    def _kill_if_running(self, process):
        if process is self.process and process.state() != QProcess.NotRunning:
            process.kill()

    def shutdown(self):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        self.close()

    def closeEvent(self, event):
        if self.process is not None and self.process.state() != QProcess.NotRunning:
            self.cancel_run()
            event.ignore()
            return
        super().closeEvent(event)
