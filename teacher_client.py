"""
Lab Manager - Teacher Client (UPDATED)
Features:
- Connect to admin server
- Transfer files directly to admin WITH FULL PATH
- Present screen to CLIENTS (like admin does) 
- View connected client list
- Monitor client screens via admin
- Basic client functions (connect, disconnect, reconnect)

FIXES:
- Presentation now targets clients (not admin)
- File transfer includes full file path
- Connected clients list shown automatically
"""

import sys
import os
import socket
import threading
import struct
import time
import json
import hashlib
from datetime import datetime

try:
    import mss
    import cv2
    import numpy as np
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install: pip install mss opencv-python numpy PyQt5")
    sys.exit(1)

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout, QPushButton,
    QMessageBox, QTextEdit, QHBoxLayout, QFileDialog, QInputDialog,
    QLineEdit, QListWidget, QSplitter, QGroupBox, QProgressBar,
    QSystemTrayIcon, QMenu, QAction
)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QPixmap, QIcon, QFont, QImage, QPainter, QColor
from PyQt5.QtCore import QByteArray

# Configuration
DEFAULT_SERVER_HOST = '127.0.0.1'  # Change this to admin PC's IP when on different computers
DEFAULT_SERVER_PORT = 5001
BUFFER_SIZE = 65536
RECONNECT_DELAY = 5000
SCREENSHOT_QUALITY = 75
CHUNK_SIZE = 8 * 1024 * 1024

# Config directory
CONFIG_DIR = os.path.join(os.path.expanduser("~"), "teacher_client_config")
os.makedirs(CONFIG_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


class TeacherSignals(QObject):
    update_status = pyqtSignal(str, str)
    show_message = pyqtSignal(str, str)
    log_message = pyqtSignal(str)
    file_progress = pyqtSignal(int, str)
    update_client_list = pyqtSignal(list)
    update_preview = pyqtSignal(bytes)
    enable_features = pyqtSignal()
    disable_features = pyqtSignal()


class TeacherClient(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lab Manager - Teacher Client")
        self.setMinimumSize(1000, 700)
        
        # Connection state
        self.server_host = DEFAULT_SERVER_HOST
        self.server_port = DEFAULT_SERVER_PORT
        self.connected = False
        self.connecting = False
        self.client_socket = None
        self.running = True
        self.reconnect_scheduled = False
        
        # Presentation state
        self.presenting = False
        self.presentation_thread = None
        self.presentation_targets = []  # List of client keys to present to
        
        # Monitoring state
        self.monitoring_client = None
        self.client_list = []  # List of available clients
        
        # Signals
        self.signals = TeacherSignals()
        self.signals.update_status.connect(self.update_status_label)
        self.signals.show_message.connect(self.show_message_box)
        self.signals.log_message.connect(self.append_log)
        self.signals.file_progress.connect(self.update_file_progress)
        self.signals.update_client_list.connect(self.update_client_list_widget)
        self.signals.update_preview.connect(self.update_preview_image)
        self.signals.enable_features.connect(self.enable_connected_features)
        self.signals.disable_features.connect(self.disable_connected_features)
        
        # Load config
        self.load_config()
        
        # Setup UI
        self.init_ui()
        self.setup_system_tray()
        
        # Start connection
        QTimer.singleShot(500, self.attempt_connection)
    
    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.server_host = config.get('server_host', DEFAULT_SERVER_HOST)
                    self.server_port = config.get('server_port', DEFAULT_SERVER_PORT)
        except:
            pass
    
    def save_config(self):
        try:
            config = {
                'server_host': self.server_host,
                'server_port': self.server_port
            }
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        except:
            pass
    
    def init_ui(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
            QPushButton:disabled {
                background-color: #3c3c3c;
                color: #888;
            }
            QLabel {
                color: #e0e0e0;
            }
            QTextEdit, QListWidget {
                background-color: #252526;
                color: #e0e0e0;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
            }
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
                color: #4EC9B0;
            }
        """)
        
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        
        # Header
        header = QLabel("👨‍🏫 Teacher Control Panel")
        header.setFont(QFont("Segoe UI", 16, QFont.Bold))
        header.setStyleSheet("color: #0078d4; padding: 10px;")
        main_layout.addWidget(header)
        
        # Status
        self.status_label = QLabel("🔄 Connecting to admin server...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 11))
        self.status_label.setStyleSheet("background-color: #3c3c3c; padding: 10px; border-radius: 5px;")
        main_layout.addWidget(self.status_label)
        
        self.connection_info = QLabel(f"Server: {self.server_host}:{self.server_port}")
        self.connection_info.setAlignment(Qt.AlignCenter)
        self.connection_info.setStyleSheet("color: #888; padding: 5px;")
        main_layout.addWidget(self.connection_info)
        
        # Main content splitter
        splitter = QSplitter(Qt.Horizontal)
        
        # Left panel - Controls and Client List
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        
        # Connection controls
        conn_group = QGroupBox("Connection")
        conn_layout = QVBoxLayout(conn_group)
        
        btn_layout = QHBoxLayout()
        self.btn_reconnect = QPushButton("🔄 Reconnect")
        self.btn_reconnect.clicked.connect(self.manual_reconnect)
        btn_layout.addWidget(self.btn_reconnect)
        
        self.btn_config = QPushButton("⚙️ Configure")
        self.btn_config.clicked.connect(self.configure_server)
        btn_layout.addWidget(self.btn_config)
        
        conn_layout.addLayout(btn_layout)
        left_layout.addWidget(conn_group)
        
        # File transfer controls
        file_group = QGroupBox("File Transfer to Admin")
        file_layout = QVBoxLayout(file_group)
        
        self.btn_send_file = QPushButton("📤 Send File")
        self.btn_send_file.clicked.connect(self.send_file_to_admin)
        self.btn_send_file.setEnabled(False)
        file_layout.addWidget(self.btn_send_file)
        
        self.file_progress = QProgressBar()
        self.file_progress.setVisible(False)
        file_layout.addWidget(self.file_progress)
        
        self.file_status = QLabel("")
        self.file_status.setStyleSheet("color: #888; font-size: 10px;")
        file_layout.addWidget(self.file_status)
        
        left_layout.addWidget(file_group)
        
        # Presentation controls
        present_group = QGroupBox("Present to Clients")
        present_layout = QVBoxLayout(present_group)
        
        present_info = QLabel("Present your screen to selected clients")
        present_info.setStyleSheet("color: #888; font-size: 10px;")
        present_layout.addWidget(present_info)
        
        self.btn_present = QPushButton("📽️ Start Presenting")
        self.btn_present.clicked.connect(self.toggle_presentation)
        self.btn_present.setEnabled(False)
        present_layout.addWidget(self.btn_present)
        
        self.present_status = QLabel("Not presenting")
        self.present_status.setStyleSheet("color: #888; font-size: 10px;")
        present_layout.addWidget(self.present_status)
        
        left_layout.addWidget(present_group)
        
        # Client List
        client_group = QGroupBox("Connected Clients")
        client_layout = QVBoxLayout(client_group)
        
        self.client_list_widget = QListWidget()
        self.client_list_widget.setSelectionMode(QListWidget.MultiSelection)
        client_layout.addWidget(self.client_list_widget)
        
        client_btn_layout = QHBoxLayout()
        self.btn_refresh_clients = QPushButton("🔄 Refresh List")
        self.btn_refresh_clients.clicked.connect(self.request_client_list)
        self.btn_refresh_clients.setEnabled(False)
        client_btn_layout.addWidget(self.btn_refresh_clients)
        
        client_layout.addLayout(client_btn_layout)
        left_layout.addWidget(client_group)
        
        left_layout.addStretch()
        
        # Right panel - Preview and Log
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        # Preview section (for monitoring clients)
        preview_group = QGroupBox("Client Monitor")
        preview_layout = QVBoxLayout(preview_group)
        
        self.preview_label = QLabel("Select a client to monitor")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(400, 300)
        self.preview_label.setStyleSheet("background-color: #2d2d2d; border: 1px solid #3c3c3c;")
        preview_layout.addWidget(self.preview_label)
        
        self.preview_info = QLabel("No client selected")
        self.preview_info.setStyleSheet("color: #888; font-size: 10px;")
        preview_layout.addWidget(self.preview_info)
        
        preview_btn_layout = QHBoxLayout()
        self.btn_monitor = QPushButton("👁️ Monitor Selected")
        self.btn_monitor.clicked.connect(self.monitor_selected_client)
        self.btn_monitor.setEnabled(False)
        preview_btn_layout.addWidget(self.btn_monitor)
        
        self.btn_stop_monitor = QPushButton("⏹️ Stop Monitoring")
        self.btn_stop_monitor.clicked.connect(self.stop_monitoring)
        self.btn_stop_monitor.setEnabled(False)
        preview_btn_layout.addWidget(self.btn_stop_monitor)
        
        preview_layout.addLayout(preview_btn_layout)
        right_layout.addWidget(preview_group)
        
        # Log section
        log_group = QGroupBox("Activity Log")
        log_layout = QVBoxLayout(log_group)
        
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMinimumHeight(200)
        log_layout.addWidget(self.txt_log)
        
        log_btn_layout = QHBoxLayout()
        btn_clear_log = QPushButton("Clear Log")
        btn_clear_log.clicked.connect(lambda: self.txt_log.clear())
        log_btn_layout.addWidget(btn_clear_log)
        log_btn_layout.addStretch()
        
        log_layout.addLayout(log_btn_layout)
        right_layout.addWidget(log_group)
        
        # Add panels to splitter
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([400, 600])
        
        main_layout.addWidget(splitter)
    
    def setup_system_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        
        tray_menu = QMenu()
        
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show)
        tray_menu.addAction(show_action)
        
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
    
    def update_status_label(self, text, color):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"background-color: {color}; color: white; padding: 10px; border-radius: 5px;"
        )
    
    def show_message_box(self, title, message):
        QMessageBox.information(self, title, message)
    
    def append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_log.append(f"[{timestamp}] {message}")
        self.txt_log.verticalScrollBar().setValue(
            self.txt_log.verticalScrollBar().maximum()
        )
    
    def log(self, message):
        self.signals.log_message.emit(message)
    
    def update_file_progress(self, percent, status):
        if percent < 0:
            self.file_progress.setVisible(False)
            self.file_status.setText("")
        else:
            self.file_progress.setVisible(True)
            self.file_progress.setValue(percent)
            self.file_status.setText(status)
    
    def update_client_list_widget(self, clients):
        self.client_list = clients
        self.client_list_widget.clear()
        for client in clients:
            self.client_list_widget.addItem(client)
        self.log(f"📋 Client list updated: {len(clients)} clients connected")
    
    def update_preview_image(self, image_data):
        try:
            qimg = QImage.fromData(QByteArray(image_data))
            if not qimg.isNull():
                pixmap = QPixmap.fromImage(qimg)
                scaled = pixmap.scaled(
                    self.preview_label.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.preview_label.setPixmap(scaled)
        except:
            pass
    
    def enable_connected_features(self):
        self.btn_send_file.setEnabled(True)
        self.btn_present.setEnabled(True)
        self.btn_refresh_clients.setEnabled(True)
        self.btn_monitor.setEnabled(True)
    
    def disable_connected_features(self):
        self.btn_send_file.setEnabled(False)
        self.btn_present.setEnabled(False)
        self.btn_refresh_clients.setEnabled(False)
        self.btn_monitor.setEnabled(False)
        self.btn_stop_monitor.setEnabled(False)
    
    def configure_server(self):
        host, ok1 = QInputDialog.getText(
            self, "Server Host", "Enter server IP:", QLineEdit.Normal, self.server_host
        )
        if not ok1:
            return
        
        port, ok2 = QInputDialog.getInt(
            self, "Server Port", "Enter port:", self.server_port, 1, 65535
        )
        if not ok2:
            return
        
        self.server_host = host
        self.server_port = port
        self.save_config()
        
        self.connection_info.setText(f"Server: {self.server_host}:{self.server_port}")
        self.log(f"Configuration updated: {self.server_host}:{self.server_port}")
        
        if self.connected:
            reply = QMessageBox.question(
                self, "Reconnect?",
                "Reconnect with new settings?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.manual_reconnect()
    
    def manual_reconnect(self):
        self.log("Manual reconnect requested")
        self.disconnect_socket()
        QTimer.singleShot(1000, self.attempt_connection)
    
    def attempt_connection(self):
        if self.connecting or self.connected:
            return
        
        self.connecting = True
        self.signals.update_status.emit("🔄 Connecting...", "#3c3c3c")
        
        threading.Thread(target=self._connect_to_server, daemon=True).start()
    
    def _connect_to_server(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((self.server_host, self.server_port))
            sock.settimeout(None)
            
            self.client_socket = sock
            self.connected = True
            self.connecting = False
            
            self.signals.update_status.emit("✅ Connected to Admin", "#16a34a")
            self.signals.enable_features.emit()
            self.log(f"✅ Connected to admin server at {self.server_host}:{self.server_port}")
            
            # Identify as teacher client
            info = {
                "type": "teacher",
                "hostname": "Teacher",
                "status": "connected"
            }
            info_json = json.dumps(info)
            self.client_socket.sendall(f"INFO:{info_json}\n".encode("utf-8"))
            self.log("👨‍🏫 Identified as teacher client to admin")
            
            # Start message handler
            threading.Thread(target=self._message_handler, daemon=True).start()
        
        except Exception as e:
            self.connecting = False
            self.signals.update_status.emit(f"❌ Connection Failed", "#d13438")
            self.log(f"❌ Connection failed: {e}")
            
            if self.running and not self.reconnect_scheduled:
                self.reconnect_scheduled = True
                QTimer.singleShot(RECONNECT_DELAY, self._schedule_reconnect)
    
    def _schedule_reconnect(self):
        self.reconnect_scheduled = False
        if not self.connected and self.running:
            self.attempt_connection()
    
    def disconnect_socket(self):
        self.connected = False
        if self.presenting:
            self.stop_presentation()
        
        # NEW: Stop monitoring if active
        if self.monitoring_client:
            self.monitoring_client = None
            self.btn_stop_monitor.setEnabled(False)
        
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
            self.client_socket = None
        
        self.signals.update_status.emit("❌ Disconnected", "#d13438")
        self.signals.disable_features.emit()
        self.log("Disconnected from admin server")
    
    def _message_handler(self):
        """Handle incoming messages from admin"""
        buffer = b""
        
        try:
            while self.connected and self.running:
                chunk = self.client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                
                buffer += chunk
                
                # Process line-based commands
                while b"\n" in buffer:
                    idx = buffer.find(b"\n")
                    line = buffer[:idx]
                    buffer = buffer[idx + 1:]
                    
                    try:
                        message = line.decode('utf-8', errors='ignore').strip()
                        if not message:
                            continue
                        
                        # Handle client list updates
                        if message.startswith("CLIENT_LIST:"):
                            client_json = message[12:]
                            clients = json.loads(client_json)
                            self.signals.update_client_list.emit(clients)
                        
                        # NEW: Handle monitored frames from admin
                        elif message.upper() == "MONITORED_FRAME":
                            # Read frame size
                            while len(buffer) < 8:
                                chunk = self.client_socket.recv(BUFFER_SIZE)
                                if not chunk:
                                    raise ConnectionError("Connection closed reading frame size")
                                buffer += chunk
                            
                            frame_size = struct.unpack(">Q", buffer[:8])[0]
                            buffer = buffer[8:]
                            
                            if frame_size <= 0 or frame_size > 200 * 1024 * 1024:
                                self.log(f"⚠️ Invalid monitored frame size: {frame_size}")
                                continue
                            
                            # Read frame data
                            while len(buffer) < frame_size:
                                chunk = self.client_socket.recv(min(BUFFER_SIZE, frame_size - len(buffer)))
                                if not chunk:
                                    raise ConnectionError("Connection closed reading frame data")
                                buffer += chunk
                            
                            frame_data = buffer[:frame_size]
                            buffer = buffer[frame_size:]
                            
                            # Display the monitored frame
                            self.signals.update_preview.emit(frame_data)
                        
                        # Handle monitoring errors
                        elif message.startswith("MONITOR_ERROR:"):
                            error_msg = message.split(":", 1)[1]
                            self.log(f"❌ Monitoring error: {error_msg}")
                            self.signals.show_message.emit("Monitoring Error", error_msg)
                        
                        # Handle other messages
                        else:
                            self.log(f"📨 Admin: {message}")
                    
                    except Exception as e:
                        self.log(f"Error processing message: {e}")
        
        except Exception as e:
            self.log(f"Message handler error: {e}")
        
        finally:
            if self.connected:
                self.disconnect_socket()
                if self.running:
                    QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)
    
    def send_file_to_admin(self):
        """Send a file to admin with full path information"""
        if not self.connected:
            QMessageBox.warning(self, "Not Connected", "Please connect to admin first")
            return
        
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Select File to Send", "", "All Files (*.*)"
        )
        
        if not filepath:
            return
        
        threading.Thread(
            target=self._transfer_file,
            args=(filepath,),
            daemon=True
        ).start()
    
    def _transfer_file(self, filepath):
        """Transfer file to admin with metadata"""
        try:
            filename = os.path.basename(filepath)
            filesize = os.path.getsize(filepath)
            
            self.log(f"📤 Sending file: {filename} ({format_bytes(filesize)})")
            self.signals.file_progress.emit(0, f"Preparing: {filename}")
            
            # Send header and metadata
            header = b"TEACHER_FILE\n"
            metadata = {
                "filename": filename,
                "filesize": filesize,
                "filepath": filepath,  # INCLUDE FULL PATH
                "timestamp": datetime.now().isoformat()
            }
            metadata_json = json.dumps(metadata).encode('utf-8')
            metadata_len = struct.pack(">I", len(metadata_json))
            
            self.client_socket.sendall(header)
            self.client_socket.sendall(metadata_len)
            self.client_socket.sendall(metadata_json)
            
            # Send file data in chunks
            with open(filepath, 'rb') as f:
                sent = 0
                while sent < filesize:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    self.client_socket.sendall(chunk)
                    sent += len(chunk)
                    
                    percent = int((sent / filesize) * 100)
                    self.signals.file_progress.emit(
                        percent,
                        f"Uploading: {format_bytes(sent)} / {format_bytes(filesize)}"
                    )
            
            # Send end marker
            self.client_socket.sendall(b"<FILE_END>")
            
            self.signals.file_progress.emit(-1, "")
            self.log(f"✅ File sent successfully: {filename}")
            self.log(f"📁 Source path: {filepath}")
            self.signals.show_message.emit(
                "File Transfer Complete",
                f"Successfully sent {filename}\nPath: {filepath}"
            )
        
        except Exception as e:
            self.signals.file_progress.emit(-1, "")
            self.log(f"❌ File transfer failed: {e}")
            self.signals.show_message.emit("Transfer Failed", f"Error: {e}")
    
    def toggle_presentation(self):
        """Toggle screen presentation to clients"""
        if not self.presenting:
            self.start_presentation()
        else:
            self.stop_presentation()
    
    def start_presentation(self):
        """Start presenting screen to selected clients"""
        if not self.connected:
            QMessageBox.warning(self, "Not Connected", "Please connect to admin first")
            return
        
        # Get selected clients
        selected_items = self.client_list_widget.selectedItems()
        if not selected_items:
            QMessageBox.warning(
                self, "No Selection",
                "Please select one or more clients to present to"
            )
            return
        
        self.presentation_targets = [item.text() for item in selected_items]
        
        try:
            # Send command to admin to start presentation
            targets_json = json.dumps(self.presentation_targets)
            cmd = f"TEACHER_START_PRESENTATION:{targets_json}\n"
            self.client_socket.sendall(cmd.encode('utf-8'))
            
            self.presenting = True
            self.btn_present.setText("⏹️ Stop Presenting")
            self.btn_present.setStyleSheet("background-color: #d13438;")
            self.present_status.setText(f"Presenting to {len(self.presentation_targets)} client(s)")
            self.present_status.setStyleSheet("color: #90ee90; font-size: 10px;")
            
            # Start presentation thread
            self.presentation_thread = threading.Thread(target=self._presentation_loop, daemon=True)
            self.presentation_thread.start()
            
            self.log(f"📽️ Started presenting to {len(self.presentation_targets)} client(s)")
        
        except Exception as e:
            self.log(f"Failed to start presentation: {e}")
            QMessageBox.critical(self, "Error", f"Failed to start presentation: {e}")
    
    def stop_presentation(self):
        """Stop screen presentation"""
        if not self.presenting:
            return
        
        self.presenting = False
        
        try:
            if self.client_socket:
                self.client_socket.sendall(b"TEACHER_STOP_PRESENTATION\n")
        except:
            pass
        
        self.btn_present.setText("📽️ Start Presenting")
        self.btn_present.setStyleSheet("background-color: #0078d4;")
        self.present_status.setText("Not presenting")
        self.present_status.setStyleSheet("color: #888; font-size: 10px;")
        
        self.log("⏹️ Stopped presenting screen")
    
    def _presentation_loop(self):
        """Thread loop for screen presentation (same as admin)"""
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            
            fps = 30
            frame_time = 1.0 / fps
            
            while self.presenting and self.connected:
                loop_start = time.time()
                
                try:
                    # Capture screen
                    screenshot = sct.grab(monitor)
                    frame = np.array(screenshot)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    
                    # Resize for better performance
                    h, w = frame.shape[:2]
                    new_size = (int(w * 0.7), int(h * 0.7))
                    frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
                    
                    # Encode
                    encode_params = [
                        cv2.IMWRITE_JPEG_QUALITY, SCREENSHOT_QUALITY,
                        cv2.IMWRITE_JPEG_OPTIMIZE, 1
                    ]
                    
                    success, encoded = cv2.imencode('.jpg', frame, encode_params)
                    
                    if not success:
                        continue
                    
                    frame_data = encoded.tobytes()
                    frame_size = len(frame_data)
                    
                    # Send frame to admin (admin will forward to clients)
                    header = b"TEACHER_PRESENT_FRAME\n"
                    size_bytes = struct.pack(">Q", frame_size)
                    
                    self.client_socket.sendall(header)
                    self.client_socket.sendall(size_bytes)
                    self.client_socket.sendall(frame_data)
                    
                    # Rate limiting
                    elapsed = time.time() - loop_start
                    sleep_time = max(0, frame_time - elapsed)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                
                except Exception as e:
                    self.log(f"Presentation error: {e}")
                    break
        
        self.presenting = False
        QTimer.singleShot(0, lambda: self.btn_present.setText("📽️ Start Presenting"))
        QTimer.singleShot(0, lambda: self.btn_present.setStyleSheet("background-color: #0078d4;"))
    
    def request_client_list(self):
        """Manually request client list from admin"""
        if not self.connected:
            QMessageBox.warning(self, "Not Connected", "Please connect to admin first")
            return
        
        try:
            self.log("Requesting client list from admin...")
            # Admin should send CLIENT_LIST automatically, but we can request
            # For now just log - admin sends it when clients connect/disconnect
            self.log("Client list should be automatically updated by admin")
        except Exception as e:
            self.log(f"Error requesting client list: {e}")
    
    def monitor_selected_client(self):
        """Request to monitor selected client"""
        if not self.connected:
            QMessageBox.warning(self, "Not Connected", "Please connect to admin first")
            return
        
        if self.client_list_widget.currentItem() is None:
            QMessageBox.warning(self, "No Selection", "Please select a client to monitor")
            return
        
        client_key = self.client_list_widget.currentItem().text()
        
        try:
            cmd = f"MONITOR_CLIENT:{client_key}\n"
            self.client_socket.sendall(cmd.encode("utf-8"))
            
            self.monitoring_client = client_key
            self.btn_stop_monitor.setEnabled(True)
            self.preview_info.setText(f"Monitoring: {client_key}")
            self.log(f"Started monitoring: {client_key}")
        
        except Exception as e:
            self.log(f"Failed to start monitoring: {e}")
            QMessageBox.critical(self, "Error", f"Failed to start monitoring: {e}")
    
    def stop_monitoring(self):
        """Stop monitoring client"""
        if not self.monitoring_client:
            return
        
        try:
            self.client_socket.sendall(b"STOP_MONITOR_CLIENT\n")
            
            self.monitoring_client = None
            self.btn_stop_monitor.setEnabled(False)
            self.preview_label.clear()
            self.preview_label.setText("Select a client to monitor")
            self.preview_info.setText("No client selected")
            self.log("Stopped monitoring client")
        
        except Exception as e:
            self.log(f"Failed to stop monitoring: {e}")
    
    def quit_application(self):
        self.running = False
        self.reconnect_scheduled = False
        self.disconnect_socket()
        QApplication.quit()
    
    def closeEvent(self, event):
        if hasattr(self, 'tray_icon') and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            try:
                self.tray_icon.showMessage(
                    "Teacher Client",
                    "Minimized to tray",
                    QSystemTrayIcon.Information,
                    2000
                )
            except:
                pass
        else:
            event.ignore()
            self.showMinimized()


def main():
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        
        window = TeacherClient()
        window.show()
        
        sys.exit(app.exec_())
    
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()