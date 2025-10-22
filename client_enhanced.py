"""
Lab Manager - Student Client - WITH BACKUP/RESTORE
Supports file backup and restore operations from admin
"""

import sys
import os
import socket
import threading
import struct
import time
import json
import hashlib
import zipfile
import shutil
from datetime import datetime
import traceback
import io

try:
    import mss
    import cv2
    import numpy as np
    from PIL import ImageGrab
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install: pip install mss opencv-python numpy pillow PyQt5")
    sys.exit(1)

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QPushButton,
    QMessageBox, QTextEdit, QProgressBar, QHBoxLayout,
    QSystemTrayIcon, QMenu, QAction, QInputDialog
)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QPixmap, QIcon, QFont, QImage, QPainter, QColor
from PyQt5.QtCore import QByteArray

# Configuration
SERVER_HOST = '192.168.100.30'  # Change to admin IP if on different computer
SERVER_PORT = 5001
BUFFER_SIZE = 65536
RECONNECT_DELAY = 5000  # 5 seconds
SCREENSHOT_QUALITY = 60
CHUNK_SIZE = 4 * 1024 * 1024
SOCKET_SEND_BUFFER = 16 * 1024 * 1024
SOCKET_RECV_BUFFER = 16 * 1024 * 1024
BATCH_ACK_SIZE = 10

RESUME_METADATA_DIR = os.path.join(os.path.expanduser("~"), "lab_transfer_cache_client")
RESTORE_TEMP_DIR = os.path.join(os.path.expanduser("~"), "lab_restore_temp")  # NEW: Temporary restore location
os.makedirs(RESUME_METADATA_DIR, exist_ok=True)
os.makedirs(RESTORE_TEMP_DIR, exist_ok=True)  # NEW


def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


class SignalHandler(QObject):
    update_status = pyqtSignal(str, str)
    show_message = pyqtSignal(str, str)
    file_progress = pyqtSignal(int, str)
    log_message = pyqtSignal(str)


class LockSignals(QObject):
    lock_requested = pyqtSignal(str)
    unlock_requested = pyqtSignal()


class ResumableFileReceiver:
    def __init__(self, transfer_id, filename, destination, filesize, total_chunks):
        self.transfer_id = transfer_id
        self.filename = filename
        self.destination = destination
        self.filesize = filesize
        self.total_chunks = total_chunks
        self.metadata_file = os.path.join(RESUME_METADATA_DIR, f"{transfer_id}.json")
        self.received_chunks = {}
        self.temp_file = None
        self._load_progress()
    
    def _calculate_chunk_checksum(self, data):
        return hashlib.md5(data).hexdigest()
    
    def _load_progress(self):
        try:
            if os.path.exists(self.metadata_file):
                with open(self.metadata_file, 'r') as f:
                    metadata = json.load(f)
                    self.received_chunks = {int(k): v for k, v in metadata.get('received_chunks', {}).items()}
                    self.temp_file = metadata.get('temp_file')
        except:
            self.received_chunks = {}
    
    def _save_progress(self):
        try:
            metadata = {
                'transfer_id': self.transfer_id,
                'filename': self.filename,
                'destination': self.destination,
                'filesize': self.filesize,
                'total_chunks': self.total_chunks,
                'received_chunks': {str(k): v for k, v in self.received_chunks.items()},
                'temp_file': self.temp_file,
                'last_update': time.time()
            }
            with open(self.metadata_file, 'w') as f:
                json.dump(metadata, f)
        except:
            pass
    
    def is_chunk_received(self, chunk_index):
        return chunk_index in self.received_chunks
    
    def get_progress(self):
        return (len(self.received_chunks) / self.total_chunks) * 100 if self.total_chunks > 0 else 0
    
    def is_complete(self):
        return len(self.received_chunks) == self.total_chunks
    
    def cleanup(self):
        try:
            if os.path.exists(self.metadata_file):
                os.remove(self.metadata_file)
        except:
            pass


class PresentationOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__()
        self.parent_window = parent
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setStyleSheet("QWidget { background-color: #000000; }")
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background-color: #000000; color: white;")
        self.image_label.setText("📽️ Connecting to presentation...")
        self.image_label.setFont(QFont("Segoe UI", 24))
        layout.addWidget(self.image_label)
        
        info_layout = QHBoxLayout()
        info_layout.setContentsMargins(20, 10, 20, 10)
        
        self.info_label = QLabel("📽️ Presentation Mode - Admin is presenting")
        self.info_label.setStyleSheet("""
            color: white;
            background-color: rgba(0, 120, 212, 180);
            padding: 8px 15px;
            border-radius: 5px;
            font-weight: bold;
        """)
        info_layout.addWidget(self.info_label)
        info_layout.addStretch()
        
        layout.addLayout(info_layout)
        self.setLayout(layout)
    
    def update_frame(self, image_data):
        try:
            qimg = QImage.fromData(QByteArray(image_data))
            if not qimg.isNull():
                pix = QPixmap.fromImage(qimg)
                scaled = pix.scaled(
                    self.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled)
        except:
            pass
    
    def showFullScreen(self):
        super().showFullScreen()
        self.setFocus()
        self.raise_()
        self.activateWindow()
    
    def keyPressEvent(self, event):
        event.ignore()
    
    def mousePressEvent(self, event):
        event.ignore()


class LockOverlay(QWidget):
    """Full-screen overlay that blocks all input and shows a lock message"""
    
    def __init__(self, message="🔒 Locked by Administrator", logo_path=None, parent=None):
        super().__init__()
        self.parent_window = parent
        self.unlocked = False
        self.logo_pixmap = None
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        
        self.setStyleSheet("""
            QWidget {
                background-color: #000000;
            }
        """)
        
        if logo_path and os.path.exists(logo_path):
            try:
                self.logo_pixmap = QPixmap(logo_path)
                if self.logo_pixmap.isNull():
                    self.logo_pixmap = None
            except:
                self.logo_pixmap = None
        
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(30)
        layout.setContentsMargins(50, 50, 50, 50)
        
        label = QLabel(message)
        label.setStyleSheet("""
            color: white; 
            font-size: 48px; 
            font-weight: bold;
            background-color: transparent;
        """)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        
        self.setLayout(layout)
    
    def showFullScreen(self):
        super().showFullScreen()
        self.setFocus()
        self.raise_()
        self.activateWindow()
    
    def keyPressEvent(self, event):
        event.ignore()
    
    def mousePressEvent(self, event):
        event.ignore()


class StudentClient(QWidget):
    def __init__(self):
        super().__init__()
        self.client_socket = None
        self.connected = False
        self.running = True
        self.reconnect_scheduled = False
        self.locked = False
        self.screen_sharing = False
        
        self.lock_overlay = None
        self.presentation_overlay = None
        
        self.signals = SignalHandler()
        self.lock_signals = LockSignals()
        
        self.signals.update_status.connect(self.update_status_label)
        self.signals.show_message.connect(self.display_message)
        self.signals.file_progress.connect(self.update_file_progress)
        self.signals.log_message.connect(self.log)
        
        self.lock_signals.lock_requested.connect(self.show_lock_screen)
        self.lock_signals.unlock_requested.connect(self.hide_lock_screen)
        
        self.init_ui()
        self.schedule_reconnect()
    
    def init_ui(self):
        self.setWindowTitle("Student Lab Client")
        self.setGeometry(100, 100, 500, 700)
        
        layout = QVBoxLayout()
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        title = QLabel("🖥️ Lab Client")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #0078d4;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        self.status_label = QLabel("⚪ Disconnected")
        self.status_label.setStyleSheet("""
            font-size: 16px; 
            padding: 10px; 
            background-color: #f0f0f0; 
            border-radius: 5px;
            color: #d13438;
            font-weight: bold;
        """)
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)
        
        self.info_label = QLabel(f"Server: {SERVER_HOST}:{SERVER_PORT}")
        self.info_label.setStyleSheet("font-size: 12px; color: #666;")
        self.info_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.info_label)
        
        button_layout = QHBoxLayout()
        
        self.share_screen_button = QPushButton("📷 Share Screen")
        self.share_screen_button.clicked.connect(self.toggle_screen_share)
        self.share_screen_button.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                padding: 12px;
                font-size: 14px;
                border-radius: 5px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        button_layout.addWidget(self.share_screen_button)
        
        layout.addLayout(button_layout)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #0078d4;
                border-radius: 5px;
                text-align: center;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
        """)
        layout.addWidget(self.progress_bar)
        
        self.progress_label = QLabel("No active transfers")
        self.progress_label.setStyleSheet("font-size: 12px; color: #666;")
        layout.addWidget(self.progress_label)
        
        log_label = QLabel("📋 Activity Log:")
        log_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(log_label)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                font-family: Consolas, monospace;
                font-size: 11px;
                border: 1px solid #444;
                border-radius: 5px;
            }
        """)
        layout.addWidget(self.log_text)
        
        footer = QLabel("© Lab Manager Client v2.0")
        footer.setStyleSheet("font-size: 10px; color: #999;")
        footer.setAlignment(Qt.AlignCenter)
        layout.addWidget(footer)
        
        self.setLayout(layout)
        
        self.setup_system_tray()
    
    def setup_system_tray(self):
        try:
            self.tray_icon = QSystemTrayIcon(self)
            self.tray_icon.setIcon(QIcon.fromTheme("computer"))
            
            tray_menu = QMenu()
            show_action = QAction("Show", self)
            show_action.triggered.connect(self.show)
            tray_menu.addAction(show_action)
            
            quit_action = QAction("Quit", self)
            quit_action.triggered.connect(self.quit_application)
            tray_menu.addAction(quit_action)
            
            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.show()
        except:
            pass
    
    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")
    
    def update_status_label(self, text, color):
        color_map = {
            "green": "#107c10",
            "red": "#d13438",
            "yellow": "#f7b900",
            "blue": "#0078d4"
        }
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"""
            font-size: 16px; 
            padding: 10px; 
            background-color: #f0f0f0; 
            border-radius: 5px;
            color: {color_map.get(color, color)};
            font-weight: bold;
        """)
    
    def schedule_reconnect(self):
        if not self.reconnect_scheduled:
            self.reconnect_scheduled = True
            QTimer.singleShot(100, self.attempt_connection)
    
    def attempt_connection(self):
        if not self.running:
            return
        
        if self.connected:
            self.reconnect_scheduled = False
            return
        
        try:
            self.log(f"🔄 Connecting to {SERVER_HOST}:{SERVER_PORT}...")
            self.signals.update_status.emit("🔄 Connecting...", "yellow")
            
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(5)
            self.client_socket.connect((SERVER_HOST, SERVER_PORT))
            
            try:
                self.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
                self.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
            except:
                pass
            
            self.connected = True
            self.reconnect_scheduled = False
            self.log("✅ Connected to server")
            self.signals.update_status.emit("✅ Connected", "green")
            
            hostname = socket.gethostname()
            info = {"hostname": hostname, "status": "ready"}
            info_json = json.dumps(info)
            self.client_socket.sendall(f"INFO:{info_json}\n".encode("utf-8"))
            
            threading.Thread(target=self.receive_loop, daemon=True).start()
        
        except Exception as e:
            self.log(f"❌ Connection failed: {e}")
            self.disconnect_socket()
            self.signals.update_status.emit("⚪ Disconnected", "red")
            QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)
    
    def disconnect_socket(self):
        self.connected = False
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
            self.client_socket = None
    
    def receive_loop(self):
        buffer = b""
        
        while self.connected and self.running:
            try:
                chunk = self.client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                
                buffer += chunk
                
                while b"\n" in buffer:
                    idx = buffer.find(b"\n")
                    line = buffer[:idx]
                    buffer = buffer[idx + 1:]
                    
                    if line.startswith(b"INIT"):
                        buffer = self.handle_file_transfer_init(buffer)
                    
                    elif line.startswith(b"LOCK:"):
                        message = line[5:].decode("utf-8", errors="ignore")
                        self.lock_signals.lock_requested.emit(message)
                    
                    elif line == b"UNLOCK":
                        self.lock_signals.unlock_requested.emit()
                    
                    elif line.startswith(b"MESSAGE:"):
                        message = line[8:].decode("utf-8", errors="ignore")
                        self.signals.show_message.emit("Message from Admin", message)
                    
                    elif line == b"REQUEST_SCREENSHOT":
                        threading.Thread(target=self.send_screen_once, daemon=True).start()
                    
                    elif line == b"PRESENT_START":
                        self.start_presentation_mode()
                    
                    elif line == b"PRESENT_STOP":
                        self.stop_presentation_mode()
                    
                    elif line.startswith(b"PRESENT_FRAME"):
                        if len(buffer) >= 8:
                            size = struct.unpack(">Q", buffer[:8])[0]
                            buffer = buffer[8:]
                            
                            while len(buffer) < size:
                                chunk = self.client_socket.recv(BUFFER_SIZE)
                                if not chunk:
                                    break
                                buffer += chunk
                            
                            frame_data = buffer[:size]
                            buffer = buffer[size:]
                            
                            if self.presentation_overlay:
                                self.presentation_overlay.update_frame(frame_data)
                    
                    # NEW: Handle backup request
                    elif line.startswith(b"BACKUP_REQUEST:"):
                        source_path = line[15:].decode("utf-8", errors="ignore")
                        threading.Thread(target=self.handle_backup_request, 
                                       args=(source_path,), daemon=True).start()
                    
                    # NEW: Handle restore request
                    elif line.startswith(b"RESTORE_START:"):
                        restore_path = line[14:].decode("utf-8", errors="ignore")
                        self.restore_target_path = restore_path
                        self.log(f"📥 Restore initiated to: {restore_path}")
            
            except Exception as e:
                if self.connected:
                    self.log(f"⚠️ Receive error: {e}")
                break
        
        self.log("🔌 Disconnected from server")
        self.disconnect_socket()
        self.signals.update_status.emit("⚪ Disconnected", "red")
        
        if self.running:
            QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)
    
    # NEW: Handle backup request from admin
    def handle_backup_request(self, source_path):
        try:
            self.log(f"💾 Backup requested: {source_path}")
            
            if not os.path.exists(source_path):
                error_msg = f"Path not found: {source_path}"
                self.log(f"❌ {error_msg}")
                self.client_socket.sendall(f"BACKUP_ERROR:{error_msg}\n".encode("utf-8"))
                return
            
            # Create temporary zip file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_zip = os.path.join(RESUME_METADATA_DIR, f"backup_{timestamp}.zip")
            
            self.log(f"📦 Creating backup archive...")
            
            with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if os.path.isfile(source_path):
                    zipf.write(source_path, os.path.basename(source_path))
                else:
                    for root, dirs, files in os.walk(source_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, source_path)
                            try:
                                zipf.write(file_path, arcname)
                            except Exception as e:
                                self.log(f"⚠️ Skipped: {file} ({e})")
            
            # Read zip file
            with open(temp_zip, 'rb') as f:
                zip_data = f.read()
            
            # Send backup data
            self.log(f"📤 Sending backup ({format_bytes(len(zip_data))})...")
            header = f"BACKUP_DATA:{len(zip_data)}\n".encode("utf-8")
            self.client_socket.sendall(header + zip_data)
            
            # Clean up
            try:
                os.remove(temp_zip)
            except:
                pass
            
            self.log(f"✅ Backup sent successfully")
            self.signals.show_message.emit("Backup Complete", 
                                          f"Files backed up to admin from:\n{source_path}")
        
        except Exception as e:
            error_msg = f"Backup failed: {e}"
            self.log(f"❌ {error_msg}")
            try:
                self.client_socket.sendall(f"BACKUP_ERROR:{error_msg}\n".encode("utf-8"))
            except:
                pass
    
    def handle_file_transfer_init(self, buffer):
        try:
            if len(buffer) < 8:
                return buffer
            
            size = struct.unpack(">Q", buffer[:8])[0]
            buffer = buffer[8:]
            
            while len(buffer) < size:
                chunk = self.client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                buffer += chunk
            
            init_data = buffer[:size]
            buffer = buffer[size:]
            
            init_info = json.loads(init_data.decode("utf-8"))
            
            transfer_id = init_info["transfer_id"]
            filename = init_info["filename"]
            destination = init_info["destination"]
            filesize = init_info["filesize"]
            total_chunks = init_info["total_chunks"]
            chunk_size = init_info["chunk_size"]
            
            # NEW: Check if this is a restore operation
            is_restore = destination == "RESTORE_TEMP"
            
            if is_restore:
                filepath = os.path.join(RESTORE_TEMP_DIR, filename)
                self.log(f"📥 Receiving restore file: {filename}")
            else:
                filepath = self._resolve_destination_path(destination, filename)
                if not filepath:
                    return buffer
                self.log(f"📥 Receiving: {filename} ({format_bytes(filesize)})")
            
            self.signals.file_progress.emit(0, f"Starting: {filename}")
            
            receiver = ResumableFileReceiver(transfer_id, filename, destination, filesize, total_chunks)
            
            chunk_data_map = {}
            chunks_since_ack = 0
            last_update = time.time()
            
            try:
                while not receiver.is_complete():
                    if len(buffer) < 6:
                        chunk = self.client_socket.recv(BUFFER_SIZE)
                        if not chunk:
                            break
                        buffer += chunk
                    
                    if buffer[:6] == b"CHUNK\n":
                        buffer = buffer[6:]
                        
                        while len(buffer) < 16:
                            chunk = self.client_socket.recv(BUFFER_SIZE)
                            if not chunk:
                                break
                            buffer += chunk
                        
                        chunk_index, chunk_len = struct.unpack(">QQ", buffer[:16])
                        buffer = buffer[16:]
                        
                        while len(buffer) < chunk_len:
                            chunk = self.client_socket.recv(BUFFER_SIZE)
                            if not chunk:
                                break
                            buffer += chunk
                        
                        chunk_data = buffer[:chunk_len]
                        buffer = buffer[chunk_len:]
                        
                        if not receiver.is_chunk_received(chunk_index):
                            chunk_data_map[chunk_index] = chunk_data
                            checksum = receiver._calculate_chunk_checksum(chunk_data)
                            receiver.received_chunks[chunk_index] = checksum
                            chunks_since_ack += 1
                        
                        progress = receiver.get_progress()
                        current_time = time.time()
                        if current_time - last_update >= 0.5:
                            self.signals.file_progress.emit(int(progress), 
                                                           f"{filename}: {progress:.1f}%")
                            last_update = time.time()
                    
                    elif buffer[:18] == b"TRANSFER_COMPLETE\n":
                        buffer = buffer[18:]
                        break
                    
                    if chunks_since_ack >= BATCH_ACK_SIZE:
                        receiver._save_progress()
                        chunks_since_ack = 0
                        last_update = time.time()
                
                if chunks_since_ack > 0:
                    try:
                        self.client_socket.sendall(b"CHUNK_OK\n")
                    except:
                        pass
            
            except Exception as e:
                self.log(f"Transfer error: {e}")
                raise
            
            if receiver.is_complete():
                self.log(f"Writing to disk...")
                try:
                    with open(filepath, "wb") as f:
                        for i in range(total_chunks):
                            if i in chunk_data_map:
                                f.write(chunk_data_map[i])
                    
                    try:
                        self.client_socket.sendall(b"VERIFIED\n")
                    except:
                        pass
                    
                    receiver.cleanup()
                    self.signals.file_progress.emit(100, f"Complete: {filename}")
                    self.log(f"✅ Saved: {filepath}")
                    
                    # NEW: Handle restore extraction
                    if is_restore:
                        threading.Thread(target=self.extract_restore_files, 
                                       args=(filepath,), daemon=True).start()
                    else:
                        self.signals.show_message.emit("File Received", f"Saved to:\n{filepath}")
                    
                    QTimer.singleShot(3000, lambda: self.signals.file_progress.emit(0, ""))
                
                except Exception as e:
                    self.log(f"Write error: {e}")
                    raise
        
        except Exception as e:
            self.log(f"Transfer error: {e}")
            self.signals.file_progress.emit(0, f"Error")
        
        return buffer
    
    # NEW: Extract and restore files
    def extract_restore_files(self, zip_path):
        try:
            self.log(f"📦 Extracting restore files...")
            
            restore_path = getattr(self, 'restore_target_path', None)
            if not restore_path:
                restore_path = os.path.join(os.path.expanduser("~"), "Documents", "RestoredFiles")
            
            os.makedirs(restore_path, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(restore_path)
            
            # Clean up
            try:
                os.remove(zip_path)
            except:
                pass
            
            self.log(f"✅ Files restored to: {restore_path}")
            self.signals.show_message.emit("Restore Complete", 
                                          f"Files restored to:\n{restore_path}")
        
        except Exception as e:
            self.log(f"❌ Restore extraction failed: {e}")
            self.signals.show_message.emit("Restore Error", f"Failed to extract files: {e}")
    
    def _resolve_destination_path(self, destination, filename):
        try:
            home = os.path.expanduser("~")
            
            if destination.lower() == "downloads":
                base_path = os.path.join(home, "Downloads")
            elif destination.lower() == "desktop":
                base_path = os.path.join(home, "Desktop")
            elif destination.lower() == "documents":
                base_path = os.path.join(home, "Documents")
            else:
                base_path = destination
            
            base_path = os.path.abspath(base_path)
            filepath = os.path.join(base_path, filename)
            filepath = os.path.abspath(filepath)
            
            if os.path.exists(filepath):
                base, ext = os.path.splitext(filepath)
                counter = 1
                while os.path.exists(f"{base}_{counter}{ext}"):
                    counter += 1
                filepath = f"{base}_{counter}{ext}"
            
            return filepath
        except:
            return None
    
    def display_message(self, title, message):
        QMessageBox.information(self, title, message)
        self.log(f"Message: {message}")
    
    def update_file_progress(self, percentage, status):
        if percentage > 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(percentage)
            self.progress_label.setText(status)
        else:
            self.progress_bar.setVisible(False)
            self.progress_label.setText(status if status else "No active transfers")
    
    def send_screen_once(self):
        if not self.connected:
            return
        
        try:
            self.log("📸 Capturing screenshot...")
            screenshot = ImageGrab.grab()
            
            buffer = io.BytesIO()
            screenshot.save(buffer, format='JPEG', quality=SCREENSHOT_QUALITY, optimize=True)
            data = buffer.getvalue()
            
            header = b"FRAME\n"
            size = struct.pack(">Q", len(data))
            
            self.client_socket.sendall(header + size + data)
            self.log(f"✅ Screenshot sent ({len(data)//1024} KB)")
        except Exception as e:
            self.log(f"Screenshot error: {e}")
    
    def toggle_screen_share(self):
        if getattr(self, 'sharing_active', False):
            self.stop_screen_share()
            self.share_screen_button.setText("📷 Share Screen")
        else:
            self.start_screen_share()
            self.share_screen_button.setText("🛑 Stop Sharing")
    
    def start_screen_share(self):
        if getattr(self, 'sharing_active', False):
            return
        
        self.sharing_active = True
        self.log("📹 Screen sharing started")
        
        def share_loop():
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while self.sharing_active and self.connected:
                    try:
                        frame = np.array(sct.grab(monitor))
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                        
                        h, w = frame.shape[:2]
                        frame = cv2.resize(frame, (w//2, h//2))
                        
                        _, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 40])
                        data = encoded.tobytes()
                        
                        header = b"FRAME\n" + struct.pack(">Q", len(data))
                        self.client_socket.sendall(header + data)
                        
                        time.sleep(0.033)
                    except:
                        break
            
            self.sharing_active = False
        
        threading.Thread(target=share_loop, daemon=True).start()
    
    def stop_screen_share(self):
        if getattr(self, 'sharing_active', False):
            self.sharing_active = False
            self.log("🛑 Screen sharing stopped")
    
    def start_presentation_mode(self):
        if not self.presentation_overlay:
            self.presentation_overlay = PresentationOverlay(self)
        
        self.presentation_overlay.showFullScreen()
        self.log("📽️ Presentation mode started")
    
    def stop_presentation_mode(self):
        if self.presentation_overlay:
            self.presentation_overlay.close()
            self.presentation_overlay = None
        
        self.log("🛑 Presentation mode stopped")
    
    def show_lock_screen(self, message):
        if self.locked:
            return
        
        self.locked = True
        self.lock_overlay = LockOverlay(message, parent=self)
        self.lock_overlay.showFullScreen()
        self.log(f"🔒 Screen locked: {message}")
    
    def hide_lock_screen(self):
        if not self.locked:
            return
        
        self.locked = False
        if self.lock_overlay:
            self.lock_overlay.close()
            self.lock_overlay = None
        
        self.log("🔓 Screen unlocked")
    
    def quit_application(self):
        self.running = False
        self.reconnect_scheduled = False
        self.disconnect_socket()
        QApplication.quit()
    
    def closeEvent(self, event):
        if self.locked:
            event.ignore()
            return
        
        if hasattr(self, 'tray_icon') and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
            try:
                self.tray_icon.showMessage("Student Client", "Minimized to tray", QSystemTrayIcon.Information, 2000)
            except:
                pass
        else:
            event.ignore()
            self.showMinimized()


def main():
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        
        window = StudentClient()
        window.show()
        
        sys.exit(app.exec_())
    
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
