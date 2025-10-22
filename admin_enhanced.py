"""
Lab Manager - Admin Server - WITH BACKUP/RESTORE
Features: Lock/Unlock, Screen Monitoring, File Transfer, Presentation Mode, Backup/Restore Client Files
"""

import sys
import os
import socket
import threading
import struct
import time
import json
import hashlib
import shutil
import zipfile
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty
from collections import defaultdict, deque

import mss
import cv2
import numpy as np

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QFileDialog, QMessageBox,
    QTextEdit, QTabWidget, QGroupBox, QComboBox, QInputDialog, QLineEdit
)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QFont
from PyQt5.QtCore import QByteArray

# Configuration
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5001
RECV_BUFFER = 65536
MAX_IMAGE_SIZE = 200 * 1024 * 1024
SOCKET_SEND_BUFFER = 16 * 1024 * 1024
SOCKET_RECV_BUFFER = 16 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024
BATCH_ACK_SIZE = 10
CHUNK_SEND_DELAY = 0.001

INBOX_DIR = os.path.join(os.path.expanduser("~"), "lab_inbox_admin")
RESUME_METADATA_DIR = os.path.join(os.path.expanduser("~"), "lab_transfer_cache")
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "ClientBackups")  # NEW: Main backup directory
os.makedirs(INBOX_DIR, exist_ok=True)
os.makedirs(RESUME_METADATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)  # NEW

PRESENTATION_FPS = 30
PRESENTATION_QUALITY = 85
PRESENTATION_SCALE = 1.0


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


class ServerSignals(QObject):
    new_frame = pyqtSignal(str, bytes)


class ResumableFileTransfer:
    def __init__(self, filepath, destination, transfer_id=None):
        self.filepath = filepath
        self.destination = destination
        self.filesize = os.path.getsize(filepath)
        self.basename = os.path.basename(filepath)
        self.transfer_id = transfer_id or self._generate_transfer_id()
        
        if self.filesize > 1024 * 1024 * 1024:
            self.chunk_size = 8 * 1024 * 1024
        elif self.filesize > 100 * 1024 * 1024:
            self.chunk_size = 4 * 1024 * 1024
        else:
            self.chunk_size = 1 * 1024 * 1024
        
        self.total_chunks = (self.filesize + self.chunk_size - 1) // self.chunk_size
        self.metadata_file = os.path.join(RESUME_METADATA_DIR, f"{self.transfer_id}.json")
        self.completed_chunks = set()
        self.chunk_checksums = {}
        self._load_progress()
    
    def _generate_transfer_id(self):
        unique_str = f"{self.filepath}_{self.filesize}_{int(time.time())}"
        return hashlib.md5(unique_str.encode()).hexdigest()[:16]
    
    def _calculate_chunk_checksum(self, data):
        return hashlib.md5(data).hexdigest()
    
    def _load_progress(self):
        try:
            if os.path.exists(self.metadata_file):
                with open(self.metadata_file, 'r') as f:
                    metadata = json.load(f)
                    self.completed_chunks = set(metadata.get('completed_chunks', []))
                    self.chunk_checksums = metadata.get('chunk_checksums', {})
        except:
            self.completed_chunks = set()
            self.chunk_checksums = {}
    
    def _save_progress(self):
        try:
            metadata = {
                'transfer_id': self.transfer_id,
                'filepath': self.filepath,
                'destination': self.destination,
                'filesize': self.filesize,
                'total_chunks': self.total_chunks,
                'chunk_size': self.chunk_size,
                'completed_chunks': list(self.completed_chunks),
                'chunk_checksums': self.chunk_checksums,
                'last_update': time.time()
            }
            with open(self.metadata_file, 'w') as f:
                json.dump(metadata, f)
        except:
            pass
    
    def get_pending_chunks(self):
        return [i for i in range(self.total_chunks) if i not in self.completed_chunks]
    
    def mark_chunk_complete(self, chunk_index, checksum):
        self.completed_chunks.add(chunk_index)
        self.chunk_checksums[str(chunk_index)] = checksum
    
    def save_progress_batch(self):
        self._save_progress()
    
    def is_complete(self):
        return len(self.completed_chunks) == self.total_chunks
    
    def get_progress(self):
        return (len(self.completed_chunks) / self.total_chunks) * 100 if self.total_chunks > 0 else 0
    
    def cleanup(self):
        try:
            if os.path.exists(self.metadata_file):
                os.remove(self.metadata_file)
        except:
            pass


class ClientHandler:
    def __init__(self, sock, addr, server):
        self.sock = sock
        self.addr = addr
        self.server = server
        self.key = f"{addr[0]}:{addr[1]}"
        self.thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.running = threading.Event()
        self.running.set()
        self.lock = threading.Lock()
        self.transferring = threading.Event()
        
        self.last_image = None
        self.last_image_ts = None
        self.connected_time = time.time()
        self.frames_received = 0
        self.bytes_received = 0
        self.last_heartbeat = time.time()
        self.client_info = {"hostname": addr[0], "status": "connected"}
        
        # NEW: Backup/Restore tracking
        self.backup_receiving = False
        self.backup_buffer = b""
        self.expected_backup_size = 0
    
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
    
    def send_command(self, cmd_str):
        try:
            data = (cmd_str + "\n").encode("utf-8")
            with self.lock:
                self.sock.sendall(data)
            self.server.log(f"✉️ Sent to {self.key}: {cmd_str}")
            return True
        except Exception as e:
            self.server.log(f"❌ Send error to {self.key}: {e}")
            return False
    
    # NEW: Request backup from client
    def request_backup(self, source_path):
        """Request client to backup a directory"""
        try:
            cmd = f"BACKUP_REQUEST:{source_path}"
            return self.send_command(cmd)
        except Exception as e:
            self.server.log(f"❌ Backup request error: {e}")
            return False
    
    # NEW: Send restore files to client
    def send_restore(self, client_name, restore_path):
        """Send backup files back to client"""
        try:
            backup_folder = os.path.join(BACKUP_DIR, client_name)
            if not os.path.exists(backup_folder):
                self.server.log(f"❌ No backup found for {client_name}")
                return False
            
            # Create a temporary zip file
            temp_zip = os.path.join(BACKUP_DIR, f"restore_{client_name}_{int(time.time())}.zip")
            with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(backup_folder):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, backup_folder)
                        zipf.write(file_path, arcname)
            
            # Send restore command with path
            cmd = f"RESTORE_START:{restore_path}"
            self.send_command(cmd)
            time.sleep(0.5)
            
            # Send the zip file
            success = self.send_file_resumable(temp_zip, "RESTORE_TEMP")
            
            # Clean up temp zip
            try:
                os.remove(temp_zip)
            except:
                pass
            
            return success
        except Exception as e:
            self.server.log(f"❌ Restore error: {e}")
            return False
    
    def send_file_resumable(self, filepath, destination=None):
        if not os.path.exists(filepath):
            self.server.log(f"❌ File not found: {filepath}")
            return False
        
        if not self.sock or not self.running.is_set():
            self.server.log(f"⚠️ Client {self.key} not connected")
            return False
        
        self.transferring.set()
        
        try:
            transfer = ResumableFileTransfer(filepath, destination or "Downloads")
            basename = transfer.basename
            filesize = transfer.filesize
            chunk_size = transfer.chunk_size
            
            self.server.log(f"📤 Starting: {basename} ({format_bytes(filesize)})")
            
            pending_chunks = transfer.get_pending_chunks()
            if len(pending_chunks) < transfer.total_chunks:
                self.server.log(f"🔄 Resume: {len(transfer.completed_chunks)}/{transfer.total_chunks} done")
            
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
            except:
                pass
            
            init_header = {
                "command": "RESUMABLE_TRANSFER_START",
                "transfer_id": transfer.transfer_id,
                "filename": basename,
                "destination": transfer.destination,
                "filesize": filesize,
                "total_chunks": transfer.total_chunks,
                "chunk_size": chunk_size
            }
            
            init_json = json.dumps(init_header)
            init_data = init_json.encode("utf-8")
            size_bytes = struct.pack(">Q", len(init_data))
            
            with self.lock:
                self.sock.sendall(b"INIT\n" + size_bytes + init_data)
            
            time.sleep(0.2)
            
            self.server.log(f"📦 Sending {len(pending_chunks)} chunks...")
            
            with open(filepath, 'rb') as f:
                chunks_sent = 0
                start_time = time.time()
                last_log = start_time
                
                for chunk_index in pending_chunks:
                    offset = chunk_index * chunk_size
                    f.seek(offset)
                    chunk_data = f.read(chunk_size)
                    
                    if not chunk_data:
                        break
                    
                    checksum = transfer._calculate_chunk_checksum(chunk_data)
                    chunk_header = struct.pack(">QQ", chunk_index, len(chunk_data))
                    
                    with self.lock:
                        self.sock.sendall(b"CHUNK\n" + chunk_header + chunk_data)
                    
                    chunks_sent += 1
                    
                    if chunks_sent % BATCH_ACK_SIZE == 0:
                        try:
                            ack = self.sock.recv(1024)
                            if b"CHUNK_OK" in ack:
                                for i in range(chunks_sent - BATCH_ACK_SIZE, chunks_sent):
                                    idx = pending_chunks[i] if i < len(pending_chunks) else chunk_index
                                    transfer.mark_chunk_complete(idx, checksum)
                                transfer.save_progress_batch()
                        except:
                            pass
                    
                    current_time = time.time()
                    if current_time - last_log >= 2.0:
                        elapsed = current_time - start_time
                        speed = (chunks_sent * chunk_size) / elapsed if elapsed > 0 else 0
                        progress = (chunks_sent / len(pending_chunks)) * 100
                        self.server.log(f"📊 Progress: {progress:.1f}% ({format_bytes(speed)}/s)")
                        last_log = current_time
                    
                    time.sleep(CHUNK_SEND_DELAY)
                
                remaining = chunks_sent % BATCH_ACK_SIZE
                if remaining > 0:
                    try:
                        self.sock.sendall(b"CHUNK_OK\n")
                        time.sleep(0.1)
                    except:
                        pass
            
            with self.lock:
                self.sock.sendall(b"TRANSFER_COMPLETE\n")
            
            time.sleep(0.5)
            
            try:
                response = self.sock.recv(1024)
                if b"VERIFIED" in response:
                    transfer.cleanup()
                    elapsed = time.time() - start_time
                    speed = filesize / elapsed if elapsed > 0 else 0
                    self.server.log(f"✅ Transfer complete: {basename} ({format_bytes(speed)}/s)")
                    return True
            except:
                pass
            
            self.server.log(f"⚠️ Transfer may be incomplete")
            return False
        
        except Exception as e:
            self.server.log(f"❌ Transfer error: {e}")
            return False
        
        finally:
            self.transferring.clear()
    
    def _reader_loop(self):
        buffer = b""
        frame_mode = False
        frame_size = 0
        frame_data = b""
        
        while self.running.is_set():
            try:
                chunk = self.sock.recv(RECV_BUFFER)
                if not chunk:
                    break
                
                buffer += chunk
                self.bytes_received += len(chunk)
                self.last_heartbeat = time.time()
                
                # NEW: Handle backup data reception
                if self.backup_receiving:
                    self.backup_buffer += chunk
                    if len(self.backup_buffer) >= self.expected_backup_size:
                        self._process_backup_data()
                        self.backup_receiving = False
                        buffer = self.backup_buffer[self.expected_backup_size:]
                        self.backup_buffer = b""
                    continue
                
                while b"\n" in buffer or frame_mode:
                    if frame_mode:
                        needed = frame_size - len(frame_data)
                        frame_data += buffer[:needed]
                        buffer = buffer[needed:]
                        
                        if len(frame_data) >= frame_size:
                            self._process_frame(frame_data)
                            frame_mode = False
                            frame_data = b""
                            frame_size = 0
                        continue
                    
                    idx = buffer.find(b"\n")
                    if idx == -1:
                        break
                    
                    line = buffer[:idx]
                    buffer = buffer[idx + 1:]
                    
                    if line == b"FRAME":
                        if len(buffer) >= 8:
                            frame_size = struct.unpack(">Q", buffer[:8])[0]
                            buffer = buffer[8:]
                            
                            if frame_size > MAX_IMAGE_SIZE:
                                self.server.log(f"⚠️ Frame too large: {frame_size}")
                                buffer = b""
                                continue
                            
                            frame_mode = True
                            frame_data = b""
                    
                    # NEW: Handle backup response
                    elif line.startswith(b"BACKUP_DATA:"):
                        try:
                            size_str = line.decode().split(":")[1]
                            self.expected_backup_size = int(size_str)
                            self.backup_receiving = True
                            self.backup_buffer = buffer
                            buffer = b""
                        except:
                            pass
                    
                    elif line.startswith(b"BACKUP_ERROR:"):
                        error_msg = line.decode().split(":", 1)[1]
                        self.server.log(f"❌ Backup error from {self.key}: {error_msg}")
                    
                    elif line.startswith(b"INFO:"):
                        try:
                            info_json = line[5:].decode("utf-8")
                            self.client_info = json.loads(info_json)
                        except:
                            pass
            
            except Exception as e:
                if self.running.is_set():
                    self.server.log(f"⚠️ Read error {self.key}: {e}")
                break
        
        self.server.remove_client(self.key)
    
    # NEW: Process received backup data
    def _process_backup_data(self):
        try:
            # Extract client hostname
            hostname = self.client_info.get("hostname", self.key.replace(":", "_"))
            client_folder = os.path.join(BACKUP_DIR, hostname)
            
            # Clear existing backup
            if os.path.exists(client_folder):
                shutil.rmtree(client_folder)
            os.makedirs(client_folder, exist_ok=True)
            
            # Save zip file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = os.path.join(client_folder, f"backup_{timestamp}.zip")
            
            with open(zip_path, 'wb') as f:
                f.write(self.backup_buffer[:self.expected_backup_size])
            
            # Extract zip
            extract_folder = os.path.join(client_folder, "files")
            os.makedirs(extract_folder, exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_folder)
            
            # Keep the zip for reference
            self.server.log(f"✅ Backup received from {hostname}: {format_bytes(len(self.backup_buffer))}")
            self.server.log(f"📁 Saved to: {client_folder}")
            
        except Exception as e:
            self.server.log(f"❌ Failed to process backup: {e}")
    
    def _process_frame(self, data):
        self.last_image = data
        self.last_image_ts = time.time()
        self.frames_received += 1
        self.server.signals.new_frame.emit(self.key, data)


class AdminServer:
    def __init__(self):
        self.listen_socket = None
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.running = threading.Event()
        self.signals = ServerSignals()
        
        self.presenting = False
        self.presentation_thread = None
        self.presentation_targets = []
        self.presentation_fps = PRESENTATION_FPS
        self.presentation_quality = PRESENTATION_QUALITY
        self.presentation_scale = PRESENTATION_SCALE
    
    def log(self, msg):
        print(f"[{now_ts()}] {msg}")
    
    def start(self):
        if self.running.is_set():
            return
        
        try:
            self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_socket.bind((LISTEN_HOST, LISTEN_PORT))
            self.listen_socket.listen(50)
            self.log(f"🚀 Server started on {LISTEN_HOST}:{LISTEN_PORT}")
            
            self.running.set()
            threading.Thread(target=self._accept_loop, daemon=True).start()
        
        except Exception as e:
            self.log(f"❌ Failed to start: {e}")
    
    def stop(self):
        self.running.clear()
        self.stop_presentation()
        
        with self.clients_lock:
            for handler in list(self.clients.values()):
                handler.stop()
            self.clients.clear()
        
        if self.listen_socket:
            try:
                self.listen_socket.close()
            except:
                pass
        
        self.log("🛑 Server stopped")
    
    def _accept_loop(self):
        while self.running.is_set():
            try:
                conn, addr = self.listen_socket.accept()
                self.log(f"🔌 New connection: {addr}")
                
                handler = ClientHandler(conn, addr, self)
                with self.clients_lock:
                    self.clients[handler.key] = handler
                handler.start()
            
            except Exception as e:
                if self.running.is_set():
                    self.log(f"⚠️ Accept error: {e}")
                break
    
    def remove_client(self, key):
        with self.clients_lock:
            if key in self.clients:
                self.clients[key].stop()
                del self.clients[key]
                self.log(f"❌ Client disconnected: {key}")
    
    def list_clients(self):
        with self.clients_lock:
            return list(self.clients.keys())
    
    def broadcast_command(self, cmd):
        with self.clients_lock:
            for handler in self.clients.values():
                handler.send_command(cmd)
    
    # NEW: Request backup from specific clients
    def request_backup_from_clients(self, client_keys, source_path):
        """Request file backup from selected clients"""
        success_count = 0
        with self.clients_lock:
            for key in client_keys:
                if key in self.clients:
                    if self.clients[key].request_backup(source_path):
                        success_count += 1
        return success_count
    
    # NEW: Restore files to specific clients
    def restore_to_clients(self, client_keys, restore_path):
        """Restore backed up files to selected clients"""
        success_count = 0
        with self.clients_lock:
            for key in client_keys:
                if key in self.clients:
                    handler = self.clients[key]
                    hostname = handler.client_info.get("hostname", key.replace(":", "_"))
                    if handler.send_restore(hostname, restore_path):
                        success_count += 1
        return success_count
    
    def start_presentation(self, target_keys):
        if self.presenting:
            return
        
        self.presenting = True
        self.presentation_targets = target_keys
        
        with self.clients_lock:
            for key in target_keys:
                if key in self.clients:
                    self.clients[key].send_command("PRESENT_START")
        
        self.presentation_thread = threading.Thread(target=self._presentation_loop, daemon=True)
        self.presentation_thread.start()
        self.log(f"📽️ Presentation started for {len(target_keys)} client(s)")
    
    def stop_presentation(self):
        if not self.presenting:
            return
        
        self.presenting = False
        
        with self.clients_lock:
            for key in self.presentation_targets:
                if key in self.clients:
                    self.clients[key].send_command("PRESENT_STOP")
        
        if self.presentation_thread:
            self.presentation_thread.join(timeout=2.0)
        
        self.presentation_targets = []
        self.log("🛑 Presentation stopped")
    
    def _presentation_loop(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            delay = 1.0 / self.presentation_fps
            
            while self.presenting and self.running.is_set():
                start = time.time()
                
                try:
                    img = np.array(sct.grab(monitor))
                    frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    
                    if self.presentation_scale != 1.0:
                        h, w = frame.shape[:2]
                        new_h = int(h * self.presentation_scale)
                        new_w = int(w * self.presentation_scale)
                        frame = cv2.resize(frame, (new_w, new_h))
                    
                    ret, buffer = cv2.imencode('.jpg', frame, 
                                              [cv2.IMWRITE_JPEG_QUALITY, self.presentation_quality])
                    if not ret:
                        continue
                    
                    data = buffer.tobytes()
                    header = b"PRESENT_FRAME\n" + struct.pack(">Q", len(data))
                    payload = header + data
                    
                    with self.clients_lock:
                        for key in self.presentation_targets:
                            if key in self.clients:
                                try:
                                    self.clients[key].sock.sendall(payload)
                                except:
                                    pass
                    
                    elapsed = time.time() - start
                    sleep_time = max(0, delay - elapsed)
                    time.sleep(sleep_time)
                
                except Exception as e:
                    self.log(f"⚠️ Presentation error: {e}")
                    break


class AdminWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.server = AdminServer()
        self.selected_preview_client = None
        self.init_ui()
        self.server.signals.new_frame.connect(self.on_new_frame)
    
    def init_ui(self):
        self.setWindowTitle("Lab Manager - Admin Server")
        self.setGeometry(100, 100, 1400, 900)
        
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        
        # Left panel
        left_panel = QVBoxLayout()
        
        # Server control
        server_group = QGroupBox("🖥️ Server Control")
        server_layout = QVBoxLayout()
        
        self.lbl_status = QLabel("⚪ Server Stopped")
        self.lbl_status.setStyleSheet("font-size: 14px; font-weight: bold; color: red;")
        server_layout.addWidget(self.lbl_status)
        
        self.btn_start = QPushButton("▶️ Start Server")
        self.btn_start.clicked.connect(self.start_server)
        server_layout.addWidget(self.btn_start)
        
        self.btn_stop = QPushButton("⏹️ Stop Server")
        self.btn_stop.clicked.connect(self.stop_server)
        self.btn_stop.setEnabled(False)
        server_layout.addWidget(self.btn_stop)
        
        server_group.setLayout(server_layout)
        left_panel.addWidget(server_group)
        
        # Client list
        client_group = QGroupBox("👥 Connected Clients")
        client_layout = QVBoxLayout()
        
        self.list_clients = QListWidget()
        self.list_clients.setSelectionMode(QListWidget.MultiSelection)
        self.list_clients.itemClicked.connect(self.on_client_selected)
        client_layout.addWidget(self.list_clients)
        
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_clients)
        client_layout.addWidget(refresh_btn)
        
        client_group.setLayout(client_layout)
        left_panel.addWidget(client_group)
        
        # Actions
        actions_group = QGroupBox("⚡ Actions")
        actions_layout = QVBoxLayout()
        
        self.btn_lock = QPushButton("🔒 Lock")
        self.btn_lock.clicked.connect(self.lock_selected)
        actions_layout.addWidget(self.btn_lock)
        
        self.btn_unlock = QPushButton("🔓 Unlock")
        self.btn_unlock.clicked.connect(self.unlock_selected)
        actions_layout.addWidget(self.btn_unlock)
        
        self.btn_screenshot = QPushButton("📸 Get Screenshot")
        self.btn_screenshot.clicked.connect(self.request_screenshot)
        actions_layout.addWidget(self.btn_screenshot)
        
        self.btn_send_file = QPushButton("📤 Send File")
        self.btn_send_file.clicked.connect(self.send_file_to_selected)
        actions_layout.addWidget(self.btn_send_file)
        
        self.btn_message = QPushButton("💬 Send Message")
        self.btn_message.clicked.connect(self.send_message_to_selected)
        actions_layout.addWidget(self.btn_message)
        
        # NEW: Backup/Restore buttons
        backup_restore_layout = QHBoxLayout()
        self.btn_backup = QPushButton("💾 Backup Files")
        self.btn_backup.clicked.connect(self.backup_client_files)
        self.btn_backup.setStyleSheet("background-color: #0078d4; color: white; padding: 8px;")
        backup_restore_layout.addWidget(self.btn_backup)
        
        self.btn_restore = QPushButton("📥 Restore Files")
        self.btn_restore.clicked.connect(self.restore_client_files)
        self.btn_restore.setStyleSheet("background-color: #107c10; color: white; padding: 8px;")
        backup_restore_layout.addWidget(self.btn_restore)
        
        actions_layout.addLayout(backup_restore_layout)
        
        actions_group.setLayout(actions_layout)
        left_panel.addWidget(actions_group)
        
        # Presentation
        present_group = QGroupBox("📽️ Presentation Mode")
        present_layout = QVBoxLayout()
        
        quality_layout = QHBoxLayout()
        quality_layout.addWidget(QLabel("Quality:"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Low (50)", "Medium (70)", "High (85)", "Max (95)"])
        self.quality_combo.setCurrentIndex(2)
        quality_layout.addWidget(self.quality_combo)
        present_layout.addLayout(quality_layout)
        
        scale_layout = QHBoxLayout()
        scale_layout.addWidget(QLabel("Scale:"))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["50%", "75%", "100%", "125%"])
        self.scale_combo.setCurrentIndex(2)
        scale_layout.addWidget(self.scale_combo)
        present_layout.addLayout(scale_layout)
        
        self.btn_present = QPushButton("📽️ Present My Screen")
        self.btn_present.clicked.connect(self.toggle_presentation)
        self.btn_present.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                color: white;
                padding: 12px 24px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #0e6b0e;
            }
        """)
        present_layout.addWidget(self.btn_present)
        
        present_group.setLayout(present_layout)
        left_panel.addWidget(present_group)
        
        left_panel.addStretch()
        main_layout.addLayout(left_panel, 1)
        
        # Right panel - Preview and log
        right_panel = QVBoxLayout()
        
        # Preview
        preview_group = QGroupBox("🖼️ Screen Preview")
        preview_layout = QVBoxLayout()
        
        self.lbl_preview = QLabel("No preview available")
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setMinimumSize(800, 450)
        self.lbl_preview.setStyleSheet("QLabel { background-color: #2b2b2b; color: white; }")
        preview_layout.addWidget(self.lbl_preview)
        
        preview_buttons = QHBoxLayout()
        btn_refresh_preview = QPushButton("🔄 Refresh")
        btn_refresh_preview.clicked.connect(self.refresh_preview)
        preview_buttons.addWidget(btn_refresh_preview)
        
        btn_save_preview = QPushButton("💾 Save Image")
        btn_save_preview.clicked.connect(self.save_preview_image)
        preview_buttons.addWidget(btn_save_preview)
        
        preview_layout.addLayout(preview_buttons)
        preview_group.setLayout(preview_layout)
        right_panel.addWidget(preview_group, 2)
        
        # Log
        log_group = QGroupBox("📋 Activity Log")
        log_layout = QVBoxLayout()
        
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("QTextEdit { background-color: #1e1e1e; color: #d4d4d4; font-family: Consolas; }")
        log_layout.addWidget(self.txt_log)
        
        log_buttons = QHBoxLayout()
        btn_clear_log = QPushButton("🗑️ Clear Log")
        btn_clear_log.clicked.connect(lambda: self.txt_log.clear())
        log_buttons.addWidget(btn_clear_log)
        
        btn_save_log = QPushButton("💾 Save Log")
        btn_save_log.clicked.connect(self.save_log)
        log_buttons.addWidget(btn_save_log)
        
        log_layout.addLayout(log_buttons)
        log_group.setLayout(log_layout)
        right_panel.addWidget(log_group, 1)
        
        main_layout.addLayout(right_panel, 2)
        
        # Auto-refresh timer
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_clients)
        self.refresh_timer.start(2000)
    
    # NEW: Backup client files
    def backup_client_files(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients to backup")
            return
        
        source_path, ok = QInputDialog.getText(
            self, "Backup Files",
            "Enter the directory path on client PC to backup:",
            text="C:\\Users\\Public\\Documents"
        )
        
        if not ok or not source_path:
            return
        
        reply = QMessageBox.question(
            self, "Confirm Backup",
            f"Backup files from:\n{source_path}\n\nFrom {len(keys)} client(s)?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            count = self.server.request_backup_from_clients(keys, source_path)
            self.log(f"💾 Backup requested from {count} client(s)")
            self.log(f"📁 Files will be saved to: {BACKUP_DIR}")
            QMessageBox.information(
                self, "Backup Started",
                f"Backup request sent to {count} client(s)\n\n"
                f"Files will be saved to:\n{BACKUP_DIR}"
            )
    
    # NEW: Restore files to clients
    def restore_client_files(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients to restore")
            return
        
        # Check if backups exist
        available_backups = [d for d in os.listdir(BACKUP_DIR) 
                           if os.path.isdir(os.path.join(BACKUP_DIR, d))]
        
        if not available_backups:
            QMessageBox.warning(
                self, "No Backups",
                f"No backup folders found in:\n{BACKUP_DIR}"
            )
            return
        
        restore_path, ok = QInputDialog.getText(
            self, "Restore Files",
            "Enter the directory path on client PC to restore to:",
            text="C:\\Users\\Public\\Documents"
        )
        
        if not ok or not restore_path:
            return
        
        reply = QMessageBox.question(
            self, "Confirm Restore",
            f"Restore files to:\n{restore_path}\n\n"
            f"On {len(keys)} client(s)?\n\n"
            f"Available backups: {', '.join(available_backups)}",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            count = self.server.restore_to_clients(keys, restore_path)
            self.log(f"📥 Restore sent to {count} client(s)")
            QMessageBox.information(
                self, "Restore Started",
                f"Restore request sent to {count} client(s)"
            )
    
    def _get_selected_keys(self):
        items = self.list_clients.selectedItems()
        return [item.text().split(" ")[0] for item in items]
    
    def log(self, msg):
        self.txt_log.append(f"[{now_ts()}] {msg}")
        self.server.log(msg)
    
    def start_server(self):
        self.server.start()
        self.lbl_status.setText("🟢 Server Running")
        self.lbl_status.setStyleSheet("font-size: 14px; font-weight: bold; color: green;")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
    
    def stop_server(self):
        self.server.stop()
        self.lbl_status.setText("⚪ Server Stopped")
        self.lbl_status.setStyleSheet("font-size: 14px; font-weight: bold; color: red;")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.list_clients.clear()
    
    def refresh_clients(self):
        keys = self.server.list_clients()
        current_items = [self.list_clients.item(i).text().split(" ")[0] 
                        for i in range(self.list_clients.count())]
        
        if set(keys) != set(current_items):
            selected_keys = self._get_selected_keys()
            self.list_clients.clear()
            
            with self.server.clients_lock:
                for key in keys:
                    handler = self.server.clients.get(key)
                    if handler:
                        uptime = int(time.time() - handler.connected_time)
                        item_text = f"{key} | Uptime: {uptime}s | Frames: {handler.frames_received}"
                        self.list_clients.addItem(item_text)
                        
                        if key in selected_keys:
                            items = self.list_clients.findItems(key, Qt.MatchStartsWith)
                            if items:
                                items[0].setSelected(True)
    
    def on_client_selected(self, item):
        key = item.text().split(" ")[0]
        self.selected_preview_client = key
        self.refresh_preview()
    
    def lock_selected(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients")
            return
        
        message, ok = QInputDialog.getText(
            self, "Lock Screen",
            "Enter lock message (optional):",
            text="🔒 Locked by Administrator"
        )
        
        if ok:
            with self.server.clients_lock:
                for k in keys:
                    if k in self.server.clients:
                        self.server.clients[k].send_command(f"LOCK:{message}")
    
    def unlock_selected(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients")
            return
        
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    self.server.clients[k].send_command("UNLOCK")
    
    def request_screenshot(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients")
            return
        
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    self.server.clients[k].send_command("REQUEST_SCREENSHOT")
    
    def send_file_to_selected(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send")
        if not path:
            return
        
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients")
            return
        
        destinations = ["Downloads", "Desktop", "Documents", "Custom Path..."]
        destination, ok = QInputDialog.getItem(
            self, "Select Destination",
            "Where should the file be saved?",
            destinations, 0, False
        )
        
        if not ok:
            return
        
        if destination == "Custom Path...":
            destination, ok = QInputDialog.getText(
                self, "Custom Destination",
                "Enter the full path:",
                text="C:\\"
            )
            if not ok or not destination:
                return
        
        filesize = os.path.getsize(path)
        
        with self.server.clients_lock:
            sent = 0
            for k in keys:
                if k in self.server.clients:
                    threading.Thread(
                        target=self.server.clients[k].send_file_resumable,
                        args=(path, destination),
                        daemon=True
                    ).start()
                    sent += 1
        
        QMessageBox.information(
            self, "File Transfer",
            f"Starting transfer:\n"
            f"File: {os.path.basename(path)} ({format_bytes(filesize)})\n"
            f"Recipients: {sent} client(s)\n"
            f"Destination: {destination}"
        )
    
    def send_message_to_selected(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients")
            return
        
        text, ok = QInputDialog.getText(
            self, "Send Message",
            "Enter message to send:"
        )
        
        if ok and text:
            with self.server.clients_lock:
                for k in keys:
                    if k in self.server.clients:
                        self.server.clients[k].send_command(f"MESSAGE:{text}")
    
    def toggle_presentation(self):
        if self.server.presenting:
            self.server.stop_presentation()
            self.btn_present.setText("📽️ Present My Screen")
            self.btn_present.setStyleSheet("""
                QPushButton {
                    background-color: #107c10;
                    padding: 12px 24px;
                }
            """)
        else:
            keys = self._get_selected_keys()
            if not keys:
                QMessageBox.warning(self, "No Selection", "Select clients to present to")
                return
            
            quality_text = self.quality_combo.currentText()
            quality = int(quality_text.split("(")[1].split(")")[0])
            
            scale_text = self.scale_combo.currentText()
            scale = float(scale_text.replace("%", "")) / 100
            
            self.server.presentation_quality = quality
            self.server.presentation_scale = scale
            self.server.presentation_fps = 30
            
            reply = QMessageBox.question(
                self, "Start Presentation",
                f"Present to {len(keys)} client(s)?\n\n"
                f"Quality: {quality}, Scale: {scale*100:.0f}%, FPS: 30",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.server.start_presentation(keys)
                self.btn_present.setText("⏹️ Stop Presenting")
                self.btn_present.setStyleSheet("""
                    QPushButton {
                        background-color: #c42b1c;
                        padding: 12px 24px;
                    }
                """)
    
    def refresh_preview(self):
        if not self.selected_preview_client:
            return
        
        with self.server.clients_lock:
            handler = self.server.clients.get(self.selected_preview_client)
        
        if handler and handler.last_image:
            self._display_image_bytes(handler.last_image)
    
    def _display_image_bytes(self, img_bytes):
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
        except:
            pass
    
    def on_new_frame(self, key, data):
        if key == self.selected_preview_client:
            self._display_image_bytes(data)
    
    def save_preview_image(self):
        pix = self.lbl_preview.pixmap()
        if not pix or pix.isNull():
            QMessageBox.information(self, "No Image", "No preview image to save")
            return
        
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Preview Image", "",
            "JPEG Files (*.jpg);;PNG Files (*.png)"
        )
        
        if filename:
            if pix.save(filename):
                QMessageBox.information(self, "Saved", f"Image saved to:\n{filename}")
    
    def save_log(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Log",
            f"lab_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)"
        )
        
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(self.txt_log.toPlainText())
                QMessageBox.information(self, "Saved", f"Log saved to:\n{filename}")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save: {e}")
    
    def closeEvent(self, event):
        if self.server.running.is_set():
            reply = QMessageBox.question(
                self, "Exit",
                "Server is running. Stop and exit?",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.server.stop()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Lab Manager - Admin")
    
    window = AdminWindow()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
