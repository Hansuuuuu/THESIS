import sys
import os
import socket
import threading
import struct
import time
import io
import json
import mss
import cv2
import numpy as np
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, 
                             QPushButton, QMessageBox, QTextEdit, QProgressBar,
                             QHBoxLayout, QSystemTrayIcon, QMenu, QAction, QInputDialog)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QPixmap, QIcon, QFont, QColor, QPalette, QImage
from PIL import ImageGrab

# ==============================
# Configuration
# ==============================
SERVER_HOST = '192.168.100.30'  # Change this to admin/teacher IP
SERVER_PORT = 5001
BUFFER_SIZE = 65536
RECONNECT_DELAY = 5000  # milliseconds
SCREENSHOT_QUALITY = 85  # JPEG quality (1-100)
STREAM_FPS = 10  # Frames per second for streaming
# Constants (you can adjust)
SCREENSHOT_QUALITY = 60      # JPEG quality
SCREEN_SHARE_INTERVAL = 0.03  # seconds per frame (≈ 30 FPS)


class PresentationOverlay(QWidget):
    """Fullscreen overlay that displays admin's presentation"""
    
    def __init__(self, parent=None):
        super().__init__()
        self.parent_window = parent
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        
        self.setStyleSheet("QWidget { background-color: #000000; }")
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Image display
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background-color: #000000;")
        self.image_label.setText("📽️ Connecting to presentation...")
        self.image_label.setFont(QFont("Segoe UI", 24))
        self.image_label.setStyleSheet("color: white; background-color: #000000;")
        layout.addWidget(self.image_label)
        
        # Info bar at bottom
        info_layout = QHBoxLayout()
        info_layout.setContentsMargins(20, 10, 20, 10)
        
        self.info_label = QLabel("📽️ Presentation Mode - Admin is presenting")
        self.info_label.setStyleSheet("""
            color: white;
            background-color: rgba(0, 120, 212, 180);
            padding: 8px 15px;
            border-radius: 5px;
            font-size: 14px;
            font-weight: bold;
        """)
        info_layout.addWidget(self.info_label)
        info_layout.addStretch()
        
        layout.addLayout(info_layout)
        
        self.setLayout(layout)
    
    def update_frame(self, image_data):
        """Update the displayed frame"""
        try:
            from PyQt5.QtCore import QByteArray
            qimg = QImage.fromData(QByteArray(image_data))
            if not qimg.isNull():
                pix = QPixmap.fromImage(qimg)
                scaled = pix.scaled(
                    self.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled)
        except Exception as e:
            print(f"Error updating presentation frame: {e}")
    
    def showFullScreen(self):
        """Show fullscreen"""
        super().showFullScreen()
        self.setFocus()
        self.raise_()
        self.activateWindow()
    
    def keyPressEvent(self, event):
        """Block all key events except ESC for admin"""
        event.ignore()
    
    def mousePressEvent(self, event):
        """Block mouse clicks"""
        event.ignore()
    
    def closeEvent(self, event):
        """Handle close"""
        event.accept()
        
class LockOverlay(QWidget):
    """Full-screen overlay that blocks all input and shows a lock message"""
    
    def __init__(self, message="🔒 Locked by Administrator", logo_path=None, parent=None):
        super().__init__()
        self.parent_window = parent
        self.unlocked = False
        
        # Use borderless window without modal
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        
        # Make background black
        self.setStyleSheet("""
            QWidget {
                background-color: #000000;
            }
        """)
        
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(30)
        layout.setContentsMargins(50, 50, 50, 50)
        
        # Optional logo
        logo_loaded = False
        if logo_path:
            print(f"[DEBUG] Looking for logo at: {logo_path}")
            print(f"[DEBUG] File exists: {os.path.exists(logo_path)}")
            
            if os.path.exists(logo_path):
                try:
                    logo = QLabel()
                    pix = QPixmap(logo_path)
                    print(f"[DEBUG] Pixmap isNull: {pix.isNull()}")
                    print(f"[DEBUG] Pixmap size: {pix.width()} x {pix.height()}")
                    
                    if not pix.isNull():
                        scaled_pix = pix.scaledToHeight(150, Qt.SmoothTransformation)
                        logo.setPixmap(scaled_pix)
                        logo.setAlignment(Qt.AlignCenter)
                        layout.addWidget(logo)
                        logo_loaded = True
                        print(f"[DEBUG] Logo loaded successfully")
                except Exception as e:
                    print(f"[ERROR] Error loading logo: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print(f"[ERROR] Logo file not found at: {logo_path}")
        
        if not logo_loaded:
            error_label = QLabel("(No logo found)")
            error_label.setStyleSheet("color: #ffcc00; font-size: 12px;")
            error_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(error_label)
        
        # Main message label
        label = QLabel(message)
        label.setStyleSheet("color: white; font-size: 36px; font-weight: bold; margin: 20px;")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        
        # Unlock instruction
        instruction = QLabel("Press 'U' key to unlock")
        instruction.setStyleSheet("color: #cccccc; font-size: 16px;")
        instruction.setAlignment(Qt.AlignCenter)
        layout.addWidget(instruction)
        
        layout.addStretch()
        self.setLayout(layout)
    
    def showFullScreen(self):
        """Show the overlay in fullscreen"""
        super().showFullScreen()
        self.setFocus()
        self.raise_()
        self.activateWindow()
        print(f"[DEBUG] Overlay shown fullscreen")
    
    def keyPressEvent(self, event):
        """Handle key press - only respond to 'U' key for unlock"""
        if event.isAutoRepeat():
            return
        
        if event.key() == Qt.Key_U:
            code, ok = QInputDialog.getText(
                self, 
                "Unlock Screen", 
                "Enter unlock code:",
                text=""
            )
            if ok and code == "admin123":
                print(f"[DEBUG] Correct unlock code entered")
                self.unlocked = True
                self.close()
            elif ok:
                print(f"[DEBUG] Incorrect unlock code entered")
                QMessageBox.warning(self, "Incorrect", "Incorrect unlock code")
        else:
            event.ignore()
    
    def mousePressEvent(self, event):
        """Ignore mouse clicks"""
        pass
    
    def closeEvent(self, event):
        """Handle window close"""
        print(f"[DEBUG] LockOverlay closeEvent triggered")
        # DO NOT set parent_window.overlay = None here
        # Let the parent handle it
        event.accept()
        
class LockSignals(QObject):
    """Signal emitter for thread-safe lock screen operations"""
    lock_requested = pyqtSignal(str)  # logo_path
    unlock_requested = pyqtSignal()

# ==============================
# Signal Handler for Thread-Safe GUI Updates
# ==============================
class SignalHandler(QObject):
    update_status = pyqtSignal(str, str)  # (message, color)
    show_message = pyqtSignal(str, str)  # (title, message)
    file_progress = pyqtSignal(int, str)  # (percentage, status)
    log_message = pyqtSignal(str)  # log entry

# ==============================
# Improved Student Client GUI
# ==============================
class StudentClient(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Student Client - Enhanced")
        self.setGeometry(100, 100, 900, 650)
        self.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
                color: #e0e0e0;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
            }
            QPushButton {
                background-color: #3c3c3c;
                border: 1px solid #555;
                padding: 10px;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
            }
            QPushButton:pressed {
                background-color: #2a2a2a;
            }
            QLabel {
                padding: 5px;
            }
            QTextEdit {
                background-color: #1e1e1e;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 8px;
            }
            QProgressBar {
                border: 1px solid #444;
                border-radius: 4px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
            }
        """)
        
        # Initialize variables
        self.client_socket = None
        self.connected = False
        self.screen_sharing = False
        self.locked = False
        self.running = True
        self.reconnect_timer = None
        self.heartbeat_timer = None
        
        # Signal handler for thread-safe updates
        self.signals = SignalHandler()
        self.signals.update_status.connect(self.update_status_label)
        self.signals.show_message.connect(self.display_message)
        self.signals.file_progress.connect(self.update_file_progress)
        self.signals.log_message.connect(self.append_log)
        
        # Signal handler for thread-safe updates
        self.signals = SignalHandler()
        self.signals.update_status.connect(self.update_status_label)
        self.signals.show_message.connect(self.display_message)
        self.signals.file_progress.connect(self.update_file_progress)
        self.signals.log_message.connect(self.append_log)
        
        # ADD THIS: Lock screen signals
        self.lock_signals = LockSignals()
        self.lock_signals.lock_requested.connect(self._create_lock_overlay)
        self.lock_signals.unlock_requested.connect(self.unlock_screen)
        
        self.setup_ui()
        self.setup_system_tray()
        
        # Start connection attempt
        self.log("Application started")
        QTimer.singleShot(500, self.attempt_connection)
        self.presentation_overlay = None
        self.presentation_signals = LockSignals()
        self.presentation_signals.lock_requested.connect(self._show_presentation)
        self.presentation_signals.unlock_requested.connect(self._hide_presentation)
    def setup_ui(self):
        """Setup the user interface"""
        main_layout = QVBoxLayout()
        
        # Header
        header = QLabel("📚 Student Client")
        header.setFont(QFont("Segoe UI", 18, QFont.Bold))
        header.setAlignment(Qt.AlignCenter)
        header.setStyleSheet("color: #0078d4; padding: 15px;")
        main_layout.addWidget(header)
        
        # Status section
        self.status_label = QLabel("🔄 Connecting to server...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 12))
        self.status_label.setStyleSheet("background-color: #3c3c3c; padding: 15px; border-radius: 8px;")
        main_layout.addWidget(self.status_label)
        
        # Connection info
        self.connection_info = QLabel(f"Server: {SERVER_HOST}:{SERVER_PORT}")
        self.connection_info.setAlignment(Qt.AlignCenter)
        self.connection_info.setStyleSheet("color: #888; padding: 5px;")
        main_layout.addWidget(self.connection_info)
        
        # Control buttons
        button_layout = QHBoxLayout()
        
        self.reconnect_button = QPushButton("🔄 Reconnect")
        self.reconnect_button.clicked.connect(self.manual_reconnect)
        self.reconnect_button.setEnabled(False)
        button_layout.addWidget(self.reconnect_button)
        
        self.share_screen_button = QPushButton("📷 Share Screen (Start)")
        self.share_screen_button.clicked.connect(self.toggle_screen_share)
        button_layout.addWidget(self.share_screen_button)
        
        self.minimize_button = QPushButton("➖ Minimize to Tray")
        self.minimize_button.clicked.connect(self.hide)
        button_layout.addWidget(self.minimize_button)
        
        main_layout.addLayout(button_layout)
        
        # File transfer progress
        progress_layout = QVBoxLayout()
        self.progress_label = QLabel("No active transfers")
        self.progress_label.setStyleSheet("color: #888;")
        progress_layout.addWidget(self.progress_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        
        main_layout.addLayout(progress_layout)
        
        # Activity log
        log_label = QLabel("📋 Activity Log:")
        log_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        main_layout.addWidget(log_label)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(200)
        main_layout.addWidget(self.log_text)
        
        # Footer
        footer = QLabel("💡 This window can be minimized to system tray")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("color: #666; font-size: 11px; padding: 10px;")
        main_layout.addWidget(footer)
        
        self.setLayout(main_layout)
        
        
 # -----------------------------------------------------------------------------------------------------------------------------       
        # 3. Add these methods to StudentClient class:
    def _show_presentation(self, _):
        """Show presentation overlay (thread-safe)"""
        if self.presentation_overlay is None:
            self.log("Starting presentation mode")
            self.presentation_overlay = PresentationOverlay(parent=self)
            self.presentation_overlay.showFullScreen()
            self.signals.update_status.emit("📽️ Viewing Presentation", "yellow")

    def _hide_presentation(self):
        """Hide presentation overlay (thread-safe)"""
        if self.presentation_overlay:
            self.log("Ending presentation mode")
            self.presentation_overlay.close()
            self.presentation_overlay = None
            self.signals.update_status.emit("✅ Connected to Admin/Teacher Server", "green")

    def update_presentation_frame(self, frame_data):
        """Update presentation overlay with new frame"""
        if self.presentation_overlay:
            self.presentation_overlay.update_frame(frame_data)   
            
            
# -----------------------------------------------------------------------------------------------------------------------------
            
            
    def setup_system_tray(self):
        """Setup system tray icon"""
        try:
            self.tray_icon = QSystemTrayIcon(self)
            # You can set an icon here if you have one
            # self.tray_icon.setIcon(QIcon("icon.png"))
            
            tray_menu = QMenu()
            show_action = QAction("Show Window", self)
            show_action.triggered.connect(self.show)
            quit_action = QAction("Exit", self)
            quit_action.triggered.connect(self.quit_application)
            
            tray_menu.addAction(show_action)
            tray_menu.addSeparator()
            tray_menu.addAction(quit_action)
            
            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.activated.connect(self.tray_icon_activated)
            self.tray_icon.show()
        except Exception as e:
            self.log(f"Could not create system tray icon: {e}")

    def tray_icon_activated(self, reason):
        """Handle tray icon clicks"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()

    def log(self, message):
        """Add message to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.signals.log_message.emit(f"[{timestamp}] {message}")

    def append_log(self, message):
        """Append to log (thread-safe)"""
        self.log_text.append(message)
        # Auto-scroll to bottom
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def update_status_label(self, message, color):
        """Update status label (thread-safe)"""
        self.status_label.setText(message)
        if color == "green":
            self.status_label.setStyleSheet("background-color: #2d5016; color: #90ee90; padding: 15px; border-radius: 8px; font-weight: bold;")
        elif color == "red":
            self.status_label.setStyleSheet("background-color: #5c1919; color: #ff6b6b; padding: 15px; border-radius: 8px; font-weight: bold;")
        elif color == "yellow":
            self.status_label.setStyleSheet("background-color: #5c4f19; color: #ffd93d; padding: 15px; border-radius: 8px; font-weight: bold;")
        else:
            self.status_label.setStyleSheet("background-color: #3c3c3c; padding: 15px; border-radius: 8px;")

    def attempt_connection(self):
        """Attempt to connect to server"""
        if self.connected or not self.running:
            return
        
        self.log("Attempting to connect to server...")
        self.signals.update_status.emit("🔄 Connecting to server...", "")
        
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(10)
            self.client_socket.connect((SERVER_HOST, SERVER_PORT))
            self.client_socket.settimeout(None)
            self.connected = True
            
            self.signals.update_status.emit("✅ Connected to Admin/Teacher Server", "green")
            self.log("Successfully connected to server")
            self.reconnect_button.setEnabled(False)
            
            # Start listening thread
            threading.Thread(target=self.listen_for_commands, daemon=True).start()
            
            # Start heartbeat
            self.start_heartbeat()
            
        except Exception as e:
            self.connected = False
            self.signals.update_status.emit(f"❌ Connection failed: {str(e)}", "red")
            self.log(f"Connection failed: {e}")
            self.reconnect_button.setEnabled(True)
            
            # Schedule reconnection
            if self.running:
                self.log(f"Retrying in {RECONNECT_DELAY//1000} seconds...")
                QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)

    def manual_reconnect(self):
        """Manually trigger reconnection"""
        self.reconnect_button.setEnabled(False)
        self.disconnect_socket()
        QTimer.singleShot(500, self.attempt_connection)

    def disconnect_socket(self):
        """Safely disconnect socket"""
        self.connected = False
        self.screen_sharing = False
        self.stop_heartbeat()
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
            self.client_socket = None

    def start_heartbeat(self):
        """Start sending heartbeat to keep connection alive"""
        self.stop_heartbeat()
        self.heartbeat_timer = QTimer()
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)
        self.heartbeat_timer.start(10000)  # Every 10 seconds

    def stop_heartbeat(self):
        """Stop heartbeat timer"""
        if self.heartbeat_timer:
            self.heartbeat_timer.stop()
            self.heartbeat_timer = None

    def send_heartbeat(self):
        """Send heartbeat to server"""
        if self.connected and self.client_socket:
            try:
                self.client_socket.sendall(b"HEARTBEAT\n")
            except:
                # Connection lost
                self.disconnect_socket()
                self.signals.update_status.emit("❌ Connection lost", "red")
                if self.running:
                    QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)

    # def listen_for_commands(self):
    #     """Listen for commands from server"""
    #     self.sock.settimeout(1.0)  # Set timeout for recv
    #     buffer = b""
        
    #     while self.connected and self.running:
    #         try:
    #             data = self.client_socket.recv(BUFFER_SIZE)
    #             if not data:
    #                 self.log("Server closed connection")
    #                 break
                
    #             buffer += data
                
    #             # Process complete commands (ending with newline)
    #             while b'\n' in buffer:
    #                 line, buffer = buffer.split(b'\n', 1)
    #                 command = line.decode('utf-8', errors='ignore').strip()
                    
    #                 if not command:
    #                     continue
                    
    #                 self.log(f"Received command: {command}")
                    
    #                 # Process command in separate thread to avoid blocking
    #                 threading.Thread(
    #                     target=self.process_command,
    #                     args=(command,),
    #                     daemon=True
    #                 ).start()
                    
    #         except socket.timeout:
    #             # Timeout is normal, just continue
    #             continue
    #         except Exception as e:
    #             self.log(f"Listen error: {e}")
    #             break
        
    #     # Connection lost
    #     self.disconnect_socket()
    #     self.signals.update_status.emit("❌ Disconnected from server", "red")
    #     self.reconnect_button.setEnabled(True)
        
    #     if self.running:
    #         self.log(f"Reconnecting in {RECONNECT_DELAY//1000} seconds...")
    #         QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)

    def process_command(self, command):
        """Process received command"""
        print(f"[DEBUG] Received command: '{command}'")
        
        if command == "LOCK":
            print("[DEBUG] Lock command received")
            self.lock_screen()
        elif command == "UNLOCK":
            self.unlock_screen()
        elif command == "START_PRESENTATION":
            print("[DEBUG] Start presentation command received")
            self.presentation_signals.lock_requested.emit("")
        elif command == "STOP_PRESENTATION":
            print("[DEBUG] Stop presentation command received")
            self.presentation_signals.unlock_requested.emit()
        elif command == "REQUEST_SCREEN":
            threading.Thread(target=self.send_screen_once, daemon=True).start()
        elif command == "START_SCREEN_STREAM":
            self.start_streaming_screen()
        elif command == "STOP_SCREEN_STREAM":
            self.stop_streaming_screen()
        elif command.startswith("MESSAGE:"):
            msg = command[8:]
            self.signals.show_message.emit("Message from Admin", msg)
        elif command.startswith("SEND_FILE:"):
            filename = command.split(":", 1)[1]
            threading.Thread(target=self.receive_file, args=(filename,), daemon=True).start()
            
    def _create_lock_overlay(self, logo_path):
        """Create lock overlay on main thread"""
        print(f"[DEBUG] _create_lock_overlay() called")
        
        if getattr(self, "overlay", None) is not None:
            print(f"[DEBUG] Overlay already exists, ignoring lock request")
            return  # already locked
        
        print(f"[DEBUG] Creating new overlay on main thread")
        self.overlay = LockOverlay(
            "🔒 Locked by Administrator",
            logo_path=logo_path,
            parent=self
        )
        self.overlay.showFullScreen()
        print(f"[DEBUG] Overlay reference stored in self.overlay")


    def lock_screen(self):
        """Lock the student's screen"""
        if getattr(self, "overlay", None) is not None:
            return

        self.log("Screen locked by administrator")
        self.signals.update_status.emit("🔒 Screen is LOCKED by Admin", "red")

        # Get the full path to school_logo.png
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            script_dir = sys._MEIPASS
        else:
            # Running as script
            script_dir = os.path.dirname(os.path.abspath(__file__))
        
        logo_path = os.path.join(script_dir, "school_logo.png")
        
        print(f"[DEBUG] Script directory: {script_dir}")
        print(f"[DEBUG] Logo path: {logo_path}")
        print(f"[DEBUG] File exists: {os.path.exists(logo_path)}")
        
        # Emit signal to create overlay on main thread (thread-safe)
        self.lock_signals.lock_requested.emit(logo_path)



    def unlock_screen(self):
        """Unlock the student's screen"""
        print(f"[DEBUG] unlock_screen() called")
        print(f"[DEBUG] overlay exists: {getattr(self, 'overlay', None) is not None}")
        
        if getattr(self, "overlay", None):
            print(f"[DEBUG] Closing overlay")
            try:
                self.overlay.close()
            except Exception as e:
                print(f"[DEBUG] Error closing overlay: {e}")
            finally:
                self.overlay = None
                print(f"[DEBUG] Overlay reference cleared")
        else:
            print(f"[DEBUG] No overlay to close")
        
        # Update status - always emit this
        self.signals.update_status.emit("✅ Screen unlocked", "green")
        self.log("Screen unlocked by administrator")
        print(f"[DEBUG] Status updated to green")
    
    def display_message(self, title, message):
        """Display message box (thread-safe)"""
        QMessageBox.information(self, title, message)
        self.log(f"Message displayed: {message}")

    def receive_file(self, filename):
        """Receive file from server with destination support"""
        try:
            # Read metadata first
            meta_len_bytes = self.client_socket.recv(4)
            if len(meta_len_bytes) < 4:
                self.log("Error: Could not read metadata length")
                return
            
            meta_len = struct.unpack(">I", meta_len_bytes)[0]
            meta_json = b""
            
            while len(meta_json) < meta_len:
                chunk = self.client_socket.recv(meta_len - len(meta_json))
                if not chunk:
                    self.log("Error: Connection closed while reading metadata")
                    return
                meta_json += chunk
            
            # Parse metadata
            try:
                metadata = json.loads(meta_json.decode('utf-8'))
                destination = metadata.get("destination", "Downloads")
                safe_filename = os.path.basename(metadata.get("filename", filename))
            except:
                destination = "Downloads"
                safe_filename = os.path.basename(filename)
            
            self.signals.file_progress.emit(0, f"Receiving: {safe_filename}")
            self.log(f"Starting file transfer: {safe_filename} -> {destination}")
            
            # Resolve destination path
            filepath = self._resolve_destination_path(destination, safe_filename)
            
            if not filepath:
                self.log(f"Error: Invalid destination path: {destination}")
                self.signals.file_progress.emit(0, "Error: Invalid destination")
                return
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            # Receive file data
            total_received = 0
            with open(filepath, 'wb') as f:
                while True:
                    chunk = self.client_socket.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    
                    # Check for terminator
                    if b"<END>" in chunk:
                        end_pos = chunk.find(b"<END>")
                        if end_pos > 0:
                            f.write(chunk[:end_pos])
                            total_received += end_pos
                        break
                    
                    f.write(chunk)
                    total_received += len(chunk)
                    
                    # Update progress
                    if total_received % (BUFFER_SIZE * 10) == 0:
                        self.signals.file_progress.emit(50, f"Receiving: {total_received//1024} KB")
            
            self.signals.file_progress.emit(100, f"Completed: {safe_filename}")
            self.log(f"File received successfully: {filepath}")
            self.signals.show_message.emit("File Received", 
                                        f"File '{safe_filename}' saved to:\n{filepath}")
            
            # Hide progress after 3 seconds
            QTimer.singleShot(3000, lambda: self.signals.file_progress.emit(0, ""))
            
        except Exception as e:
            self.log(f"File receive error: {e}")
            self.signals.file_progress.emit(0, f"Error: {str(e)}")
            
    def listen_for_commands(self):
        """Listen for commands and files from server"""
        self.client_socket.settimeout(1.0)
        buffer = b""
        
        while self.connected and self.running:
            try:
                data = self.client_socket.recv(BUFFER_SIZE)
                if not data:
                    self.log("Server closed connection")
                    break
                
                buffer += data
                
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    command = line.decode('utf-8', errors='ignore').strip()
                    
                    if not command:
                        continue
                    
                    self.log(f"Received command: {command}")
                    
                    # Handle presentation frames
                    if command.upper() == "PRESENT_FRAME":
                        # Read 8-byte size
                        while len(buffer) < 8:
                            chunk = self.client_socket.recv(BUFFER_SIZE)
                            if not chunk:
                                break
                            buffer += chunk
                        
                        if len(buffer) < 8:
                            continue
                        
                        size = struct.unpack(">Q", buffer[:8])[0]
                        buffer = buffer[8:]
                        
                        # Read frame data
                        while len(buffer) < size:
                            needed = size - len(buffer)
                            chunk = self.client_socket.recv(min(BUFFER_SIZE, needed))
                            if not chunk:
                                break
                            buffer += chunk
                        
                        if len(buffer) >= size:
                            frame_data = buffer[:size]
                            buffer = buffer[size:]
                            
                            # Update presentation display
                            self.update_presentation_frame(frame_data)
                    
                    elif command.upper() == "SEND_FILE":
                        self._receive_file_from_socket(buffer)
                        buffer = b""
                    else:
                        threading.Thread(
                            target=self.process_command,
                            args=(command,),
                            daemon=True
                        ).start()
                    
            except socket.timeout:
                continue
            except Exception as e:
                self.log(f"Listen error: {e}")
                break
        
        # Connection lost
        self.disconnect_socket()
        self.signals.update_status.emit("❌ Disconnected from server", "red")
        self.reconnect_button.setEnabled(True)
        
        if self.running:
            self.log(f"Reconnecting in {RECONNECT_DELAY//1000} seconds...")
            QTimer.singleShot(RECONNECT_DELAY, self.attempt_connection)


    def _receive_file_from_socket(self, initial_buffer):
        """Receive file directly from socket with metadata"""
        try:
            buffer = initial_buffer
            
            # Read metadata length (4 bytes)
            while len(buffer) < 4:
                chunk = self.client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    self.log("Error: Connection closed while reading metadata length")
                    return
                buffer += chunk
            
            meta_len = struct.unpack(">I", buffer[:4])[0]
            buffer = buffer[4:]
            
            # Read metadata JSON
            while len(buffer) < meta_len:
                chunk = self.client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    self.log("Error: Connection closed while reading metadata")
                    return
                buffer += chunk
            
            meta_json = buffer[:meta_len]
            buffer = buffer[meta_len:]
            
            # Parse metadata
            try:
                metadata = json.loads(meta_json.decode('utf-8'))
                destination = metadata.get("destination", "Downloads")
                safe_filename = os.path.basename(metadata.get("filename", "file"))
            except Exception as e:
                self.log(f"Error parsing metadata: {e}")
                destination = "Downloads"
                safe_filename = "file"
            
            self.signals.file_progress.emit(0, f"Receiving: {safe_filename}")
            self.log(f"Starting file transfer: {safe_filename} -> {destination}")
            
            # Resolve destination path
            filepath = self._resolve_destination_path(destination, safe_filename)
            
            if not filepath:
                self.log(f"Error: Invalid destination path: {destination}")
                self.signals.file_progress.emit(0, "Error: Invalid destination")
                return
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            # Receive file data
            total_received = 0
            with open(filepath, 'wb') as f:
                while True:
                    # Need at least some data
                    if len(buffer) == 0:
                        chunk = self.client_socket.recv(BUFFER_SIZE)
                        if not chunk:
                            self.log("Error: Connection closed during file transfer")
                            break
                        buffer += chunk
                    
                    # Check for terminator
                    if b"<END>" in buffer:
                        end_pos = buffer.find(b"<END>")
                        if end_pos > 0:
                            f.write(buffer[:end_pos])
                            total_received += end_pos
                        buffer = buffer[end_pos + 5:]  # Skip past <END>
                        break
                    
                    # Write chunk
                    to_write = len(buffer)
                    f.write(buffer)
                    total_received += to_write
                    buffer = b""
                    
                    # Update progress
                    if total_received % (BUFFER_SIZE * 5) == 0:
                        self.signals.file_progress.emit(50, f"Receiving: {total_received//1024} KB")
            
            self.signals.file_progress.emit(100, f"Completed: {safe_filename}")
            self.log(f"File received successfully: {filepath} ({total_received} bytes)")
            self.signals.show_message.emit("File Received", 
                                        f"File '{safe_filename}' saved to:\n{filepath}")
            
            # Hide progress after 3 seconds
            QTimer.singleShot(3000, lambda: self.signals.file_progress.emit(0, ""))
            
        except Exception as e:
            self.log(f"File receive error: {e}")
            import traceback
            self.log(f"Traceback: {traceback.format_exc()}")
            self.signals.file_progress.emit(0, f"Error: {str(e)}")


    def _resolve_destination_path(self, destination, filename):
        """Resolve destination path, handling special keywords and custom paths"""
        try:
            home = os.path.expanduser("~")
            
            # Handle common destinations
            if destination.lower() == "downloads":
                base_path = os.path.join(home, "Downloads")
            elif destination.lower() == "desktop":
                base_path = os.path.join(home, "Desktop")
            elif destination.lower() == "documents":
                base_path = os.path.join(home, "Documents")
            else:
                # Treat as custom path
                base_path = destination
            
            # Validate and make absolute path
            base_path = os.path.abspath(base_path)
            
            filepath = os.path.join(base_path, filename)
            filepath = os.path.abspath(filepath)
            
            # Ensure unique filename if it exists
            if os.path.exists(filepath):
                base, ext = os.path.splitext(filepath)
                counter = 1
                while os.path.exists(f"{base}_{counter}{ext}"):
                    counter += 1
                filepath = f"{base}_{counter}{ext}"
                self.log(f"File already exists, saving as: {os.path.basename(filepath)}")
            
            return filepath
            
        except Exception as e:
            self.log(f"Error resolving destination path: {e}")
            return None

    def update_file_progress(self, percentage, status):
        """Update file transfer progress (thread-safe)"""
        if percentage > 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(percentage)
            self.progress_label.setText(status)
        else:
            self.progress_bar.setVisible(False)
            self.progress_label.setText(status if status else "No active transfers")

    def send_screen_once(self):
        """Send a single screenshot"""
        if not self.connected:
            self.log("Cannot send screenshot: not connected")
            return
        
        try:
            self.log("Capturing screenshot...")
            screenshot = ImageGrab.grab()
            
            # Convert to JPEG with compression
            buffer = io.BytesIO()
            screenshot.save(buffer, format='JPEG', quality=SCREENSHOT_QUALITY, optimize=True)
            data = buffer.getvalue()
            
            # Send with protocol: "FRAME\n" + 8-byte size + data
            header = b"FRAME\n"
            size = struct.pack(">Q", len(data))
            
            self.client_socket.sendall(header + size + data)
            
            self.log(f"Screenshot sent ({len(data)//1024} KB)")
            self.signals.update_status.emit("📸 Screenshot sent", "green")
            
            # Reset status after 2 seconds
            QTimer.singleShot(2000, lambda: self.signals.update_status.emit(
                "✅ Connected to Admin/Teacher Server", "green"))
            
        except Exception as e:
            self.log(f"Screenshot send error: {e}")
            self.disconnect_socket()
            

            
    def toggle_screen_share(self):
        """Toggle continuous screen sharing on/off"""
        if getattr(self, 'sharing_active', False):
            self.stop_screen_share()
            self.share_screen_button.setText("📷 Share Screen (Start)")
        else:
            self.start_screen_share()
            self.share_screen_button.setText("🛑 Stop Screen Share")

    def start_screen_share(self):
        """Optimized screen sharing"""
        if getattr(self, 'sharing_active', False):
            return
        
        self.sharing_active = True
        
        def share_loop():
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while self.sharing_active:
                    try:
                        # Capture
                        frame = np.array(sct.grab(monitor))
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                        
                        # Scale down (50%)
                        h, w = frame.shape[:2]
                        frame = cv2.resize(frame, (w//2, h//2), interpolation=cv2.INTER_LINEAR)
                        
                        # Compress (quality 40)
                        _, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 40])
                        data = encoded.tobytes()
                        
                        # Send with header
                        header = b"FRAME\n" + struct.pack(">Q", len(data))
                        self.sock.sendall(header + data)
                        
                        # 30 FPS
                        time.sleep(0.033)
                        
                    except:
                        break
        
        threading.Thread(target=share_loop, daemon=True).start()

    def stop_screen_share(self):
        """Stop continuous screen sharing"""
        if getattr(self, 'sharing_active', False):
            self.sharing_active = False
            self.log("Stopping screen sharing...")
        else:
            self.log("Screen sharing is not active")


    def start_streaming_screen(self):
        """Start streaming screen"""
        if not self.screen_sharing:
            self.screen_sharing = True
            self.log("Started screen streaming")
            self.signals.update_status.emit("📹 Streaming screen...", "yellow")
            threading.Thread(target=self.stream_screen, daemon=True).start()

    def stop_streaming_screen(self):
        """Stop streaming screen"""
        if self.screen_sharing:
            self.screen_sharing = False
            self.log("Stopped screen streaming")
            self.signals.update_status.emit("✅ Connected to Admin/Teacher Server", "green")

    def stream_screen(self):
        """Continuously capture and send the screen in real-time"""
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            fps = 20  # ⬆️ increase FPS slightly
            jpeg_quality = 50  # ⬇️ lower quality for faster transfer
            
            try:
                while self.screen_sharing and self.connected:
                    frame_start = time.time()

                    # Capture fast frame
                    img = np.array(sct.grab(monitor))
                    frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    
                    # Resize for speed (optional)
                    frame = cv2.resize(frame, (1280, 720))  # 720p stream

                    # Compress to JPEG (small, fast)
                    ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
                    if not ret:
                        continue

                    # Send frame
                    data = buffer.tobytes()
                    size = struct.pack(">Q", len(data))
                    self.client_socket.sendall(b"FRAME\n" + size + data)

                    # Maintain FPS
                    elapsed = time.time() - frame_start
                    sleep_time = max(0, 1/fps - elapsed)
                    time.sleep(sleep_time)

            except Exception as e:
                self.log(f"Streaming error: {e}")
                self.screen_sharing = False

    def quit_application(self):
        """Quit the application"""
        self.running = False
        self.disconnect_socket()
        QApplication.quit()

    def closeEvent(self, event):
        """Handle close event"""
        if self.locked:
            event.ignore()
            return
        
        # Minimize to tray instead of closing
        event.ignore()
        self.hide()
        if hasattr(self, 'tray_icon'):
            self.tray_icon.showMessage(
                "Student Client",
                "Application minimized to system tray",
                QSystemTrayIcon.Information,
                2000
            )


# ==============================
# Main Entry Point
# ==============================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # Keep running in tray
    
    window = StudentClient()
    window.show()
    
    sys.exit(app.exec_())