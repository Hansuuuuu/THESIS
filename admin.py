# admin_server.py - Enhanced Admin Server
# Full-featured controller for student/teacher clients with improved UI and features

import sys
import os
import socket
import threading
import struct
import json
import time
from datetime import datetime
from queue import Queue, Empty
from collections import defaultdict

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QTextEdit, QSizePolicy, QSplitter, QInputDialog,
    QGroupBox, QCheckBox, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar
)
from PyQt5.QtCore import Qt, QTimer, QByteArray
from PyQt5.QtGui import QPixmap, QImage, QFont, QColor

# ============ Configuration ============
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5001
RECV_BUFFER = 65536
MAX_IMAGE_SIZE = 200 * 1024 * 1024  # 200 MB
INBOX_DIR = os.path.join(os.path.expanduser("~"), "lab_inbox_admin")
os.makedirs(INBOX_DIR, exist_ok=True)

# ============ Helper Functions ============
def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def format_bytes(bytes_size):
    """Format bytes to human readable"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} TB"

# ============ Client Handler ============
class ClientHandler:
    """Enhanced client handler with better protocol support"""
    
    def __init__(self, sock: socket.socket, addr, server):
        self.sock = sock
        self.addr = addr
        self.server = server
        self.key = f"{addr[0]}:{addr[1]}"
        self.thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.running = threading.Event()
        self.running.set()
        self.lock = threading.Lock()
        
        # Statistics
        self.last_image = None
        self.last_image_ts = None
        self.connected_time = time.time()
        self.frames_received = 0
        self.bytes_received = 0
        self.files_received = 0
        self.is_streaming = False
        self.last_heartbeat = time.time()
        self.client_info = {
            "hostname": addr[0],
            "status": "connected"
        }

    def start(self):
        self.thread.start()

    def stop(self):
        self.running.clear()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            self.sock.close()
        except:
            pass

    def send_command(self, cmd_str: str):
        """Send command to client"""
        try:
            data = (cmd_str + "\n").encode("utf-8")
            with self.lock:
                self.sock.sendall(data)
            self.server.log(f"✉️ Sent to {self.key}: {cmd_str}")
            return True
        except Exception as e:
            self.server.log(f"❌ Send error to {self.key}: {e}")
            return False

    def send_file(self, filepath: str):
        """Send file to client"""
        if not os.path.exists(filepath):
            self.server.log(f"❌ File not found: {filepath}")
            return False
        
        try:
            basename = os.path.basename(filepath)
            filesize = os.path.getsize(filepath)
            
            self.server.log(f"📤 Sending {basename} ({format_bytes(filesize)}) to {self.key}")
            
            header = f"SEND_FILE:{basename}\n".encode("utf-8")
            with self.lock:
                self.sock.sendall(header)
                time.sleep(0.05)
                
                sent = 0
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(RECV_BUFFER)
                        if not chunk:
                            break
                        self.sock.sendall(chunk)
                        sent += len(chunk)
                
                self.sock.sendall(b"<END>")
            
            self.server.log(f"✅ File sent successfully: {basename}")
            return True
            
        except Exception as e:
            self.server.log(f"❌ File send error: {e}")
            return False

    def _reader_loop(self):
        """Read data from client"""
        sock = self.sock
        sock.settimeout(30.0)  # 30 second timeout - long enough for heartbeats
        
        try:
            buffer = b""
            
            while self.running.is_set():
                try:
                    # Read data
                    data = sock.recv(RECV_BUFFER)
                    if not data:
                        self.server.log(f"⚠️ Client {self.key} closed connection")
                        break
                    
                    buffer += data
                    
                    # Process complete messages (ending with newline)
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        header = line.decode('utf-8', errors='ignore').strip()
                        
                        if not header:
                            continue
                        
                        try:
                            # Process the header - wrap in try/except to avoid disconnect on errors
                            if header.upper() == "FRAME":
                                # Read 8-byte size
                                while len(buffer) < 8:
                                    chunk = sock.recv(RECV_BUFFER)
                                    if not chunk:
                                        raise ConnectionError("Connection closed while reading frame size")
                                    buffer += chunk
                                
                                size_bytes = buffer[:8]
                                buffer = buffer[8:]
                                size = struct.unpack(">Q", size_bytes)[0]
                                
                                if size <= 0 or size > MAX_IMAGE_SIZE:
                                    self.server.log(f"⚠️ Invalid frame size from {self.key}: {size}")
                                    continue
                                
                                # Read frame data
                                while len(buffer) < size:
                                    chunk = sock.recv(min(RECV_BUFFER, size - len(buffer)))
                                    if not chunk:
                                        raise ConnectionError("Connection closed while reading frame data")
                                    buffer += chunk
                                
                                frame_data = buffer[:size]
                                buffer = buffer[size:]
                                
                                self.last_image = frame_data
                                self.last_image_ts = time.time()
                                self.frames_received += 1
                                self.bytes_received += len(frame_data)
                                
                                self.server.on_client_frame(self.key, frame_data)
                                
                            elif header.upper() in ("FILE_BACK", "FILE"):
                                # Handle file upload
                                self._receive_file_from_buffer(sock, buffer)
                                buffer = b""  # Clear buffer after file
                            
                            elif header.upper() == "HEARTBEAT":
                                # Heartbeat received - update timestamp
                                self.last_heartbeat = time.time()
                                # Don't log every heartbeat to reduce noise
                                
                            elif header.upper().startswith("STATUS"):
                                self.server.log(f"📊 Status from {self.key}: {header}")
                                
                            elif header.upper().startswith("MSG"):
                                self.server.log(f"💬 Message from {self.key}: {header}")
                                
                            else:
                                # Log unknown header
                                self.server.log(f"📝 From {self.key}: {header}")
                        
                        except Exception as header_error:
                            self.server.log(f"⚠️ Error processing header '{header}' from {self.key}: {header_error}")
                            # Don't break connection, just skip this message
                            continue
                    
                except socket.timeout:
                    # Check if client is still alive (heartbeat should come every 10s)
                    time_since_heartbeat = time.time() - self.last_heartbeat
                    if time_since_heartbeat > 60:  # 60 seconds without heartbeat
                        self.server.log(f"⏱️ Client {self.key} timed out (no heartbeat for {int(time_since_heartbeat)}s)")
                        break
                    # Otherwise timeout is normal, continue
                    continue
                except Exception as e:
                    self.server.log(f"⚠️ Read error from {self.key}: {e}")
                    import traceback
                    self.server.log(f"Traceback: {traceback.format_exc()}")
                    break
                    
        except Exception as e:
            self.server.log(f"❌ Handler error for {self.key}: {e}")
        finally:
            self.running.clear()
            self.client_info["status"] = "disconnected"
            self.server.log(f"❌ {self.key} disconnected")
            self.server.remove_client(self.key)

    def _receive_file_from_buffer(self, sock, initial_buffer):
        """Receive file data"""
        try:
            buffer = initial_buffer
            
            # Read metadata line
            while b'\n' not in buffer:
                chunk = sock.recv(RECV_BUFFER)
                if not chunk:
                    return
                buffer += chunk
            
            meta_line, buffer = buffer.split(b'\n', 1)
            metadata = {}
            try:
                meta_str = meta_line.decode('utf-8', errors='ignore').strip()
                if meta_str:
                    metadata = json.loads(meta_str)
            except:
                metadata = {"filename": meta_line.decode('utf-8', errors='ignore')}
            
            # Read file size (8 bytes)
            while len(buffer) < 8:
                chunk = sock.recv(RECV_BUFFER)
                if not chunk:
                    return
                buffer += chunk
            
            size_bytes = buffer[:8]
            buffer = buffer[8:]
            filesize = struct.unpack(">Q", size_bytes)[0]
            
            if filesize < 0 or filesize > 10 * 1024 * 1024 * 1024:
                self.server.log(f"⚠️ Invalid file size from {self.key}: {filesize}")
                return
            
            # Generate filename
            fname = metadata.get("filename") or f"{self.key.replace(':','_')}_{int(time.time())}"
            fname = os.path.basename(fname)
            
            outpath = os.path.join(INBOX_DIR, fname)
            tmp_path = outpath + ".part"
            
            self.server.log(f"📥 Receiving file from {self.key}: {fname} ({format_bytes(filesize)})")
            
            # Write file
            with open(tmp_path, "wb") as outf:
                # Write what we already have in buffer
                to_write = min(len(buffer), filesize)
                outf.write(buffer[:to_write])
                remaining = filesize - to_write
                
                # Read rest of file
                while remaining > 0:
                    chunk = sock.recv(min(RECV_BUFFER, remaining))
                    if not chunk:
                        raise ConnectionError("Connection closed during file transfer")
                    outf.write(chunk)
                    remaining -= len(chunk)
            
            # Move to final location
            try:
                os.replace(tmp_path, outpath)
            except:
                os.rename(tmp_path, outpath)
            
            self.files_received += 1
            self.server.log(f"✅ File received from {self.key}: {fname}")
            self.server.on_client_file(self.key, outpath, metadata)
            
        except Exception as e:
            self.server.log(f"❌ File receive error from {self.key}: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass

    def get_stats(self):
        """Get client statistics"""
        uptime = time.time() - self.connected_time
        return {
            "key": self.key,
            "uptime": uptime,
            "frames": self.frames_received,
            "bytes": self.bytes_received,
            "files": self.files_received,
            "streaming": self.is_streaming,
            "status": self.client_info["status"]
        }


# ============ Admin Server ============
class AdminServer:
    """Enhanced admin server with improved management"""
    
    def __init__(self, host=LISTEN_HOST, port=LISTEN_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.accept_thread = None
        self.running = threading.Event()
        self.clients = {}  # key -> ClientHandler
        self.clients_lock = threading.Lock()
        self.log_queue = Queue()
        self.frame_queue = Queue()
        
        # Statistics
        self.total_connections = 0
        self.start_time = None

    def start(self):
        """Start the server"""
        if self.running.is_set():
            return
        
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.listen(200)
            self.running.set()
            self.start_time = time.time()
            
            self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self.accept_thread.start()
            
            self.log(f"🚀 Server started on {self.host}:{self.port}")
            return True
        except Exception as e:
            self.log(f"❌ Failed to start server: {e}")
            return False

    def stop(self):
        """Stop the server"""
        self.running.clear()
        
        try:
            if self.sock:
                self.sock.close()
        except:
            pass
        
        with self.clients_lock:
            keys = list(self.clients.keys())
        
        for k in keys:
            try:
                self.clients[k].stop()
            except:
                pass
        
        self.log("🛑 Server stopped")

    def _accept_loop(self):
        """Accept incoming connections"""
        while self.running.is_set():
            try:
                conn, addr = self.sock.accept()
                key = f"{addr[0]}:{addr[1]}"
                
                handler = ClientHandler(conn, addr, self)
                handler.start()
                
                with self.clients_lock:
                    self.clients[key] = handler
                
                self.total_connections += 1
                self.log(f"✅ Client connected: {key} (Total active: {len(self.clients)})")
                
            except Exception as e:
                if self.running.is_set():
                    self.log(f"⚠️ Accept error: {e}")
                break

    def remove_client(self, key):
        """Remove disconnected client"""
        with self.clients_lock:
            if key in self.clients:
                try:
                    self.clients[key].stop()
                except:
                    pass
                del self.clients[key]
                self.log(f"🗑️ Removed client: {key} (Remaining: {len(self.clients)})")

    def broadcast_command(self, cmd_str: str):
        """Send command to all clients"""
        with self.clients_lock:
            clients = list(self.clients.values())
        
        success = 0
        for handler in clients:
            if handler.send_command(cmd_str):
                success += 1
        
        self.log(f"📢 Broadcast '{cmd_str}' to {success}/{len(clients)} clients")

    def send_file_to_clients(self, filepath: str, keys: list):
        """Send file to specific clients"""
        with self.clients_lock:
            for k in keys:
                if k in self.clients:
                    threading.Thread(
                        target=self.clients[k].send_file,
                        args=(filepath,),
                        daemon=True
                    ).start()

    def log(self, msg: str):
        """Add message to log queue"""
        timestamp = now_ts()
        self.log_queue.put(f"[{timestamp}] {msg}")

    def on_client_frame(self, client_key: str, image_bytes: bytes):
        """Handle received frame"""
        try:
            ts = int(time.time() * 1000)
            fname = os.path.join(INBOX_DIR, f"{client_key.replace(':','_')}_frame_{ts}.jpg")
            
            with open(fname, "wb") as f:
                f.write(image_bytes)
            
            # Put in frame queue for UI update
            self.frame_queue.put((client_key, image_bytes))
            
        except Exception as e:
            self.log(f"❌ Error saving frame from {client_key}: {e}")

    def on_client_file(self, client_key: str, filepath: str, metadata: dict):
        """Handle received file"""
        self.log(f"📁 File received from {client_key}: {os.path.basename(filepath)}")

    def list_clients(self):
        """Get list of connected clients"""
        with self.clients_lock:
            return sorted(list(self.clients.keys()))

    def get_client_stats(self, key):
        """Get statistics for a client"""
        with self.clients_lock:
            if key in self.clients:
                return self.clients[key].get_stats()
        return None

    def get_server_stats(self):
        """Get server statistics"""
        uptime = time.time() - self.start_time if self.start_time else 0
        with self.clients_lock:
            active_clients = len(self.clients)
        
        return {
            "uptime": uptime,
            "active_clients": active_clients,
            "total_connections": self.total_connections
        }


# ============ Admin GUI ============
class AdminWindow(QMainWindow):
    """Enhanced admin window with modern UI"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lab Manager - Admin Server")
        self.resize(1400, 900)
        
        # Apply modern dark theme
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #e0e0e0;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
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
            QPushButton:pressed {
                background-color: #005a9e;
            }
            QPushButton:disabled {
                background-color: #3c3c3c;
                color: #666;
            }
            QListWidget, QTextEdit, QTableWidget {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
            }
            QListWidget::item:selected, QTableWidget::item:selected {
                background-color: #094771;
            }
            QListWidget::item:hover {
                background-color: #2a2d2e;
            }
            QLabel {
                color: #e0e0e0;
            }
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QTabWidget::pane {
                border: 1px solid #3c3c3c;
                border-radius: 4px;
            }
            QTabBar::tab {
                background-color: #2d2d30;
                color: #e0e0e0;
                padding: 8px 20px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background-color: #0078d4;
            }
            QProgressBar {
                border: 1px solid #3c3c3c;
                border-radius: 3px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
        """)
        
        self.server = AdminServer()
        self.selected_preview_client = None
        
        self._build_ui()
        self._start_timers()

    def _build_ui(self):
        """Build the user interface"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Header
        header = QLabel("🎓 Lab Manager - Admin Control Panel")
        header.setFont(QFont("Segoe UI", 20, QFont.Bold))
        header.setStyleSheet("color: #0078d4; padding: 10px;")
        main_layout.addWidget(header)
        
        # Server status bar
        status_layout = QHBoxLayout()
        self.lbl_server_status = QLabel("⚫ Server: Stopped")
        self.lbl_server_status.setFont(QFont("Segoe UI", 11))
        status_layout.addWidget(self.lbl_server_status)
        
        self.lbl_clients_count = QLabel("👥 Clients: 0")
        status_layout.addWidget(self.lbl_clients_count)
        
        self.lbl_uptime = QLabel("⏱️ Uptime: 00:00:00")
        status_layout.addWidget(self.lbl_uptime)
        
        status_layout.addStretch()
        main_layout.addLayout(status_layout)
        
        # Main content area with tabs
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # Tab 1: Control Panel
        self.tab_control = self._create_control_tab()
        self.tabs.addTab(self.tab_control, "🎮 Control Panel")
        
        # Tab 2: Monitor
        self.tab_monitor = self._create_monitor_tab()
        self.tabs.addTab(self.tab_monitor, "📺 Monitor")
        
        # Tab 3: Files & Inbox
        self.tab_files = self._create_files_tab()
        self.tabs.addTab(self.tab_files, "📁 Files & Inbox")
        
        # Tab 4: Logs
        self.tab_logs = self._create_logs_tab()
        self.tabs.addTab(self.tab_logs, "📋 Logs")

    def _create_control_tab(self):
        """Create control panel tab"""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        
        # Left: Server controls
        left_group = QGroupBox("Server Controls")
        left_layout = QVBoxLayout(left_group)
        
        self.btn_start_server = QPushButton("▶️ Start Server")
        self.btn_start_server.clicked.connect(self.start_server)
        left_layout.addWidget(self.btn_start_server)
        
        self.btn_stop_server = QPushButton("⏹️ Stop Server")
        self.btn_stop_server.clicked.connect(self.stop_server)
        self.btn_stop_server.setEnabled(False)
        left_layout.addWidget(self.btn_stop_server)
        
        left_layout.addSpacing(20)
        
        # Client list
        left_layout.addWidget(QLabel("Connected Clients:"))
        self.lst_clients = QListWidget()
        self.lst_clients.setSelectionMode(QListWidget.MultiSelection)
        self.lst_clients.itemSelectionChanged.connect(self._on_client_selection_changed)
        left_layout.addWidget(self.lst_clients)
        
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self.refresh_clients)
        left_layout.addWidget(btn_refresh)
        
        layout.addWidget(left_group, 1)
        
        # Right: Actions
        right_group = QGroupBox("Client Actions")
        right_layout = QVBoxLayout(right_group)
        
        # Screen Lock Controls
        lock_group = QGroupBox("🔒 Screen Control")
        lock_layout = QVBoxLayout(lock_group)
        
        btn_lock_all = QPushButton("🔒 Lock All Screens")
        btn_lock_all.clicked.connect(lambda: self.server.broadcast_command("LOCK"))
        lock_layout.addWidget(btn_lock_all)
        
        btn_unlock_all = QPushButton("🔓 Unlock All Screens")
        btn_unlock_all.clicked.connect(lambda: self.server.broadcast_command("UNLOCK"))
        lock_layout.addWidget(btn_unlock_all)
        
        btn_lock_selected = QPushButton("🔒 Lock Selected")
        btn_lock_selected.clicked.connect(lambda: self.send_to_selected("LOCK"))
        lock_layout.addWidget(btn_lock_selected)
        
        btn_unlock_selected = QPushButton("🔓 Unlock Selected")
        btn_unlock_selected.clicked.connect(lambda: self.send_to_selected("UNLOCK"))
        lock_layout.addWidget(btn_unlock_selected)
        
        right_layout.addWidget(lock_group)
        
        # Screen Monitoring
        monitor_group = QGroupBox("📺 Screen Monitoring")
        monitor_layout = QVBoxLayout(monitor_group)
        
        btn_screenshot = QPushButton("📸 Request Screenshot")
        btn_screenshot.clicked.connect(lambda: self.send_to_selected("REQUEST_SCREEN"))
        monitor_layout.addWidget(btn_screenshot)
        
        btn_start_stream = QPushButton("▶️ Start Live View")
        btn_start_stream.clicked.connect(lambda: self.send_to_selected("START_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_start_stream)
        
        btn_stop_stream = QPushButton("⏹️ Stop Live View")
        btn_stop_stream.clicked.connect(lambda: self.send_to_selected("STOP_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_stop_stream)
        
        right_layout.addWidget(monitor_group)
        
        # File Transfer
        file_group = QGroupBox("📤 File Transfer")
        file_layout = QVBoxLayout(file_group)
        
        btn_send_file = QPushButton("📤 Send File to Selected")
        btn_send_file.clicked.connect(self.send_file_to_selected)
        file_layout.addWidget(btn_send_file)
        
        btn_send_all = QPushButton("📤 Send File to All")
        btn_send_all.clicked.connect(self.send_file_to_all)
        file_layout.addWidget(btn_send_all)
        
        right_layout.addWidget(file_group)
        
        # Messaging
        msg_group = QGroupBox("💬 Messaging")
        msg_layout = QVBoxLayout(msg_group)
        
        btn_message = QPushButton("💬 Send Message to Selected")
        btn_message.clicked.connect(self.send_message_to_selected)
        msg_layout.addWidget(btn_message)
        
        btn_broadcast = QPushButton("📢 Broadcast Message")
        btn_broadcast.clicked.connect(self.broadcast_message)
        msg_layout.addWidget(btn_broadcast)
        
        right_layout.addWidget(msg_group)
        
        right_layout.addStretch()
        
        layout.addWidget(right_group, 1)
        
        return widget

    def _create_monitor_tab(self):
        """Create monitoring tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("📺 Live Screen Preview (Select a client)"))
        
        # Image preview
        self.lbl_preview = QLabel()
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setStyleSheet("background-color: #000; border: 2px solid #3c3c3c;")
        self.lbl_preview.setMinimumSize(800, 600)
        self.lbl_preview.setText("No preview available\nSelect a client and request screen")
        layout.addWidget(self.lbl_preview)
        
        # Preview controls
        controls = QHBoxLayout()
        
        btn_save_preview = QPushButton("💾 Save Image")
        btn_save_preview.clicked.connect(self.save_preview_image)
        controls.addWidget(btn_save_preview)
        
        btn_refresh_preview = QPushButton("🔄 Refresh")
        btn_refresh_preview.clicked.connect(self.refresh_preview)
        controls.addWidget(btn_refresh_preview)
        
        controls.addStretch()
        
        self.lbl_preview_info = QLabel("No client selected")
        controls.addWidget(self.lbl_preview_info)
        
        layout.addLayout(controls)
        
        return widget

    def _create_files_tab(self):
        """Create files and inbox tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("📁 Received Files (Inbox)"))
        
        # Inbox list
        self.lst_inbox = QListWidget()
        self.lst_inbox.itemDoubleClicked.connect(self.open_inbox_file)
        layout.addWidget(self.lst_inbox)
        
        # Inbox controls
        inbox_controls = QHBoxLayout()
        
        btn_refresh_inbox = QPushButton("🔄 Refresh")
        btn_refresh_inbox.clicked.connect(self.refresh_inbox)
        inbox_controls.addWidget(btn_refresh_inbox)
        
        btn_open_folder = QPushButton("📂 Open Inbox Folder")
        btn_open_folder.clicked.connect(self.open_inbox_folder)
        inbox_controls.addWidget(btn_open_folder)
        
        btn_clear_inbox = QPushButton("🗑️ Clear Inbox")
        btn_clear_inbox.clicked.connect(self.clear_inbox)
        inbox_controls.addWidget(btn_clear_inbox)
        
        inbox_controls.addStretch()
        layout.addLayout(inbox_controls)
        
        return widget

    def _create_logs_tab(self):
        """Create logs tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("📋 Server Activity Log"))
        
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Consolas", 10))
        layout.addWidget(self.txt_log)
        
        # Log controls
        log_controls = QHBoxLayout()
        
        btn_clear_log = QPushButton("🗑️ Clear Log")
        btn_clear_log.clicked.connect(lambda: self.txt_log.clear())
        log_controls.addWidget(btn_clear_log)
        
        btn_save_log = QPushButton("💾 Save Log")
        btn_save_log.clicked.connect(self.save_log)
        log_controls.addWidget(btn_save_log)
        
        log_controls.addStretch()
        layout.addLayout(log_controls)
        
        return widget

    def _start_timers(self):
        """Start update timers"""
        # Log drain timer
        self.timer_log = QTimer(self)
        self.timer_log.setInterval(200)
        self.timer_log.timeout.connect(self._drain_logs)
        self.timer_log.start()
        
        # Clients refresh timer
        self.timer_clients = QTimer(self)
        self.timer_clients.setInterval(2000)
        self.timer_clients.timeout.connect(self.refresh_clients)
        self.timer_clients.start()
        
        # Inbox refresh timer
        self.timer_inbox = QTimer(self)
        self.timer_inbox.setInterval(3000)
        self.timer_inbox.timeout.connect(self.refresh_inbox)
        self.timer_inbox.start()
        
        # Frame update timer
        self.timer_frames = QTimer(self)
        self.timer_frames.setInterval(100)
        self.timer_frames.timeout.connect(self._update_frames)
        self.timer_frames.start()
        
        # Status update timer
        self.timer_status = QTimer(self)
        self.timer_status.setInterval(1000)
        self.timer_status.timeout.connect(self._update_status)
        self.timer_status.start()

    def _drain_logs(self):
        """Drain log queue"""
        while True:
            try:
                msg = self.server.log_queue.get_nowait()
                self.txt_log.append(msg)
                # Auto-scroll
                scrollbar = self.txt_log.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())
            except Empty:
                break

    def _update_frames(self):
        """Update preview with new frames"""
        try:
            while True:
                client_key, image_bytes = self.server.frame_queue.get_nowait()
                
                # Only update if this is the selected client
                if client_key == self.selected_preview_client:
                    self._display_image_bytes(image_bytes)
                    
        except Empty:
            pass

    def _update_status(self):
        """Update status bar"""
        if self.server.running.is_set():
            stats = self.server.get_server_stats()
            
            # Uptime
            hours, remainder = divmod(int(stats['uptime']), 3600)
            minutes, seconds = divmod(remainder, 60)
            self.lbl_uptime.setText(f"⏱️ Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
            
            # Client count
            self.lbl_clients_count.setText(f"👥 Clients: {stats['active_clients']}")

    def start_server(self):
        """Start the server"""
        if self.server.start():
            self.lbl_server_status.setText(f"🟢 Server: Running on {self.server.host}:{self.server.port}")
            self.lbl_server_status.setStyleSheet("color: #4EC9B0;")
            self.btn_start_server.setEnabled(False)
            self.btn_stop_server.setEnabled(True)
        else:
            QMessageBox.critical(self, "Error", "Failed to start server")

    def stop_server(self):
        """Stop the server"""
        reply = QMessageBox.question(
            self,
            "Stop Server",
            "Are you sure you want to stop the server?\nAll clients will be disconnected.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.server.stop()
            self.lbl_server_status.setText("⚫ Server: Stopped")
            self.lbl_server_status.setStyleSheet("color: #e0e0e0;")
            self.btn_start_server.setEnabled(True)
            self.btn_stop_server.setEnabled(False)
            self.lst_clients.clear()

    def refresh_clients(self):
        """Refresh client list"""
        keys = self.server.list_clients()
        selected = set([it.text().replace("💻 ", "") for it in self.lst_clients.selectedItems()])
        
        # Only update if the list has changed
        current_keys = set([self.lst_clients.item(i).text().replace("💻 ", "") 
                           for i in range(self.lst_clients.count())])
        
        if current_keys == set(keys):
            # No change, don't refresh to avoid flickering
            return
        
        self.lst_clients.clear()
        for k in keys:
            item = QListWidgetItem(f"💻 {k}")
            if k in selected:
                item.setSelected(True)
            self.lst_clients.addItem(item)

    def refresh_inbox(self):
        """Refresh inbox list"""
        try:
            files = sorted(os.listdir(INBOX_DIR), reverse=True)
        except:
            files = []
        
        self.lst_inbox.clear()
        for fn in files:
            path = os.path.join(INBOX_DIR, fn)
            size = format_bytes(os.path.getsize(path))
            item = QListWidgetItem(f"📄 {fn} ({size})")
            self.lst_inbox.addItem(item)

    def _get_selected_keys(self):
        """Get selected client keys"""
        return [it.text().replace("💻 ", "") for it in self.lst_clients.selectedItems()]

    def send_to_selected(self, command):
        """Send command to selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    self.server.clients[k].send_command(command)

    def send_file_to_selected(self):
        """Send file to selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send")
        if path:
            self.server.send_file_to_clients(path, keys)
            QMessageBox.information(
                self,
                "File Transfer",
                f"Sending {os.path.basename(path)} to {len(keys)} client(s)"
            )

    def send_file_to_all(self):
        """Send file to all clients"""
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send to All")
        if not path:
            return
        
        keys = self.server.list_clients()
        if not keys:
            QMessageBox.warning(self, "No Clients", "No connected clients")
            return
        
        self.server.send_file_to_clients(path, keys)
        QMessageBox.information(
            self,
            "File Transfer",
            f"Sending {os.path.basename(path)} to {len(keys)} client(s)"
        )

    def send_message_to_selected(self):
        """Send message to selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Send Message",
            "Enter message to send to selected clients:"
        )
        
        if ok and text:
            with self.server.clients_lock:
                for k in keys:
                    if k in self.server.clients:
                        self.server.clients[k].send_command(f"MESSAGE:{text}")

    def broadcast_message(self):
        """Broadcast message to all clients"""
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Broadcast Message",
            "Enter message to broadcast to all clients:"
        )
        
        if ok and text:
            self.server.broadcast_command(f"MESSAGE:{text}")

    def _on_client_selection_changed(self):
        """Handle client selection change"""
        keys = self._get_selected_keys()
        if keys:
            self.selected_preview_client = keys[0]
            self.lbl_preview_info.setText(f"Monitoring: {keys[0]}")
            self.refresh_preview()
        else:
            self.selected_preview_client = None
            self.lbl_preview_info.setText("No client selected")
            self.lbl_preview.clear()
            self.lbl_preview.setText("No preview available\nSelect a client and request screen")

    def refresh_preview(self):
        """Refresh preview image"""
        if not self.selected_preview_client:
            return
        
        with self.server.clients_lock:
            handler = self.server.clients.get(self.selected_preview_client)
        
        if handler and handler.last_image:
            self._display_image_bytes(handler.last_image)
        else:
            # Try to load latest from disk
            self._load_latest_frame_from_disk()

    def _display_image_bytes(self, img_bytes: bytes):
        """Display image from bytes"""
        try:
            qimg = QImage.fromData(QByteArray(img_bytes))
            if not qimg.isNull():
                pix = QPixmap.fromImage(qimg)
                scaled_pix = pix.scaled(
                    self.lbl_preview.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.lbl_preview.setPixmap(scaled_pix)
                return
        except Exception as e:
            self.server.log(f"❌ Error displaying image: {e}")
        
        self.lbl_preview.setText("Failed to load image")

    def _load_latest_frame_from_disk(self):
        """Load latest frame from disk for selected client"""
        if not self.selected_preview_client:
            return
        
        try:
            prefix = self.selected_preview_client.replace(":", "_")
            files = [
                f for f in os.listdir(INBOX_DIR)
                if f.startswith(prefix) and f.endswith(('.jpg', '.jpeg', '.png'))
            ]
            
            if files:
                files.sort(reverse=True)
                latest = os.path.join(INBOX_DIR, files[0])
                
                pix = QPixmap(latest)
                if not pix.isNull():
                    scaled_pix = pix.scaled(
                        self.lbl_preview.size(),
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation
                    )
                    self.lbl_preview.setPixmap(scaled_pix)
        except Exception as e:
            self.server.log(f"❌ Error loading frame from disk: {e}")

    def save_preview_image(self):
        """Save current preview image"""
        pix = self.lbl_preview.pixmap()
        if not pix or pix.isNull():
            QMessageBox.information(self, "No Image", "No preview image to save")
            return
        
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Preview Image",
            "",
            "JPEG Files (*.jpg);;PNG Files (*.png);;All Files (*.*)"
        )
        
        if filename:
            if pix.save(filename):
                QMessageBox.information(self, "Saved", f"Image saved to:\n{filename}")
            else:
                QMessageBox.warning(self, "Error", "Failed to save image")

    def open_inbox_folder(self):
        """Open inbox folder in file explorer"""
        try:
            path = os.path.realpath(INBOX_DIR)
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform.startswith("darwin"):
                os.system(f"open '{path}'")
            else:
                os.system(f"xdg-open '{path}'")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open folder: {e}")

    def open_inbox_file(self, item):
        """Open file from inbox"""
        text = item.text()
        # Remove emoji and size info
        filename = text.replace("📄 ", "").split(" (")[0]
        path = os.path.join(INBOX_DIR, filename)
        
        if not os.path.exists(path):
            QMessageBox.warning(self, "Error", "File not found")
            return
        
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform.startswith("darwin"):
                os.system(f"open '{path}'")
            else:
                os.system(f"xdg-open '{path}'")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open file: {e}")

    def clear_inbox(self):
        """Clear all files from inbox"""
        reply = QMessageBox.question(
            self,
            "Clear Inbox",
            "Are you sure you want to delete all files in the inbox?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                count = 0
                for filename in os.listdir(INBOX_DIR):
                    filepath = os.path.join(INBOX_DIR, filename)
                    try:
                        if os.path.isfile(filepath):
                            os.remove(filepath)
                            count += 1
                    except:
                        pass
                
                self.refresh_inbox()
                QMessageBox.information(self, "Cleared", f"Deleted {count} file(s)")
                self.server.log(f"🗑️ Cleared inbox: {count} files deleted")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to clear inbox: {e}")

    def save_log(self):
        """Save log to file"""
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Log",
            f"lab_manager_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt);;All Files (*.*)"
        )
        
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(self.txt_log.toPlainText())
                QMessageBox.information(self, "Saved", f"Log saved to:\n{filename}")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save log: {e}")

    def closeEvent(self, event):
        """Handle window close"""
        if self.server.running.is_set():
            reply = QMessageBox.question(
                self,
                "Exit",
                "Server is still running. Stop server and exit?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.server.stop()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ============ Main Entry Point ============
def main():
    """Main application entry point"""
    app = QApplication(sys.argv)
    app.setApplicationName("Lab Manager")
    app.setOrganizationName("LabManager")
    
    # Set application icon if available
    # app.setWindowIcon(QIcon("icon.png"))
    
    window = AdminWindow()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()