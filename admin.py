# admin_server.py - Enhanced Admin Server
# Full-featured controller for student/teacher clients with improved UI and features
#testing

from email.mime import message
import sys
import os
import socket
import threading
import struct
import io
import json
import time
import mss
import numpy as np
import cv2
from PIL import ImageGrab, Image
from datetime import datetime
from queue import Queue, Empty
from collections import defaultdict
import hashlib
from collections import deque
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QTextEdit, QSizePolicy, QSplitter, QInputDialog,
    QGroupBox, QCheckBox, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QProgressBar,QComboBox
)
from PyQt5.QtCore import Qt, QTimer, QByteArray,QObject,pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QFont, QColor

# ============ Configuration ============\
FILE_TRANSFER_BUFFER = 1024 * 1024 * 4  # 4MB chunks
CHUNK_SIZE = 1024 * 1024  # 1MB chunks for resumable transfers
RESUME_METADATA_DIR = os.path.join(os.path.expanduser("~"), "lab_transfer_cache")
os.makedirs(RESUME_METADATA_DIR, exist_ok=True)
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

# Constants (you can adjust)
SCREENSHOT_QUALITY = 40      # Lower quality for speed
SCREEN_SCALE = 0.5           # Scale to 50% 
SCREEN_SHARE_FPS = 30        # Target FPS
FRAME_BUFFER_SIZE = 3        # Keep only latest 3 frames per client
PRESENTATION_FPS = 30           # Target frames per second
PRESENTATION_QUALITY = 85       # JPEG quality (70-95 for high quality)
PRESENTATION_SCALE = 1.0        # 1.0 = full resolution, 0.75 = 75% scale, 0.5 = 50% scale

# ------------------------------------------------------------------------------
class ResumableFileTransfer:
    """Handles resumable file transfers with checksums"""
    
    def __init__(self, filepath, destination, transfer_id=None):
        self.filepath = filepath
        self.destination = destination
        self.filesize = os.path.getsize(filepath)
        self.basename = os.path.basename(filepath)
        
        # Generate unique transfer ID based on file path and size
        self.transfer_id = transfer_id or self._generate_transfer_id()
        
        # Calculate total chunks
        self.total_chunks = (self.filesize + CHUNK_SIZE - 1) // CHUNK_SIZE
        
        # Metadata file for tracking progress
        self.metadata_file = os.path.join(
            RESUME_METADATA_DIR, 
            f"{self.transfer_id}.json"
        )
        
        # Load or initialize progress
        self.completed_chunks = set()
        self.chunk_checksums = {}
        self._load_progress()
    
    def _generate_transfer_id(self):
        """Generate unique transfer ID"""
        unique_str = f"{self.filepath}_{self.filesize}_{int(time.time())}"
        return hashlib.sha256(unique_str.encode()).hexdigest()[:16]
    
    def _calculate_chunk_checksum(self, data):
        """Calculate SHA256 checksum for chunk"""
        return hashlib.sha256(data).hexdigest()
    
    def _load_progress(self):
        """Load transfer progress from metadata file"""
        try:
            if os.path.exists(self.metadata_file):
                with open(self.metadata_file, 'r') as f:
                    metadata = json.load(f)
                    self.completed_chunks = set(metadata.get('completed_chunks', []))
                    self.chunk_checksums = metadata.get('chunk_checksums', {})
        except Exception as e:
            print(f"⚠️ Could not load progress: {e}")
            self.completed_chunks = set()
            self.chunk_checksums = {}
    
    def _save_progress(self):
        """Save transfer progress to metadata file"""
        try:
            metadata = {
                'transfer_id': self.transfer_id,
                'filepath': self.filepath,
                'destination': self.destination,
                'filesize': self.filesize,
                'total_chunks': self.total_chunks,
                'completed_chunks': list(self.completed_chunks),
                'chunk_checksums': self.chunk_checksums,
                'last_update': time.time()
            }
            with open(self.metadata_file, 'w') as f:
                json.dump(metadata, f)
        except Exception as e:
            print(f"⚠️ Could not save progress: {e}")
    
    def get_pending_chunks(self):
        """Get list of chunks that still need to be sent"""
        return [i for i in range(self.total_chunks) if i not in self.completed_chunks]
    
    def mark_chunk_complete(self, chunk_index, checksum):
        """Mark a chunk as successfully transferred"""
        self.completed_chunks.add(chunk_index)
        self.chunk_checksums[str(chunk_index)] = checksum
        self._save_progress()
    
    def is_complete(self):
        """Check if transfer is complete"""
        return len(self.completed_chunks) == self.total_chunks
    
    def get_progress(self):
        """Get transfer progress percentage"""
        return (len(self.completed_chunks) / self.total_chunks) * 100 if self.total_chunks > 0 else 0
    
    def cleanup(self):
        """Remove metadata file after successful transfer"""
        try:
            if os.path.exists(self.metadata_file):
                os.remove(self.metadata_file)
        except:
            pass
# ---------------------------------------------------------------------------------------------------------------
class ServerSignals(QObject):
    """Qt signals for thread-safe UI updates"""
    new_frame = pyqtSignal(str, bytes)  # client_key, frame_data

# ============ Client Handler ============
class ClientHandler:
    """Enhanced client handler with screen share support"""

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

        # Screen share
        self.sharing_active = False
        self.client_socket = sock
        self.connected = True

    # def start_screen_share(self):
    #     """Start continuous screen sharing"""

    #     if not self.connected:
    #         print("[❌] Cannot start screen sharing: not connected")
    #         return
    #     if getattr(self, "sharing_active", False):
    #         print("[ℹ️] Screen sharing is already active")
    #         return

    #     self.sharing_active = True
    #     print("[🖥️] Starting screen sharing...")

    #     def share_loop():
    #         with mss.mss() as sct:
    #             monitor = sct.monitors[1]  # Primary display
    #             while self.sharing_active and self.connected:
    #                 try:
    #                     frame = np.array(sct.grab(monitor))
    #                     frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    #                     # Encode frame in memory for streaming (not saving)
    #                     success, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 50])
    #                     if not success:
    #                         continue

    #                     data = encoded.tobytes()
    #                     header = b"FRAME\n"
    #                     size = struct.pack(">Q", len(data))
    #                     self.client_socket.sendall(header + size + data)

    #                     # Optional: track stats
    #                     self.frames_received = getattr(self, "frames_received", 0) + 1
    #                     self.bytes_received = getattr(self, "bytes_received", 0) + len(data)

    #                     # Control frame rate
    #                     time.sleep(SCREEN_SHARE_INTERVAL)

    #                 except Exception as e:
    #                     print(f"[⚠️] Screen share error: {e}")
    #                     break

    #         # Clean up after stopping
    #         self.sharing_active = False
    #         print("[🛑] Screen sharing stopped")

    #     # Run in a separate thread
    #     threading.Thread(target=share_loop, daemon=True).start()

    def stop_screen_share(self):
        """Stop continuous screen sharing"""
        if getattr(self, "sharing_active", False):
            self.sharing_active = False
            print("[🛑] Stopping screen sharing...")
        else:
            print("[ℹ️] Screen sharing is not active")

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

    # In admin_server.py, ClientHandler class:
    def request_file_from_client(self, remote_path: str, save_to: str = None):
        """Request a file from client's filesystem"""
        try:
            save_to = save_to or INBOX_DIR
            
            # Send request
            request = json.dumps({
                "command": "SEND_FILE_TO_ADMIN",
                "filepath": remote_path
            })
            self.send_command(request)
            
            self.server.log(f"📥 Requesting file from {self.key}: {remote_path}")
            return True
            
        except Exception as e:
            self.server.log(f"❌ File request error: {e}")
            return False
    # In ClientHandler class, update send_file_resumable method:

    def send_file_resumable(self, filepath: str, destination: str = None):
        """Send file with resume capability - FIXED VERSION"""
        if not os.path.exists(filepath):
            self.server.log(f"❌ File not found: {filepath}")
            return False
        
        try:
            transfer = ResumableFileTransfer(filepath, destination or "Downloads")
            basename = transfer.basename
            filesize = transfer.filesize
            
            self.server.log(f"📤 Starting transfer: {basename} ({format_bytes(filesize)})")
            
            pending_chunks = transfer.get_pending_chunks()
            if len(pending_chunks) < transfer.total_chunks:
                self.server.log(f"🔄 Resume: {len(transfer.completed_chunks)}/{transfer.total_chunks} chunks done")
            
            # Optimize socket
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
            except:
                pass
            
            # Send header
            init_header = {
                "command": "RESUMABLE_TRANSFER_START",
                "transfer_id": transfer.transfer_id,
                "filename": basename,
                "destination": transfer.destination,
                "filesize": filesize,
                "total_chunks": transfer.total_chunks,
                "chunk_size": CHUNK_SIZE
            }
            
            init_json = json.dumps(init_header).encode('utf-8')
            header = b"RESUMABLE_FILE\n" + struct.pack(">I", len(init_json)) + init_json
            
            with self.lock:
                self.sock.sendall(header)
                
                # Wait for READY
                self.sock.settimeout(15.0)
                try:
                    ack = self.sock.recv(1024)
                    if b"READY" not in ack:
                        raise Exception(f"Client not ready: {ack}")
                finally:
                    self.sock.settimeout(None)
                
                # Send chunks
                start_time = time.time()
                sent_bytes = 0
                
                with open(filepath, "rb") as f:
                    for chunk_index in pending_chunks:
                        f.seek(chunk_index * CHUNK_SIZE)
                        chunk_data = f.read(CHUNK_SIZE)
                        if not chunk_data:
                            break
                        
                        checksum = transfer._calculate_chunk_checksum(chunk_data)
                        chunk_header = struct.pack(">II", chunk_index, len(chunk_data))
                        chunk_header += checksum.encode('utf-8').ljust(64, b'\x00')
                        self.sock.sendall(chunk_header + chunk_data)
                        
                        # Wait for ACK
                        self.sock.settimeout(5.0)
                        try:
                            chunk_ack = self.sock.recv(32)
                            if b"CHUNK_OK" not in chunk_ack:
                                raise Exception(f"Chunk {chunk_index} failed")
                        finally:
                            self.sock.settimeout(None)
                        
                        transfer.mark_chunk_complete(chunk_index, checksum)
                        sent_bytes += len(chunk_data)
                        
                        # Log progress
                        if time.time() - start_time >= 2.0:
                            progress = transfer.get_progress()
                            speed = sent_bytes / (time.time() - start_time)
                            self.server.log(f"📊 {progress:.1f}% | {format_bytes(speed)}/s")
                            start_time = time.time()
                            sent_bytes = 0
                
                # Send completion (don't wait for response)
                self.sock.sendall(b"TRANSFER_COMPLETE\n")
            
            transfer.cleanup()
            self.server.log(f"✅ Complete: {basename}")
            return True
            
        except Exception as e:
            self.server.log(f"❌ Transfer error: {e}")
            return False

    def resume_file_transfer(self, transfer_id: str):
        """Resume a previously interrupted transfer"""
        try:
            metadata_file = os.path.join(RESUME_METADATA_DIR, f"{transfer_id}.json")
            
            if not os.path.exists(metadata_file):
                self.server.log(f"❌ No saved progress for transfer ID: {transfer_id}")
                return False
            
            # Load metadata
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
            
            filepath = metadata['filepath']
            destination = metadata['destination']
            
            self.server.log(f"🔄 Resuming transfer: {os.path.basename(filepath)}")
            
            # Resume transfer
            return self.send_file(filepath, destination)
            
        except Exception as e:
            self.server.log(f"❌ Resume transfer error: {e}")
            return False


    # In admin_server.py, update ClientHandler._reader_loop to handle socket errors gracefully:

    def _reader_loop(self):
        """Read data from client"""
        sock = self.sock
        sock.settimeout(30.0)

        try:
            buffer = b""
            consecutive_errors = 0
            max_consecutive_errors = 3
            
            while self.running.is_set():
                try:
                    data = sock.recv(RECV_BUFFER)
                    if not data:
                        self.server.log(f"⚠️ Client {self.key} closed connection")
                        break

                    # Reset error counter
                    consecutive_errors = 0
                    buffer += data

                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        header = line.decode('utf-8', errors='ignore').strip()
                        if not header:
                            continue

                        try:
                            # ✅ Handle live screen frames (do NOT save)
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

                                # ✅ Only show on UI, never save
                                self.last_image = frame_data
                                self.last_image_ts = time.time()
                                self.frames_received += 1
                                self.bytes_received += len(frame_data)

                                if hasattr(self.server, "signals"):
                                    self.server.signals.new_frame.emit(self.key, frame_data)
                                else:
                                    self.server.on_client_frame(self.key, frame_data)

                            elif header.upper() == "HEARTBEAT":
                                self.last_heartbeat = time.time()

                            elif header.upper().startswith("STATUS"):
                                self.server.log(f"📊 Status from {self.key}: {header}")

                            elif header.upper().startswith("MSG"):
                                self.server.log(f"💬 Message from {self.key}: {header}")

                            else:
                                self.server.log(f"📝 From {self.key}: {header}")

                        except ConnectionError as ce:
                            self.server.log(f"⚠️ Connection error processing header from {self.key}: {ce}")
                            break
                        except Exception as header_error:
                            self.server.log(f"⚠️ Error processing header '{header}' from {self.key}: {header_error}")
                            continue

                except socket.timeout:
                    if time.time() - self.last_heartbeat > 60:
                        self.server.log(f"⏱️ Client {self.key} timed out")
                        break
                    continue

                except ConnectionResetError:
                    self.server.log(f"⚠️ Connection reset by {self.key}")
                    break
                except ConnectionAbortedError:
                    self.server.log(f"⚠️ Connection aborted by {self.key}")
                    break
                except OSError as e:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        self.server.log(f"⚠️ Too many socket errors from {self.key}: {e}")
                        break
                    time.sleep(0.1)
                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        self.server.log(f"⚠️ Read error from {self.key}: {e}")
                        import traceback
                        self.server.log(f"Traceback: {traceback.format_exc()}")
                        break
                    time.sleep(0.1)

        except Exception as e:
            self.server.log(f"❌ Handler error for {self.key}: {e}")
        finally:
            self.running.clear()
            self.client_info["status"] = "disconnected"
            self.server.log(f"❌ {self.key} disconnected")
            self.server.remove_client(self.key)
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


    def _reader_loop(self):
        """Read data from client"""
        sock = self.sock
        sock.settimeout(30.0)

        try:
            buffer = b""
            while self.running.is_set():
                try:
                    data = sock.recv(RECV_BUFFER)
                    if not data:
                        self.server.log(f"⚠️ Client {self.key} closed connection")
                        break

                    buffer += data

                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        header = line.decode('utf-8', errors='ignore').strip()
                        if not header:
                            continue

                        try:
                            # ✅ Handle live screen frames (do NOT save)
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

                                # ✅ Only show on UI, never save
                                self.last_image = frame_data
                                self.last_image_ts = time.time()
                                self.frames_received += 1
                                self.bytes_received += len(frame_data)

                                if hasattr(self.server, "signals"):
                                    self.server.signals.new_frame.emit(self.key, frame_data)
                                else:
                                    self.server.on_client_frame(self.key, frame_data)

                            # ✅ Handle actual file transfers only (non-screen)
                            elif header.upper() in ("FILE", "FILE_BACK"):
                                # Peek at metadata to skip screen captures
                                peek = buffer[:200].lower()
                                if b".jpg" in peek or b".jpeg" in peek or b"frame" in peek:
                                    self.server.log(f"🚫 Skipped screen frame pretending to be file from {self.key}")
                                    # discard data instead of saving
                                    buffer = b""
                                    continue

                                # otherwise handle normal file
                                self._receive_file_from_buffer(sock, buffer)
                                buffer = b""

                            elif header.upper() == "HEARTBEAT":
                                self.last_heartbeat = time.time()

                            elif header.upper().startswith("STATUS"):
                                self.server.log(f"📊 Status from {self.key}: {header}")

                            elif header.upper().startswith("MSG"):
                                self.server.log(f"💬 Message from {self.key}: {header}")

                            else:
                                self.server.log(f"📝 From {self.key}: {header}")

                        except Exception as header_error:
                            self.server.log(f"⚠️ Error processing header '{header}' from {self.key}: {header_error}")
                            continue

                except socket.timeout:
                    if time.time() - self.last_heartbeat > 60:
                        self.server.log(f"⏱️ Client {self.key} timed out")
                        break
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

            # # 🚫 Ignore screenshots or JPEG frames
            # if fname.lower().endswith((".jpg", ".jpeg")) or "frame" in fname.lower():
            #     self.server.log(f"🚫 Ignored incoming screen frame file from {self.key}: {fname}")
            #     # Read and discard data instead of saving it
            #     remaining = filesize
            #     while remaining > 0:
            #         chunk = sock.recv(min(RECV_BUFFER, remaining))
            #         if not chunk:
            #             break
            #         remaining -= len(chunk)
            #     return  # Skip saving
            
            outpath = os.path.join(INBOX_DIR, fname)
            tmp_path = outpath + ".part"
            
            self.server.log(f"📥 Receiving file from {self.key}: {fname} ({format_bytes(filesize)})")
            
            # Write file
            with open(tmp_path, "wb") as outf:
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
        
        # Optimized frame handling with per-client buffers
        self.frame_buffers = defaultdict(lambda: deque(maxlen=FRAME_BUFFER_SIZE))
        self.frame_locks = defaultdict(threading.Lock)
        
        self.signals = ServerSignals()
        self.total_connections = 0
        self.start_time = None
        
        # NEW: Presentation mode attributes
        self.presenting = False
        self.presentation_thread = None
    # ============ ADMIN SIDE (admin_server.py) ============



# 2. Add these methods to AdminServer class:
    def start_presentation(self, target_keys):
        """Start presenting admin screen to specific clients"""
        if self.presenting:
            self.log("Presentation already active")
            return
        
        self.presenting = True
        self.log(f"Starting presentation to {len(target_keys)} client(s)")
        
        # Send start presentation command
        for key in target_keys:
            with self.clients_lock:
                if key in self.clients:
                    try:
                        self.clients[key].client_socket.sendall(b"START_PRESENTATION\n")
                    except Exception as e:
                        self.log(f"Failed to send START_PRESENTATION to {key}: {e}")
        
        # Start capture thread
        self.presentation_thread = threading.Thread(
            target=self._presentation_loop,
            args=(target_keys,),
            daemon=True
        )
        self.presentation_thread.start()

    def stop_presentation(self):
        """Stop presenting admin screen"""
        if not self.presenting:
            return
        
        self.presenting = False
        self.log("Stopping presentation")
        
        # Send stop command to all clients
        with self.clients_lock:
            for handler in self.clients.values():
                try:
                    handler.client_socket.sendall(b"STOP_PRESENTATION\n")
                except:
                    pass

    def _presentation_loop(self, target_keys):
        """Capture and send admin's screen to clients - CONFIGURABLE HIGH QUALITY"""
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            
            # Get settings (use defaults if not defined)
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
                    # Capture screen
                    screenshot = sct.grab(monitor)
                    frame = np.array(screenshot)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    
                    # Apply scaling if needed
                    if scale != 1.0:
                        h, w = frame.shape[:2]
                        new_size = (int(w * scale), int(h * scale))
                        frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
                    
                    # High quality compression
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
                    
                    # Send to clients
                    header = b"PRESENT_FRAME\n" + struct.pack(">Q", len(data))
                    
                    failed_keys = []
                    with self.clients_lock:
                        for key in target_keys:
                            if key in self.clients:
                                try:
                                    self.clients[key].client_socket.sendall(header + data)
                                except Exception as e:
                                    failed_keys.append(key)
                    
                    # Remove disconnected clients
                    for key in failed_keys:
                        if key in target_keys:
                            target_keys.remove(key)
                    
                    # Log stats every 5 seconds
                    if frame_count % (fps * 5) == 0:
                        elapsed = time.time() - start_time
                        actual_fps = frame_count / elapsed if elapsed > 0 else 0
                        avg_size = total_bytes / frame_count if frame_count > 0 else 0
                        self.log(f"📊 Presentation stats: {actual_fps:.1f} FPS, {avg_size/1024:.1f} KB/frame")
                    
                    # Maintain target FPS
                    elapsed = time.time() - loop_start
                    sleep_time = max(0, frame_time - elapsed)
                    time.sleep(sleep_time)
                    
                except Exception as e:
                    self.log(f"❌ Presentation error: {e}")
                    break
            
            # Final stats
            total_time = time.time() - start_time
            avg_fps = frame_count / total_time if total_time > 0 else 0
            self.log(f"📊 Presentation ended: {frame_count} frames, {avg_fps:.1f} avg FPS")
            self.presenting = False
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
        """Send command to all connected clients"""
        with self.clients_lock:
            clients = list(self.clients.values())

        success = 0
        for handler in clients:
            try:
                # Send properly encoded command with newline
                handler.client_socket.sendall((cmd_str + "\n").encode())
                success += 1
            except Exception as e:
                self.log(f"❌ Failed to send '{cmd_str}' to a client: {e}")

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
        """Handle received frame — display only, do NOT save to disk"""
        try:
            # Just forward to frame queue or signal for live viewing
            self.frame_queue.put((client_key, image_bytes))
            # (No file saving)
        except Exception as e:
            self.log(f"❌ Error handling live frame from {client_key}: {e}")

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
        self.presenting = False
        self.presentation_thread = None
    def log(self, message):
        """Write log message to console or admin log area"""
        print(message)  # or append to a QTextEdit if you have one

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
        btn_lock_selected.clicked.connect(lambda: self.send_command_to_selected("LOCK"))
        lock_layout.addWidget(btn_lock_selected)
        
        btn_unlock_selected = QPushButton("🔓 Unlock Selected")
        btn_unlock_selected.clicked.connect(lambda: self.send_command_to_selected("UNLOCK"))
        lock_layout.addWidget(btn_unlock_selected)
        
        right_layout.addWidget(lock_group)
        
        # Screen Monitoring
        monitor_group = QGroupBox("📺 Screen Monitoring")
        monitor_layout = QVBoxLayout(monitor_group)
        
        btn_screenshot = QPushButton("📸 Request Screenshot")
        btn_screenshot.clicked.connect(lambda: self.send_command_to_selected("REQUEST_SCREEN"))
        monitor_layout.addWidget(btn_screenshot)
        
        btn_start_stream = QPushButton("▶️ Start Live View")
        btn_start_stream.clicked.connect(lambda: self.send_command_to_selected("START_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_start_stream)
        
        btn_stop_stream = QPushButton("⏹️ Stop Live View")
        btn_stop_stream.clicked.connect(lambda: self.send_command_to_selected("STOP_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_stop_stream)
        
        right_layout.addWidget(monitor_group)
        
        # File Transfer
        file_group = QGroupBox("📤 File Transfer")
        file_layout = QVBoxLayout(file_group)
        
        btn_send_file = QPushButton("📤 Send File to Selected")
        btn_send_file.clicked.connect(self.send_file_to_selected)  # FIXED: Direct method call
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
        
        btn_request_file = QPushButton("📥 Request File from Client")
        btn_request_file.clicked.connect(self.request_file_from_selected)
        file_layout.addWidget(btn_request_file)
        
        right_layout.addWidget(msg_group)
        
        right_layout.addStretch()
        
        layout.addWidget(right_group, 1)
        
        return widget

    def request_file_from_selected(self):
        """Request file from selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        filepath, ok = QInputDialog.getText(
            self, "Request File",
            "Enter full path on client machine:\n(e.g., C:\\Users\\Student\\Desktop\\file.txt)"
        )
        
        if not ok or not filepath:
            return
        
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    self.server.clients[k].request_file_from_client(filepath)
        
        QMessageBox.information(
            self, "File Request",
            f"Requested file from {len(keys)} client(s):\n{filepath}"
        )
    # Add this new method to AdminWindow class:
    def send_command_to_selected(self, command):
        """Send a specific command to selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return

        sent = 0
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    try:
                        self.server.clients[k].client_socket.sendall((command + "\n").encode())
                        sent += 1
                    except Exception as e:
                        self.server.log(f"❌ Failed to send '{command}' to {k}: {e}")

        self.server.log(f"📨 Sent '{command}' to {sent}/{len(keys)} selected clients")

    def _create_monitor_tab(self):
        """Create monitoring tab with Present Screen button"""
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
        
        # NEW: Quality settings group
        quality_group = QGroupBox("Presentation Quality")
        quality_layout = QHBoxLayout(quality_group)
        
        quality_layout.addWidget(QLabel("Quality:"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["High (85)", "Very High (95)", "Medium (70)", "Low (50)"])
        self.quality_combo.setCurrentIndex(0)  # Default to High
        quality_layout.addWidget(self.quality_combo)
        
        quality_layout.addWidget(QLabel("Scale:"))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["100%", "75%", "50%"])
        self.scale_combo.setCurrentIndex(0)  # Default to 100%
        quality_layout.addWidget(self.scale_combo)
        
        controls.addWidget(quality_group)
        
        # Present Screen button
        self.btn_present = QPushButton("📽️ Present My Screen")
        self.btn_present.clicked.connect(self.toggle_presentation)
        self.btn_present.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                color: white;
                font-weight: bold;
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

    def update_preview(self, client_key, data):
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        scaled = pixmap.scaled(
            self.lbl_preview.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        self.lbl_preview.setPixmap(scaled)
        self.lbl_preview_info.setText(f"📡 Live stream from {client_key}")

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
    def toggle_presentation(self):
        """Toggle presentation mode with quality settings"""
        if self.server.presenting:
            # Stop presentation
            self.server.stop_presentation()
            self.btn_present.setText("📽️ Present My Screen")
            self.btn_present.setStyleSheet("""
                QPushButton {
                    background-color: #107c10;
                    color: white;
                    font-weight: bold;
                    padding: 12px 24px;
                }
                QPushButton:hover {
                    background-color: #0e6b0e;
                }
            """)
            self.server.log("Presentation stopped")
        else:
            # Start presentation
            keys = self._get_selected_keys()
            if not keys:
                QMessageBox.warning(self, "No Selection", "Please select one or more clients to present to")
                return
            
            # Get quality settings
            quality_text = self.quality_combo.currentText()
            quality = int(quality_text.split("(")[1].split(")")[0])
            
            scale_text = self.scale_combo.currentText()
            scale = float(scale_text.replace("%", "")) / 100
            
            # Set server presentation settings
            self.server.presentation_quality = quality
            self.server.presentation_scale = scale
            self.server.presentation_fps = 30
            
            reply = QMessageBox.question(
                self,
                "Start Presentation",
                f"Present your screen to {len(keys)} selected client(s)?\n\n"
                f"Quality: {quality}, Scale: {scale*100:.0f}%, FPS: 30\n"
                "Their screens will be locked and show your screen.",
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                self.server.start_presentation(keys)
                self.btn_present.setText("⏹️ Stop Presenting")
                self.btn_present.setStyleSheet("""
                    QPushButton {
                        background-color: #c42b1c;
                        color: white;
                        font-weight: bold;
                        padding: 12px 24px;
                    }
                    QPushButton:hover {
                        background-color: #a52a1a;
                    }
                """)
                self.server.log(f"Started presenting: {quality} quality, {scale*100:.0f}% scale, 30 FPS")
    def _start_timers(self):
        """Start optimized update timers"""
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
        
        # OPTIMIZED: Faster frame updates (60 FPS UI refresh)
        self.timer_frames = QTimer(self)
        self.timer_frames.setInterval(16)  # ~60 FPS
        self.timer_frames.timeout.connect(self._update_frames_optimized)
        self.timer_frames.start()
        
        # Status update timer
        self.timer_status = QTimer(self)
        self.timer_status.setInterval(1000)
        self.timer_status.timeout.connect(self._update_status)
        self.timer_status.start()
        
        # Connect signal for thread-safe frame updates
        self.server.signals.new_frame.connect(self._on_new_frame)

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

    def send_to_selected(self, command=None):
        """DEPRECATED: Use send_command_to_selected or send_file_to_selected instead"""
        if command:
            self.send_command_to_selected(command)
        else:
            self.send_file_to_selected()

        # Add this method to AdminWindow class in admin.py:
    def _on_new_frame(self, client_key, frame_data):
            """Thread-safe frame handler"""
            if client_key == self.selected_preview_client:
                self._display_image_bytes(frame_data)

    def _update_frames_optimized(self):
            """Optimized frame update from buffer"""
            if not self.selected_preview_client:
                return
            
            # Get latest frame from buffer
            with self.server.frame_locks[self.selected_preview_client]:
                buffer = self.server.frame_buffers.get(self.selected_preview_client)
                if buffer and len(buffer) > 0:
                    # Get most recent frame
                    frame_data = buffer[-1]
                    self._display_image_bytes(frame_data)
# =======================================================================================
    def send_file_to_selected(self):
        """Send file to selected clients with destination choice"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        # Choose file
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send")
        if not path:
            return
        
        # Show dialog for destination
        destinations = ["Downloads", "Desktop", "Documents", "Custom Path..."]
        
        destination, ok = QInputDialog.getItem(
            self, "Select Destination",
            "Where should the file be saved on the client?",
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
        
        # Send file to selected clients using resumable transfer
        with self.server.clients_lock:
            sent = 0
            for k in keys:
                if k in self.server.clients:
                    threading.Thread(
                        target=self.server.clients[k].send_file_resumable,  # FIXED
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

    def list_resumable_transfers(self):
        """List all resumable transfers in progress"""
        try:
            transfers = []
            for filename in os.listdir(RESUME_METADATA_DIR):
                if filename.endswith('.json'):
                    filepath = os.path.join(RESUME_METADATA_DIR, filename)
                    try:
                        with open(filepath, 'r') as f:
                            metadata = json.load(f)
                            transfers.append(metadata)
                    except:
                        pass
            return transfers
        except:
            return []
# ==============================================================================================
    def _display_image_bytes(self, img_bytes: bytes):
        """Display image from bytes (fallback method)"""
        self._display_image_bytes(img_bytes)
        
    def send_to_selected(self, command=None):
        """Send command to selected clients or initiate file transfer"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return

        # If no command provided, this is a file transfer request
        if command is None:
            self.send_file_to_selected_resumable()
            return

        # Send command to selected clients
        sent = 0
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    try:
                        self.server.clients[k].client_socket.sendall((command + "\n").encode())
                        sent += 1
                    except Exception as e:
                        self.server.log(f"❌ Failed to send '{command}' to {k}: {e}")

        self.server.log(f"📨 Sent '{command}' to {sent}/{len(keys)} selected clients")


    def send_file_to_selected(self):
        """Send file to selected clients with destination choice"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select one or more clients")
            return
        
        # Choose file
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send")
        if not path:
            return
        
        # Show dialog for destination
        destinations = [
            "Downloads",
            "Desktop",
            "Documents",
            "Custom Path..."
        ]
        
        destination, ok = QInputDialog.getItem(
            self,
            "Select Destination",
            "Where should the file be saved on the client?",
            destinations,
            0,
            False
        )
        
        if not ok:
            return
        
        # If custom path selected, ask user to input it
        if destination == "Custom Path...":
            destination, ok = QInputDialog.getText(
                self,
                "Custom Destination",
                "Enter the full path (e.g., C:\\Users\\Student\\Desktop or /home/user/Documents):",
                text="C:\\"
            )
            if not ok or not destination:
                return
        
        # Determine which transfer method to use based on file size
        filesize = os.path.getsize(path)
        use_resumable = filesize > 100 * 1024 * 1024  # Use resumable for files > 100MB
        
        # Send file to selected clients
        with self.server.clients_lock:
            sent = 0
            for k in keys:
                if k in self.server.clients:
                    if use_resumable:
                        threading.Thread(
                            target=self.server.clients[k].send_file_resumable,
                            args=(path, destination),
                            daemon=True
                        ).start()
                    else:
                        threading.Thread(
                            target=self.server.clients[k].send_file_resumable,
                            args=(path, destination),
                            daemon=True
                        ).start()
                    sent += 1
        
        transfer_type = "Resumable" if use_resumable else "Standard"
        QMessageBox.information(
            self,
            "File Transfer",
            f"{transfer_type} transfer started:\n"
            f"File: {os.path.basename(path)} ({format_bytes(filesize)})\n"
            f"Recipients: {sent} client(s)\n"
            f"Destination: {destination}"
        )

    def send_file_to_all(self):
        """Send file to all clients with destination choice"""
        path, _ = QFileDialog.getOpenFileName(self, "Choose File to Send to All")
        if not path:
            return
        
        keys = self.server.list_clients()
        if not keys:
            QMessageBox.warning(self, "No Clients", "No connected clients")
            return
        
        # Show dialog for destination
        destinations = ["Downloads", "Desktop", "Documents", "Custom Path..."]
        
        destination, ok = QInputDialog.getItem(
            self, "Select Destination",
            "Where should the file be saved on all clients?",
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
        
        # Send file to all clients using resumable transfer
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    threading.Thread(
                        target=self.server.clients[k].send_file_resumable,  # FIXED
                        args=(path, destination),
                        daemon=True
                    ).start()
        
        filesize = os.path.getsize(path)
        QMessageBox.information(
            self, "File Transfer",
            f"Sending {os.path.basename(path)} ({format_bytes(filesize)}) to {len(keys)} client(s)\n"
            f"Destination: {destination}"
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