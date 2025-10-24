"""
Lab Manager - Admin Server - COMPLETE VERSION
All features working: Lock/Unlock, Screen Monitoring, File Transfer, Presentation Mode
"""

import shutil
import sys
import os
import socket
import threading
import struct
import time
import json
import hashlib
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
os.makedirs(INBOX_DIR, exist_ok=True)
os.makedirs(RESUME_METADATA_DIR, exist_ok=True)
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "ClientBackups")  # NEW: Main backup directory

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
    
    
class FileSyncManager:
    """Automatically syncs files from all connected clients"""
    
    def __init__(self, server, base_sync_dir=None):
        self.server = server
        self.base_sync_dir = base_sync_dir or os.path.join(os.path.expanduser("~"), "lab_sync")
        os.makedirs(self.base_sync_dir, exist_ok=True)
        
        # Track file hashes to detect changes
        self.file_hashes = {}  # {client_key: {file_path: hash}}
        self.client_configs = {}  # {client_key: source_path}
        self.sync_thread = None
        self.sync_running = False
        self.server.log(f"File sync manager initialized at {self.base_sync_dir}")
    
    def set_global_source_path(self, source_path):
        """Set the source path for ALL clients"""
        self.global_source_path = source_path
        self.server.log(f"Global source path set to: {source_path}")
    
    def start_sync_cycle(self, sync_interval=30):
        """Start automatic sync for ALL connected clients"""
        if self.sync_running:
            self.server.log("Sync cycle already running")
            return
        
        if not hasattr(self, 'global_source_path'):
            self.server.log("ERROR: Source path not configured")
            return
        
        self.sync_running = True
        self.server.log(f"Starting AUTO SYNC for ALL clients (interval: {sync_interval}s)")
        
        def sync_loop():
            while self.sync_running:
                try:
                    clients = self.server.list_clients()
                    if clients:
                        self.server.log(f"Syncing {len(clients)} connected clients...")
                        for client_key in clients:
                            self._sync_client_files(client_key, self.global_source_path)
                    time.sleep(sync_interval)
                except Exception as e:
                    self.server.log(f"Sync cycle error: {e}")
                    time.sleep(5)
        
        self.sync_thread = threading.Thread(target=sync_loop, daemon=True)
        self.sync_thread.start()
    
    def stop_sync_cycle(self):
        """Stop the automatic sync cycle"""
        self.sync_running = False
        self.server.log("AUTO SYNC stopped")
    
    def _sync_client_files(self, client_key, source_path):
        """Trigger file collection from a client"""
        # Send command to client to collect files
        cmd = f"COLLECT_FILES:{source_path}"
        
        with self.server.clients_lock:
            if client_key in self.server.clients:
                handler = self.server.clients[client_key]
                handler.send_command(cmd)
    
    def receive_file_list(self, client_key, files_data):
        """Receive and process file list from client"""
        try:
            files = json.loads(files_data)
            client_sync_dir = os.path.join(self.base_sync_dir, client_key.replace(":", "_"))
            os.makedirs(client_sync_dir, exist_ok=True)
            
            if client_key not in self.file_hashes:
                self.file_hashes[client_key] = {}
            
            new_files = []
            modified_files = []
            
            for file_info in files:
                file_path = file_info["path"]
                file_hash = file_info["hash"]
                
                # Check if file is new or modified
                old_hash = self.file_hashes[client_key].get(file_path)
                
                if old_hash is None:
                    new_files.append(file_path)
                    self.server.log(f"NEW: {client_key} -> {file_path}")
                elif old_hash != file_hash:
                    modified_files.append(file_path)
                    self.server.log(f"MODIFIED: {client_key} -> {file_path}")
                
                # Update hash record
                self.file_hashes[client_key][file_path] = file_hash
            
            # Request all new and modified files
            files_to_request = new_files + modified_files
            for file_path in files_to_request:
                self._request_file_from_client(client_key, file_path)
            
            if files_to_request:
                self.server.log(f"Sync {client_key}: {len(new_files)} new, {len(modified_files)} modified")
            
        except Exception as e:
            self.server.log(f"Error processing file list from {client_key}: {e}")
    
    def _request_file_from_client(self, client_key, file_path):
        """Request a specific file from client"""
        cmd = f"SEND_FILE_TO_ADMIN:{file_path}"
        
        with self.server.clients_lock:
            if client_key in self.server.clients:
                handler = self.server.clients[client_key]
                handler.send_command(cmd)
    
    def receive_file_from_client(self, client_key, file_info, file_data):
        """Receive and save file sent from client"""
        try:
            file_path = file_info.get("path", "unknown")
            file_name = file_info.get("name", "file")
            
            # Create directory structure
            client_dir = os.path.join(self.base_sync_dir, client_key.replace(":", "_"))
            os.makedirs(client_dir, exist_ok=True)
            
            # Save file with relative path structure
            relative_path = file_path.replace("\\", "/")
            save_path = os.path.join(client_dir, relative_path.lstrip("/"))
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            # Write file
            with open(save_path, "wb") as f:
                f.write(file_data)
            
            self.server.log(f"SAVED: {client_key} -> {save_path}")
            
        except Exception as e:
            self.server.log(f"Error saving file from {client_key}: {e}")
    
    def get_sync_status(self):
        """Get sync status"""
        status = {
            "running": self.sync_running,
            "clients_syncing": len(self.file_hashes),
            "total_files": sum(len(files) for files in self.file_hashes.values()),
            "sync_dir": self.base_sync_dir
        }
        return status


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
                # Ensure socket is in blocking mode for send
                self.sock.setblocking(True)
                self.sock.settimeout(5.0)
                self.sock.sendall(data)
                self.sock.setblocking(False)
                self.sock.settimeout(None)
            self.server.log(f"✉️ Sent to {self.key}: {cmd_str}")
            return True
        except socket.timeout:
            self.server.log(f"⏱️ Send timeout to {self.key}: {cmd_str}")
            return False
        except Exception as e:
            self.server.log(f"❌ Send error to {self.key}: {e}")
            return False
    
    # def send_file_resumable(self, filepath, destination=None):
    #     if not os.path.exists(filepath):
    #         self.server.log(f"❌ File not found: {filepath}")
    #         return False
        
    #     if not self.sock or not self.running.is_set():
    #         self.server.log(f"⚠️ Client {self.key} not connected")
    #         return False
        
    #     self.transferring.set()
        
    #     try:
    #         transfer = ResumableFileTransfer(filepath, destination or "Downloads")
    #         basename = transfer.basename
    #         filesize = transfer.filesize
    #         chunk_size = transfer.chunk_size
            
    #         self.server.log(f"📤 Starting: {basename} ({format_bytes(filesize)})")
            
    #         pending_chunks = transfer.get_pending_chunks()
    #         if len(pending_chunks) < transfer.total_chunks:
    #             self.server.log(f"🔄 Resume: {len(transfer.completed_chunks)}/{transfer.total_chunks} done")
            
    #         try:
    #             self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    #             self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
    #             self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
    #         except:
    #             pass
            
    #         init_header = {
    #             "command": "RESUMABLE_TRANSFER_START",
    #             "transfer_id": transfer.transfer_id,
    #             "filename": basename,
    #             "destination": transfer.destination,
    #             "filesize": filesize,
    #             "total_chunks": transfer.total_chunks,
    #             "chunk_size": chunk_size,
    #             "batch_ack_size": BATCH_ACK_SIZE
    #         }
            
    #         init_json = json.dumps(init_header).encode("utf-8")
    #         header = b"RESUMABLE_FILE\n" + struct.pack(">I", len(init_json)) + init_json
            
    #         with self.lock:
    #             try:
    #                 self.sock.sendall(header)
    #             except OSError:
    #                 self.server.log(f"❌ Cannot start transfer")
    #                 return False
                
    #             self.sock.settimeout(15.0)
    #             buffer = b""
    #             ready_received = False
    #             start_wait = time.time()
                
    #             while not ready_received and (time.time() - start_wait) < 15:
    #                 try:
    #                     chunk = self.sock.recv(1024)
    #                 except socket.timeout:
    #                     continue
    #                 except OSError:
    #                     self.server.log(f"⚠️ Socket closed waiting for READY")
    #                     return False
                    
    #                 if not chunk:
    #                     raise Exception("Connection closed waiting for READY")
                    
    #                 buffer += chunk
    #                 while b'\n' in buffer:
    #                     line, buffer = buffer.split(b'\n', 1)
    #                     msg = line.decode('utf-8', errors='ignore').strip()
                        
    #                     if msg == "READY":
    #                         ready_received = True
    #                         break
    #                     elif msg == "HEARTBEAT":
    #                         continue
    #                     elif msg == "ERROR":
    #                         raise Exception("Client error during handshake")
                
    #             if not ready_received:
    #                 raise Exception("Timeout waiting for READY")
                
    #             self.server.log("✅ Client READY - Starting transfer")
    #             self.sock.settimeout(None)
                
    #             start_time = time.time()
    #             sent_bytes = 0
    #             last_log = start_time
    #             chunks_in_batch = []
    #             chunk_delay = CHUNK_SEND_DELAY
                
    #             with open(filepath, "rb") as f:
    #                 for idx, chunk_index in enumerate(pending_chunks):
    #                     if not self.running.is_set():
    #                         self.server.log(f"⚠️ Transfer stopped")
    #                         break
                        
    #                     try:
    #                         f.seek(chunk_index * chunk_size)
    #                         chunk_data = f.read(chunk_size)
    #                         if not chunk_data:
    #                             break
                            
    #                         checksum = transfer._calculate_chunk_checksum(chunk_data)
    #                         chunk_header = struct.pack(">II", chunk_index, len(chunk_data))
    #                         chunk_header += checksum.encode('utf-8').ljust(64, b'\x00')
                            
    #                         try:
    #                             self.sock.sendall(chunk_header + chunk_data)
    #                         except OSError:
    #                             self.server.log(f"⚠️ Socket closed during chunk {chunk_index}")
    #                             transfer.save_progress_batch()
    #                             return False
                            
    #                         time.sleep(chunk_delay)
    #                         sent_bytes += len(chunk_data)
    #                         chunks_in_batch.append((chunk_index, checksum))
                            
    #                         if len(chunks_in_batch) >= BATCH_ACK_SIZE or idx == len(pending_chunks) - 1:
    #                             self.sock.settimeout(30.0)
    #                             ack_buffer = b""
    #                             ack_received = False
    #                             ack_start = time.time()
                                
    #                             while not ack_received and (time.time() - ack_start) < 30:
    #                                 try:
    #                                     chunk_ack = self.sock.recv(4096)
    #                                 except socket.timeout:
    #                                     continue
    #                                 except OSError:
    #                                     self.server.log(f"⚠️ Socket closed waiting for ACK")
    #                                     transfer.save_progress_batch()
    #                                     return False
                                    
    #                                 if not chunk_ack:
    #                                     raise Exception("Connection closed during ACK")
                                    
    #                                 ack_buffer += chunk_ack
    #                                 while b'\n' in ack_buffer:
    #                                     line, ack_buffer = ack_buffer.split(b'\n', 1)
    #                                     msg = line.decode('utf-8', errors='ignore').strip().upper()
                                        
    #                                     if msg == "CHUNK_OK":
    #                                         ack_received = True
    #                                         break
    #                                     elif msg == "HEARTBEAT":
    #                                         continue
    #                                     elif msg in ["CHUNK_ERROR", "ERROR"]:
    #                                         raise Exception(f"Client error at chunk {chunk_index}")
                                
    #                             if not ack_received:
    #                                 raise Exception(f"Timeout waiting for ACK at chunk {chunk_index}")
                                
    #                             self.sock.settimeout(None)
                                
    #                             for ci, cs in chunks_in_batch:
    #                                 transfer.mark_chunk_complete(ci, cs)
    #                             chunks_in_batch = []
    #                             if idx % 50 == 0:
    #                                 transfer.save_progress_batch()
                            
    #                         current_time = time.time()
    #                         if current_time - last_log >= 2.0:
    #                             progress = transfer.get_progress()
    #                             elapsed = current_time - start_time
    #                             speed = sent_bytes / elapsed if elapsed > 0 else 0
    #                             eta = ((filesize - sent_bytes) / speed) if speed > 0 else 0
    #                             self.server.log(
    #                                 f"📊 {progress:.1f}% | {format_bytes(speed)}/s | "
    #                                 f"ETA: {int(eta)}s"
    #                             )
    #                             last_log = current_time
    #                             sent_bytes = 0
                        
    #                     except Exception as e:
    #                         self.server.log(f"❌ Chunk {chunk_index} error: {e}")
    #                         raise
                
    #             try:
    #                 self.sock.sendall(b"TRANSFER_COMPLETE\n")
    #                 self.server.log("✅ All chunks sent, waiting for VERIFIED")
    #             except OSError:
    #                 self.server.log(f"⚠️ Socket closed before TRANSFER_COMPLETE")
    #                 transfer.save_progress_batch()
    #                 return False
                
    #             self.sock.settimeout(60.0)
    #             try:
    #                 verify_buffer = b""
    #                 verify_start = time.time()
                    
    #                 while (time.time() - verify_start) < 60:
    #                     try:
    #                         final_ack = self.sock.recv(4096)
    #                         if not final_ack:
    #                             raise ConnectionError("Connection closed waiting for VERIFIED")
                            
    #                         verify_buffer += final_ack
                            
    #                         if b"VERIFIED" in verify_buffer:
    #                             self.server.log("✅ Client verified transfer")
    #                             break
    #                         elif b"HEARTBEAT" in verify_buffer:
    #                             verify_buffer = b""
    #                             continue
                        
    #                     except socket.timeout:
    #                         continue
    #                 else:
    #                     self.server.log("⚠️ Timeout waiting for VERIFIED")
                
    #             except OSError:
    #                 self.server.log("⚠️ Socket closed before VERIFIED")
    #             finally:
    #                 self.sock.settimeout(None)
            
    #         elapsed = time.time() - start_time
    #         speed = filesize / elapsed if elapsed > 0 else 0
    #         self.server.log(f"✅ Complete: {basename} | {format_bytes(speed)}/s | {int(elapsed)}s")
            
    #         transfer.cleanup()
    #         return True
        
    #     except Exception as e:
    #         self.server.log(f"❌ Transfer error: {e}")
    #         self.server.log("💾 Progress saved - can resume")
    #         return False
        
    #     finally:
    #         self.transferring.clear()
            
            
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
        """FIXED: Unified resumable file transfer with proper protocol"""
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
            
            # Configure socket
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
            except:
                pass
            
            # Prepare transfer header
            init_header = {
                "command": "RESUMABLE_TRANSFER_START",
                "transfer_id": transfer.transfer_id,
                "filename": basename,
                "destination": transfer.destination,
                "filesize": filesize,
                "total_chunks": transfer.total_chunks,
                "chunk_size": chunk_size,
                "batch_ack_size": BATCH_ACK_SIZE
            }
            
            init_json = json.dumps(init_header).encode("utf-8")
            header = b"RESUMABLE_FILE\n" + struct.pack(">I", len(init_json)) + init_json
            
            with self.lock:
                try:
                    self.sock.sendall(header)
                except OSError:
                    self.server.log(f"❌ Cannot start transfer")
                    return False
                
                # Wait for READY confirmation
                self.sock.settimeout(15.0)
                buffer = b""
                ready_received = False
                start_wait = time.time()
                
                while not ready_received and (time.time() - start_wait) < 15:
                    try:
                        chunk = self.sock.recv(1024)
                    except socket.timeout:
                        continue
                    except OSError:
                        self.server.log(f"⚠️ Socket closed waiting for READY")
                        return False
                    
                    if not chunk:
                        raise Exception("Connection closed waiting for READY")
                    
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        msg = line.decode('utf-8', errors='ignore').strip()
                        
                        if msg == "READY":
                            ready_received = True
                            break
                        elif msg == "HEARTBEAT":
                            continue
                        elif msg == "ERROR":
                            raise Exception("Client error during handshake")
                
                if not ready_received:
                    raise Exception("Timeout waiting for READY")
                
                self.server.log("✅ Client READY - Starting transfer")
                self.sock.settimeout(None)
                
                # Transfer chunks
                start_time = time.time()
                sent_bytes = 0
                last_log = start_time
                chunks_in_batch = []
                chunk_delay = CHUNK_SEND_DELAY
                
                with open(filepath, "rb") as f:
                    for idx, chunk_index in enumerate(pending_chunks):
                        if not self.running.is_set():
                            self.server.log(f"⚠️ Transfer stopped")
                            break
                        
                        try:
                            # Read chunk
                            f.seek(chunk_index * chunk_size)
                            chunk_data = f.read(chunk_size)
                            if not chunk_data:
                                break
                            
                            # Calculate checksum
                            checksum = transfer._calculate_chunk_checksum(chunk_data)
                            
                            # Prepare chunk header (index + size + checksum)
                            chunk_header = struct.pack(">II", chunk_index, len(chunk_data))
                            chunk_header += checksum.encode('utf-8').ljust(64, b'\x00')
                            
                            # Send chunk
                            try:
                                self.sock.sendall(chunk_header + chunk_data)
                            except OSError:
                                self.server.log(f"⚠️ Socket closed during chunk {chunk_index}")
                                transfer.save_progress_batch()
                                return False
                            
                            time.sleep(chunk_delay)
                            sent_bytes += len(chunk_data)
                            chunks_in_batch.append((chunk_index, checksum))
                            
                            # Wait for batch acknowledgment
                            if len(chunks_in_batch) >= BATCH_ACK_SIZE or idx == len(pending_chunks) - 1:
                                self.sock.settimeout(30.0)
                                ack_buffer = b""
                                ack_received = False
                                ack_start = time.time()
                                
                                while not ack_received and (time.time() - ack_start) < 30:
                                    try:
                                        chunk_ack = self.sock.recv(4096)
                                    except socket.timeout:
                                        continue
                                    except OSError:
                                        self.server.log(f"⚠️ Socket closed waiting for ACK")
                                        transfer.save_progress_batch()
                                        return False
                                    
                                    if not chunk_ack:
                                        raise Exception("Connection closed during ACK")
                                    
                                    ack_buffer += chunk_ack
                                    while b'\n' in ack_buffer:
                                        line, ack_buffer = ack_buffer.split(b'\n', 1)
                                        msg = line.decode('utf-8', errors='ignore').strip().upper()
                                        
                                        if msg == "CHUNK_OK":
                                            ack_received = True
                                            break
                                        elif msg == "HEARTBEAT":
                                            continue
                                        elif msg in ["CHUNK_ERROR", "ERROR"]:
                                            raise Exception(f"Client error at chunk {chunk_index}")
                                
                                if not ack_received:
                                    raise Exception(f"Timeout waiting for ACK at chunk {chunk_index}")
                                
                                self.sock.settimeout(None)
                                
                                # Mark chunks as complete
                                for ci, cs in chunks_in_batch:
                                    transfer.mark_chunk_complete(ci, cs)
                                chunks_in_batch = []
                                
                                # Save progress periodically
                                if idx % 50 == 0:
                                    transfer.save_progress_batch()
                            
                            # Log progress
                            current_time = time.time()
                            if current_time - last_log >= 2.0:
                                progress = transfer.get_progress()
                                elapsed = current_time - start_time
                                speed = sent_bytes / elapsed if elapsed > 0 else 0
                                eta = ((filesize - (len(transfer.completed_chunks) * chunk_size)) / speed) if speed > 0 else 0
                                self.server.log(
                                    f"📊 {progress:.1f}% | {format_bytes(speed)}/s | "
                                    f"ETA: {int(eta)}s"
                                )
                                last_log = current_time
                                sent_bytes = 0
                        
                        except Exception as e:
                            self.server.log(f"❌ Chunk {chunk_index} error: {e}")
                            raise
                
                # Send completion signal
                try:
                    self.sock.sendall(b"TRANSFER_COMPLETE\n")
                    self.server.log("✅ All chunks sent, waiting for VERIFIED")
                except OSError:
                    self.server.log(f"⚠️ Socket closed before TRANSFER_COMPLETE")
                    transfer.save_progress_batch()
                    return False
                
                # Wait for verification
                self.sock.settimeout(60.0)
                try:
                    verify_buffer = b""
                    verify_start = time.time()
                    
                    while (time.time() - verify_start) < 60:
                        try:
                            final_ack = self.sock.recv(4096)
                            if not final_ack:
                                raise ConnectionError("Connection closed waiting for VERIFIED")
                            
                            verify_buffer += final_ack
                            
                            if b"VERIFIED" in verify_buffer:
                                self.server.log("✅ Client verified transfer")
                                break
                            elif b"HEARTBEAT" in verify_buffer:
                                verify_buffer = b""
                                continue
                        
                        except socket.timeout:
                            continue
                    else:
                        self.server.log("⚠️ Timeout waiting for VERIFIED")
                
                except OSError:
                    self.server.log("⚠️ Socket closed before VERIFIED")
                finally:
                    self.sock.settimeout(None)
            
            # Success
            elapsed = time.time() - start_time
            speed = filesize / elapsed if elapsed > 0 else 0
            self.server.log(f"✅ Complete: {basename} | {format_bytes(speed)}/s | {int(elapsed)}s")
            
            transfer.cleanup()
            return True
        
        except Exception as e:
            self.server.log(f"❌ Transfer error: {e}")
            self.server.log("💾 Progress saved - can resume")
            return False
        
        finally:
            self.transferring.clear()
            
    def _reader_loop(self):
            sock = self.sock
            sock.setblocking(True)
            sock.settimeout(30.0)

            try:
                buffer = b""
                consecutive_errors = 0
                max_consecutive_errors = 3

                while self.running.is_set():
                    if self.transferring.is_set():
                        time.sleep(0.1)
                        continue

                    try:
                        chunk = sock.recv(RECV_BUFFER)
                        if not chunk:
                            self.server.log(f"⚠️ Client {self.key} closed connection")
                            break

                        consecutive_errors = 0
                        buffer += chunk
                        self.bytes_received += len(chunk)
                        self.last_heartbeat = time.time()  # Update heartbeat on ANY data received

                        # Handle backup data reception
                        if getattr(self, "backup_receiving", False):
                            self.backup_buffer += chunk
                            if len(self.backup_buffer) >= self.expected_backup_size:
                                self._process_backup_data()
                                self.backup_receiving = False
                                buffer = self.backup_buffer[self.expected_backup_size:]
                                self.backup_buffer = b""
                            continue

                        while b"\n" in buffer:
                            idx = buffer.find(b"\n")
                            if idx == -1:
                                break

                            line = buffer[:idx]
                            buffer = buffer[idx + 1:]
                            header = line.decode('utf-8', errors='ignore').strip()

                            if not header:
                                continue

                            # --- Frame Handling ---
                            if header.upper() == "FRAME":
                                # Wait for size header (8 bytes)
                                while len(buffer) < 8:
                                    chunk = sock.recv(RECV_BUFFER)
                                    if not chunk:
                                        raise ConnectionError("Connection closed reading frame size")
                                    buffer += chunk
                                    self.bytes_received += len(chunk)
                                    self.last_heartbeat = time.time()
                                
                                frame_size = struct.unpack(">Q", buffer[:8])[0]
                                buffer = buffer[8:]

                                if frame_size <= 0 or frame_size > MAX_IMAGE_SIZE:
                                    self.server.log(f"⚠️ Invalid frame size: {frame_size}")
                                    continue

                                # Wait for complete frame data
                                while len(buffer) < frame_size:
                                    chunk = sock.recv(min(RECV_BUFFER, frame_size - len(buffer)))
                                    if not chunk:
                                        raise ConnectionError("Connection closed reading frame data")
                                    buffer += chunk
                                    self.bytes_received += len(chunk)
                                    self.last_heartbeat = time.time()
                                
                                frame_data = buffer[:frame_size]
                                buffer = buffer[frame_size:]
                                
                                # Process the complete frame
                                self._process_frame(frame_data)

                            # --- Heartbeat ---
                            elif header.upper() == "HEARTBEAT":
                                self.last_heartbeat = time.time()

                            # --- Status Message ---
                            elif header.upper().startswith("STATUS"):
                                self.server.log(f"📊 {self.key}: {header}")

                            # --- General Message ---
                            elif header.upper().startswith("MSG"):
                                self.server.log(f"💬 {self.key}: {header}")

                            # --- Backup Reception ---
                            elif header.startswith("BACKUP_DATA:"):
                                try:
                                    size_str = header.split(":", 1)[1]
                                    self.expected_backup_size = int(size_str)
                                    self.backup_receiving = True
                                    self.backup_buffer = buffer
                                    buffer = b""
                                    self.server.log(f"💾 Receiving backup ({format_bytes(self.expected_backup_size)}) from {self.key}")
                                except Exception as e:
                                    self.server.log(f"⚠️ Backup data header error from {self.key}: {e}")

                            elif header.startswith("BACKUP_ERROR:"):
                                error_msg = header.split(":", 1)[1]
                                self.server.log(f"❌ Backup error from {self.key}: {error_msg}")

                            elif header.startswith("INFO:"):
                                try:
                                    info_json = header[5:]
                                    self.client_info = json.loads(info_json)
                                    
                                    # NEW: Check for duplicate hostnames
                                    hostname = self.client_info.get("hostname", "Unknown")
                                    duplicate_found = False
                                    duplicate_keys = []
                                    
                                    with self.server.clients_lock:
                                        for other_key, other_handler in self.server.clients.items():
                                            if other_key != self.key:
                                                other_hostname = other_handler.client_info.get("hostname", "")
                                                if other_hostname == hostname:
                                                    duplicate_found = True
                                                    duplicate_keys.append(other_key)
                                    
                                    if duplicate_found:
                                        self.server.log(
                                            f"⚠️ WARNING: Duplicate PC name detected!\n"
                                            f"   PC Name: '{hostname}'\n"
                                            f"   New client: {self.key}\n"
                                            f"   Existing: {', '.join(duplicate_keys)}\n"
                                            f"   Both clients remain connected but backups may conflict!"
                                        )
                                    else:
                                        self.server.log(f"📋 Client identified: {hostname} ({self.key})")
                                    
                                except Exception as e:
                                    self.server.log(f"⚠️ Failed to parse INFO from {self.key}: {e}")

                            else:
                                self.server.log(f"📝 {self.key}: {header}")

                    except socket.timeout:
                        if time.time() - self.last_heartbeat > 60:
                            self.server.log(f"⏱️ Client {self.key} timed out")
                            break
                        continue

                    except (ConnectionResetError, ConnectionAbortedError):
                        self.server.log(f"⚠️ Connection lost with {self.key}")
                        break

                    except ConnectionError as ce:
                        self.server.log(f"⚠️ Connection error: {ce}")
                        break

                    except OSError as e:
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            self.server.log(f"⚠️ Too many errors from {self.key}")
                            break
                        time.sleep(0.1)

            except Exception as e:
                self.server.log(f"❌ Handler error for {self.key}: {e}")

            finally:
                self.running.clear()
                try:
                    if self.sock:
                        self.sock.close()
                except:
                    pass

                self.client_info["status"] = "disconnected"
                self.server.log(f"❌ {self.key} disconnected")
                self.server.remove_client(self.key)


    def _process_backup_data(self):
        try:
            hostname = self.client_info.get("hostname", self.key.replace(":", "_"))
            
            # Use custom backup directory if set
            backup_dir = getattr(self.server, 'custom_backup_dir', BACKUP_DIR)
            client_folder = os.path.join(backup_dir, hostname)

            # Clear existing backup in this folder
            if os.path.exists(client_folder):
                shutil.rmtree(client_folder)
            os.makedirs(client_folder, exist_ok=True)

            # Save zip file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = os.path.join(client_folder, f"backup_{timestamp}.zip")

            # Track progress
            total_size = self.expected_backup_size
            bytes_written = 0
            
            self.server.log(f"💾 Saving backup from {hostname} ({format_bytes(total_size)})...")
            
            with open(zip_path, 'wb') as f:
                chunk_size = 1024 * 1024  # 1MB chunks
                buffer_data = self.backup_buffer[:self.expected_backup_size]
                
                for i in range(0, len(buffer_data), chunk_size):
                    chunk = buffer_data[i:i+chunk_size]
                    f.write(chunk)
                    bytes_written += len(chunk)
                    
                    # Log progress every 20%
                    progress = int((bytes_written / total_size) * 100)
                    if progress > 0 and progress % 20 == 0:
                        self.server.log(f"📊 Saving {hostname}: {progress}% ({format_bytes(bytes_written)}/{format_bytes(total_size)})")

            # Extract zip
            extract_folder = os.path.join(client_folder, "files")
            os.makedirs(extract_folder, exist_ok=True)

            self.server.log(f"📦 Extracting backup from {hostname}...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                file_list = zip_ref.namelist()
                total_files = len(file_list)
                
                for idx, file in enumerate(file_list):
                    zip_ref.extract(file, extract_folder)
                    
                    # Log progress every 100 files or at completion
                    if (idx + 1) % 100 == 0 or (idx + 1) == total_files:
                        progress = int(((idx + 1) / total_files) * 100)
                        self.server.log(f"📂 Extracting: {progress}% ({idx + 1}/{total_files} files)")

            self.server.log(f"✅ Backup received from {hostname}: {total_files} files ({format_bytes(len(self.backup_buffer))})")
            self.server.log(f"📁 Saved to: {client_folder}")

        except Exception as e:
            self.server.log(f"❌ Failed to process backup: {e}")
            import traceback
            self.server.log(traceback.format_exc())

    def _process_frame(self, data):
        self.last_image = data
        self.last_image_ts = time.time()
        self.frames_received += 1
        self.server.signals.new_frame.emit(self.key, data)

class AdminServer:
    def __init__(self, host=LISTEN_HOST, port=LISTEN_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.accept_thread = None
        self.running = threading.Event()
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.log_queue = Queue()
        
        self.frame_buffers = defaultdict(lambda: deque(maxlen=3))
        self.frame_locks = defaultdict(threading.Lock)
        
        self.signals = ServerSignals()
        self.total_connections = 0
        self.start_time = None
        
        self.presenting = False
        self.presentation_thread = None
    
    def start(self):
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
        while self.running.is_set():
            try:
                conn, addr = self.sock.accept()
                key = f"{addr[0]}:{addr[1]}"
                
                handler = ClientHandler(conn, addr, self)
                handler.start()
                
                with self.clients_lock:
                    self.clients[key] = handler
                
                self.total_connections += 1
                self.log(f"✅ Client connected: {key} (Total: {len(self.clients)})")
            
            except Exception as e:
                if self.running.is_set():
                    self.log(f"⚠️ Accept error: {e}")
                break
    
    def remove_client(self, key):
        with self.clients_lock:
            if key in self.clients:
                try:
                    self.clients[key].stop()
                except:
                    pass
                del self.clients[key]
                self.log(f"🗑️ Removed client: {key} (Remaining: {len(self.clients)})")
    
    def broadcast_command(self, cmd_str):
        with self.clients_lock:
            clients = list(self.clients.values())
        
        success = 0
        for handler in clients:
            if handler.send_command(cmd_str):
                success += 1
        
        self.log(f"📢 Broadcast '{cmd_str}' to {success}/{len(clients)} clients")
    
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
            self.log("Presentation already active")
            return
        
        self.presenting = True
        self.log(f"Starting presentation to {len(target_keys)} client(s)")
        
        for key in target_keys:
            with self.clients_lock:
                if key in self.clients:
                    self.clients[key].send_command("START_PRESENTATION")
        
        self.presentation_thread = threading.Thread(
            target=self._presentation_loop,
            args=(target_keys,),
            daemon=True
        )
        self.presentation_thread.start()
    
    def stop_presentation(self):
        if not self.presenting:
            return
        
        self.presenting = False
        self.log("Stopping presentation")
        
        with self.clients_lock:
            for handler in self.clients.values():
                handler.send_command("STOP_PRESENTATION")
    
    def _presentation_loop(self, target_keys):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            
            fps = getattr(self, 'presentation_fps', PRESENTATION_FPS)
            quality = getattr(self, 'presentation_quality', PRESENTATION_QUALITY)
            scale = getattr(self, 'presentation_scale', PRESENTATION_SCALE)
            
            frame_time = 1.0 / fps
            
            self.log(f"📽️ Presentation: {fps} FPS, Quality: {quality}, Scale: {scale*100:.0f}%")
            
            frame_count = 0
            total_bytes = 0
            start_time = time.time()
            
            while self.presenting:
                loop_start = time.time()
                
                try:
                    screenshot = sct.grab(monitor)
                    frame = np.array(screenshot)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    
                    if scale != 1.0:
                        h, w = frame.shape[:2]
                        new_size = (int(w * scale), int(h * scale))
                        frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
                    
                    encode_params = [
                        cv2.IMWRITE_JPEG_QUALITY, quality,
                        cv2.IMWRITE_JPEG_OPTIMIZE, 1,
                        cv2.IMWRITE_JPEG_PROGRESSIVE, 1
                    ]
                    
                    success, encoded = cv2.imencode('.jpg', frame, encode_params)
                    
                    if not success:
                        continue
                    
                    data = encoded.tobytes()
                    frame_count += 1
                    total_bytes += len(data)
                    
                    header = b"PRESENT_FRAME\n" + struct.pack(">Q", len(data))
                    
                    failed_keys = []
                    with self.clients_lock:
                        for key in list(target_keys):
                            if key in self.clients:
                                try:
                                    with self.clients[key].lock:
                                        self.clients[key].sock.sendall(header + data)
                                except:
                                    failed_keys.append(key)
                    
                    for key in failed_keys:
                        if key in target_keys:
                            target_keys.remove(key)
                    
                    if frame_count % (fps * 5) == 0:
                        elapsed = time.time() - start_time
                        actual_fps = frame_count / elapsed if elapsed > 0 else 0
                        avg_size = total_bytes / frame_count if frame_count > 0 else 0
                        self.log(f"📊 Stats: {actual_fps:.1f} FPS, {avg_size/1024:.1f} KB/frame")
                    
                    elapsed = time.time() - loop_start
                    sleep_time = max(0, frame_time - elapsed)
                    time.sleep(sleep_time)
                
                except Exception as e:
                    self.log(f"❌ Presentation error: {e}")
                    break
            
            total_time = time.time() - start_time
            avg_fps = frame_count / total_time if total_time > 0 else 0
            self.log(f"📊 Presentation ended: {frame_count} frames, {avg_fps:.1f} avg FPS")
            self.presenting = False
    
    def log(self, msg):
        timestamp = now_ts()
        self.log_queue.put(f"[{timestamp}] {msg}")
    
    def list_clients(self):
        with self.clients_lock:
            return sorted(list(self.clients.keys()))
    
    def get_server_stats(self):
        uptime = time.time() - self.start_time if self.start_time else 0
        with self.clients_lock:
            active_clients = len(self.clients)
        
        return {
            "uptime": uptime,
            "active_clients": active_clients,
            "total_connections": self.total_connections
        }


class AdminWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lab Manager - Admin Server")
        self.resize(1400, 900)
        
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #e0e0e0;
                font-family: 'Segoe UI', Arial, sans-serif;
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
                color: #666;
            }
            QListWidget, QTextEdit {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
            }
            QListWidget::item:selected {
                background-color: #094771;
            }
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
            }
        """)
        
        self.server = AdminServer()
        self.selected_preview_client = None
        
        self._build_ui()
        self._start_timers()
    
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        
        header = QLabel("🎓 Lab Manager - Admin Control Panel")
        header.setFont(QFont("Segoe UI", 20, QFont.Bold))
        header.setStyleSheet("color: #0078d4; padding: 10px;")
        main_layout.addWidget(header)
        
        status_layout = QHBoxLayout()
        self.lbl_server_status = QLabel("⚫ Server: Stopped")
        status_layout.addWidget(self.lbl_server_status)
        
        self.lbl_clients_count = QLabel("👥 Clients: 0")
        status_layout.addWidget(self.lbl_clients_count)
        
        self.lbl_uptime = QLabel("⏱️ Uptime: 00:00:00")
        status_layout.addWidget(self.lbl_uptime)
        status_layout.addStretch()
        main_layout.addLayout(status_layout)
        
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        self.tab_control = self._create_control_tab()
        self.tabs.addTab(self.tab_control, "🎮 Control")
        
        self.tab_monitor = self._create_monitor_tab()
        self.tabs.addTab(self.tab_monitor, "📺 Monitor")
        
        self.tab_logs = self._create_logs_tab()
        self.tabs.addTab(self.tab_logs, "📋 Logs")
        
        self.tab_sync = self._create_sync_tab()
        self.tabs.addTab(self.tab_sync, "💾 File Sync")
    
    def _create_control_tab(self):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        
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
        left_layout.addWidget(QLabel("Connected Clients:"))
        
        self.lst_clients = QListWidget()
        self.lst_clients.setSelectionMode(QListWidget.MultiSelection)
        self.lst_clients.itemSelectionChanged.connect(self._on_client_selection_changed)
        left_layout.addWidget(self.lst_clients)
        
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self.refresh_clients)
        left_layout.addWidget(btn_refresh)
        
        layout.addWidget(left_group, 1)
        
        right_group = QGroupBox("Client Actions")
        right_layout = QVBoxLayout(right_group)
        
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
        
        monitor_group = QGroupBox("📺 Screen Monitoring")
        monitor_layout = QVBoxLayout(monitor_group)
        
        # btn_screenshot = QPushButton("📸 Request Screenshot")
        # btn_screenshot.clicked.connect(lambda: self.send_to_selected("REQUEST_SCREEN"))
        # monitor_layout.addWidget(btn_screenshot)
        
        btn_start_stream = QPushButton("▶️ Start Live View")
        btn_start_stream.clicked.connect(lambda: self.send_to_selected("START_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_start_stream)
        
        btn_stop_stream = QPushButton("⏹️ Stop Live View")
        btn_stop_stream.clicked.connect(lambda: self.send_to_selected("STOP_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_stop_stream)
        
        right_layout.addWidget(monitor_group)
        
        file_group = QGroupBox("📤 File Transfer")
        file_layout = QVBoxLayout(file_group)
        
        btn_send_file = QPushButton("📤 Send File to Selected")
        btn_send_file.clicked.connect(self.send_file_to_selected)
        file_layout.addWidget(btn_send_file)
        
        btn_send_all = QPushButton("📤 Send File to All")
        btn_send_all.clicked.connect(self.send_file_to_all)
        file_layout.addWidget(btn_send_all)
        
        right_layout.addWidget(file_group)
        
        msg_group = QGroupBox("💬 Messaging")
        msg_layout = QVBoxLayout(msg_group)
        
        btn_message = QPushButton("💬 Send Message to Selected")
        btn_message.clicked.connect(self.send_message_to_selected)
        msg_layout.addWidget(btn_message)
        
        btn_broadcast = QPushButton("📢 Broadcast Message")
        btn_broadcast.clicked.connect(self.broadcast_message)
        msg_layout.addWidget(btn_broadcast)
        
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
        
        right_layout.addLayout(backup_restore_layout)
        
        left_group.setLayout(left_layout)
        # left_panel.addWidget(actions_group)
        
        right_layout.addWidget(msg_group)
        right_layout.addStretch()
        
        layout.addWidget(right_group, 1)
        
        return widget
    
    def _create_monitor_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("📺 Live Screen Preview"))
        
        self.lbl_preview = QLabel()
        self.lbl_preview.setAlignment(Qt.AlignCenter)
        self.lbl_preview.setStyleSheet("background-color: #000; border: 2px solid #3c3c3c;")
        self.lbl_preview.setMinimumSize(800, 600)
        self.lbl_preview.setText("Select a client and request screen")
        layout.addWidget(self.lbl_preview)
        
        controls = QHBoxLayout()
        
        btn_save = QPushButton("💾 Save Image")
        btn_save.clicked.connect(self.save_preview_image)
        controls.addWidget(btn_save)
        
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self.refresh_preview)
        controls.addWidget(btn_refresh)
        
        quality_group = QGroupBox("Presentation Quality")
        quality_layout = QHBoxLayout(quality_group)
        
        quality_layout.addWidget(QLabel("Quality:"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["High (85)", "Very High (95)", "Medium (70)", "Low (50)"])
        quality_layout.addWidget(self.quality_combo)
        
        quality_layout.addWidget(QLabel("Scale:"))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["100%", "75%", "50%"])
        quality_layout.addWidget(self.scale_combo)
        
        controls.addWidget(quality_group)
        
        self.btn_present = QPushButton("📽️ Present My Screen")
        self.btn_present.clicked.connect(self.toggle_presentation)
        self.btn_present.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                padding: 12px 24px;
            }
            QPushButton:hover {
                background-color: #0e6b0e;
            }
        """)
        controls.addWidget(self.btn_present)
        
        controls.addStretch()
        
        self.lbl_preview_info = QLabel("No client selected")
        controls.addWidget(self.lbl_preview_info)
        
        layout.addLayout(controls)
        
        return widget
    
    def _create_logs_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("📋 Server Activity Log"))
        
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Consolas", 10))
        layout.addWidget(self.txt_log)
        
        log_controls = QHBoxLayout()
        
        btn_clear = QPushButton("🗑️ Clear Log")
        btn_clear.clicked.connect(lambda: self.txt_log.clear())
        log_controls.addWidget(btn_clear)
        
        btn_save = QPushButton("💾 Save Log")
        btn_save.clicked.connect(self.save_log)
        log_controls.addWidget(btn_save)
        
        log_controls.addStretch()
        layout.addLayout(log_controls)
        
        return widget
    
    def _start_timers(self):
        self.timer_log = QTimer(self)
        self.timer_log.setInterval(200)
        self.timer_log.timeout.connect(self._drain_logs)
        self.timer_log.start()
        
        self.timer_clients = QTimer(self)
        self.timer_clients.setInterval(2000)
        self.timer_clients.timeout.connect(self.refresh_clients)
        self.timer_clients.start()
        
        self.timer_frames = QTimer(self)
        self.timer_frames.setInterval(16)
        self.timer_frames.timeout.connect(self._update_frames)
        self.timer_frames.start()
        
        self.timer_status = QTimer(self)
        self.timer_status.setInterval(1000)
        self.timer_status.timeout.connect(self._update_status)
        self.timer_status.start()
        
        self.server.signals.new_frame.connect(self._on_new_frame)
    
    def _drain_logs(self):
        while True:
            try:
                msg = self.server.log_queue.get_nowait()
                self.txt_log.append(msg)
                scrollbar = self.txt_log.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())
            except Empty:
                break
    
    def _update_frames(self):
        if not self.selected_preview_client:
            return
        
        with self.server.frame_locks[self.selected_preview_client]:
            buffer = self.server.frame_buffers.get(self.selected_preview_client)
            if buffer and len(buffer) > 0:
                frame_data = buffer[-1]
                self._display_image_bytes(frame_data)
    
    def _update_status(self):
        if self.server.running.is_set():
            stats = self.server.get_server_stats()
            
            hours, remainder = divmod(int(stats['uptime']), 3600)
            minutes, seconds = divmod(remainder, 60)
            self.lbl_uptime.setText(f"⏱️ Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
            
            self.lbl_clients_count.setText(f"👥 Clients: {stats['active_clients']}")
    
    def _on_new_frame(self, client_key, frame_data):
        if client_key == self.selected_preview_client:
            self._display_image_bytes(frame_data)
    
    def _on_client_selection_changed(self):
        keys = self._get_selected_keys()
        if keys:
            self.selected_preview_client = keys[0]
            self.lbl_preview_info.setText(f"Monitoring: {keys[0]}")
        else:
            self.selected_preview_client = None
            self.lbl_preview_info.setText("No client selected")
    
    def start_server(self):
        if self.server.start():
            self.lbl_server_status.setText(f"🟢 Server: Running on {self.server.host}:{self.server.port}")
            self.lbl_server_status.setStyleSheet("color: #4EC9B0;")
            self.btn_start_server.setEnabled(False)
            self.btn_stop_server.setEnabled(True)
        else:
            QMessageBox.critical(self, "Error", "Failed to start server")
    
    def stop_server(self):
        reply = QMessageBox.question(
            self, "Stop Server",
            "Stop server and disconnect all clients?",
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
        keys = self.server.list_clients()
        selected = set([it.data(Qt.UserRole) for it in self.lst_clients.selectedItems() if it.data(Qt.UserRole)])
        
        # Get current keys
        current_keys = set([self.lst_clients.item(i).data(Qt.UserRole) 
                           for i in range(self.lst_clients.count())])
        
        if current_keys == set(keys):
            return
        
        # Build hostname map and detect duplicates
        hostname_map = {}
        duplicate_hostnames = set()
        
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    hostname = handler.client_info.get("hostname", k.split(":")[0])
                    
                    if hostname in hostname_map:
                        duplicate_hostnames.add(hostname)
                    else:
                        hostname_map[hostname] = []
                    hostname_map[hostname].append(k)
        
        # Alert if duplicates found
        if duplicate_hostnames:
            dup_list = "\n".join([f"  • {h} ({len(hostname_map[h])} clients)" for h in duplicate_hostnames])
            self.server.log(f"⚠️ WARNING: Duplicate PC names detected:\n{dup_list}")
        
        self.lst_clients.clear()
        for k in keys:
            from PyQt5.QtWidgets import QListWidgetItem
            
            # Get hostname
            hostname = k.split(":")[0]  # Default to IP
            with self.server.clients_lock:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    hostname = handler.client_info.get("hostname", hostname)
            
            # Check if this hostname is duplicated
            is_duplicate = hostname in duplicate_hostnames
            
            # Format display text
            if is_duplicate:
                display_text = f"⚠️ {hostname} ({k.split(':')[0]})"  # Show warning and IP
            else:
                display_text = f"💻 {hostname} ({k.split(':')[0]})"  # Show hostname and IP
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, k)  # Store actual key for reference
            
            if k in selected:
                item.setSelected(True)
            
            self.lst_clients.addItem(item)
    
    def _get_selected_keys(self):
        return [it.data(Qt.UserRole) for it in self.lst_clients.selectedItems()]
    
    def send_to_selected(self, command):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        sent = 0
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    if self.server.clients[k].send_command(command):
                        sent += 1
        
        self.server.log(f"📨 Sent '{command}' to {sent}/{len(keys)} clients")
    
    def send_file_to_selected(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients")
            return
        
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send")
        if not path:
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
    
    def send_file_to_all(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send to All")
        if not path:
            return
        
        keys = self.server.list_clients()
        if not keys:
            QMessageBox.warning(self, "No Clients", "No connected clients")
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
        
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    threading.Thread(
                        target=self.server.clients[k].send_file_resumable,
                        args=(path, destination),
                        daemon=True
                    ).start()
        
        filesize = os.path.getsize(path)
        QMessageBox.information(
            self, "File Transfer",
            f"Sending {os.path.basename(path)} ({format_bytes(filesize)}) to {len(keys)} clients\n"
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
    
    def broadcast_message(self):
        text, ok = QInputDialog.getText(
            self, "Broadcast Message",
            "Enter message to broadcast:"
        )
        
        if ok and text:
            self.server.broadcast_command(f"MESSAGE:{text}")
    
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
            
    def backup_client_files(self):
        """Request backup from selected clients with file picker UI"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients to backup")
            return
        
        # Show dialog to choose source path
        source_path, ok = QInputDialog.getText(
            self, "Backup Source Path",
            "Enter the path to backup on the client(s):\n(e.g., C:\\Users\\Student\\Documents)",
            QLineEdit.Normal,
            "C:\\Users\\Student\\Documents"
        )
        
        if not ok or not source_path:
            return
        
        # Allow admin to choose backup destination with file browser
        backup_dest = QFileDialog.getExistingDirectory(
            self,
            "Select Backup Destination Folder",
            BACKUP_DIR,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        
        # If no directory selected, use default
        if not backup_dest:
            backup_dest = BACKUP_DIR
        
        # Create timestamped subfolder
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_backup_dir = os.path.join(backup_dest, f"Backup_{timestamp}")
        os.makedirs(final_backup_dir, exist_ok=True)
        
        reply = QMessageBox.question(
            self, "Confirm Backup",
            f"Request backup from {len(keys)} client(s)?\n\n"
            f"Source (on clients): {source_path}\n"
            f"Destination (on this PC): {final_backup_dir}\n\n"
            f"Files will be organized by client hostname.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Store the custom backup directory in server
            self.server.custom_backup_dir = final_backup_dir
            
            try:
                success_count = self.server.request_backup_from_clients(keys, source_path)
                QMessageBox.information(
                    self, "Backup Started",
                    f"Backup request sent to {success_count} client(s)\n\n"
                    f"Files will be saved to:\n{final_backup_dir}\n\n"
                    f"Check the log for progress updates."
                )
            finally:
                # Clear custom backup directory
                self.server.custom_backup_dir = None
    
    def restore_client_files(self):
        """Restore backed up files to selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients to restore")
            return
        
        restore_path, ok = QInputDialog.getText(
            self, "Restore Destination Path",
            "Enter the path to restore files to (e.g., C:\\Users\\Student\\Documents):",
            QLineEdit.Normal,
            "C:\\Users\\Student\\Documents"
        )
        
        if not ok or not restore_path:
            return
        
        reply = QMessageBox.question(
            self, "Confirm Restore",
            f"Restore files to {len(keys)} client(s)?\n\n"
            f"Destination: {restore_path}\n"
            f"WARNING: This will overwrite existing files!",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            success_count = self.server.restore_to_clients(keys, restore_path)
            QMessageBox.information(
                self, "Restore Started",
                f"Restore initiated for {success_count} client(s)"
            )
    
    def _create_sync_tab(self):
        """Create the file sync tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        layout.addWidget(QLabel("💾 Automatic File Synchronization"))
        
        # Sync configuration
        config_group = QGroupBox("Sync Configuration")
        config_layout = QVBoxLayout(config_group)
        
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("Source Path:"))
        self.txt_sync_path = QLineEdit()
        self.txt_sync_path.setPlaceholderText("e.g., C:\\Users\\Student\\Documents")
        path_layout.addWidget(self.txt_sync_path)
        
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_sync_path)
        path_layout.addWidget(btn_browse)
        config_layout.addLayout(path_layout)
        
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("Sync Interval (seconds):"))
        self.txt_sync_interval = QLineEdit("30")
        self.txt_sync_interval.setMaximumWidth(100)
        interval_layout.addWidget(self.txt_sync_interval)
        interval_layout.addStretch()
        config_layout.addLayout(interval_layout)
        
        layout.addWidget(config_group)
        
        # Sync controls
        controls_layout = QHBoxLayout()
        
        self.btn_start_sync = QPushButton("▶️ Start Auto Sync")
        self.btn_start_sync.clicked.connect(self._start_sync)
        controls_layout.addWidget(self.btn_start_sync)
        
        self.btn_stop_sync = QPushButton("⏹️ Stop Auto Sync")
        self.btn_stop_sync.clicked.connect(self._stop_sync)
        self.btn_stop_sync.setEnabled(False)
        controls_layout.addWidget(self.btn_stop_sync)
        
        controls_layout.addStretch()
        layout.addLayout(controls_layout)
        
        # Sync status
        self.lbl_sync_status = QLabel("Status: Not running")
        layout.addWidget(self.lbl_sync_status)
        
        # Sync log
        layout.addWidget(QLabel("Sync Activity:"))
        self.txt_sync_log = QTextEdit()
        self.txt_sync_log.setReadOnly(True)
        self.txt_sync_log.setFont(QFont("Consolas", 9))
        layout.addWidget(self.txt_sync_log)
        
        return widget
    
    def _browse_sync_path(self):
        """Browse for sync source path"""
        path = QFileDialog.getExistingDirectory(self, "Select Source Directory")
        if path:
            self.txt_sync_path.setText(path)
    
    def _start_sync(self):
        """Start automatic file sync"""
        source_path = self.txt_sync_path.text().strip()
        if not source_path:
            QMessageBox.warning(self, "No Path", "Please enter a source path")
            return
        
        try:
            interval = int(self.txt_sync_interval.text())
            if interval < 5:
                QMessageBox.warning(self, "Invalid Interval", "Interval must be at least 5 seconds")
                return
        except ValueError:
            QMessageBox.warning(self, "Invalid Interval", "Please enter a valid number")
            return
        
        # Initialize sync manager if not exists
        if not hasattr(self.server, 'sync_manager'):
            self.server.sync_manager = FileSyncManager(self.server)
        
        self.server.sync_manager.set_global_source_path(source_path)
        self.server.sync_manager.start_sync_cycle(interval)
        
        self.btn_start_sync.setEnabled(False)
        self.btn_stop_sync.setEnabled(True)
        self.lbl_sync_status.setText(f"Status: Running (syncing every {interval}s)")
        self.lbl_sync_status.setStyleSheet("color: #4EC9B0;")
    
    def _stop_sync(self):
        """Stop automatic file sync"""
        if hasattr(self.server, 'sync_manager'):
            self.server.sync_manager.stop_sync_cycle()
        
        self.btn_start_sync.setEnabled(True)
        self.btn_stop_sync.setEnabled(False)
        self.lbl_sync_status.setText("Status: Not running")
        self.lbl_sync_status.setStyleSheet("color: #e0e0e0;")
    
    def closeEvent(self, event):
        if hasattr(self.server, 'sync_manager'):
            self.server.sync_manager.stop_sync_cycle()
        event.accept()

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Lab Manager - Admin")
    
    window = AdminWindow()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()