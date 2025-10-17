import sys
import os
import socket
import threading
import struct
import time
import io
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, 
                             QPushButton, QMessageBox, QTextEdit, QProgressBar,
                             QHBoxLayout, QSystemTrayIcon, QMenu, QAction)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QIcon, QFont
from PIL import ImageGrab

# ==============================
# Configuration
# ==============================
SERVER_HOST = '192.168.68.106'  # Change this to admin/teacher IP
SERVER_PORT = 5001
BUFFER_SIZE = 65536
RECONNECT_DELAY = 5000  # milliseconds
SCREENSHOT_QUALITY = 85  # JPEG quality (1-100)
STREAM_FPS = 10  # Frames per second for streaming

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
        
        self.setup_ui()
        self.setup_system_tray()
        
        # Start connection attempt
        self.log("Application started")
        QTimer.singleShot(500, self.attempt_connection)

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
        
        self.share_screen_button = QPushButton("📷 Share Screen (Test)")
        self.share_screen_button.clicked.connect(self.send_screen_once)
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

    def listen_for_commands(self):
        """Listen for commands from server"""
        self.sock.settimeout(1.0)  # Set timeout for recv
        buffer = b""
        
        while self.connected and self.running:
            try:
                data = self.client_socket.recv(BUFFER_SIZE)
                if not data:
                    self.log("Server closed connection")
                    break
                
                buffer += data
                
                # Process complete commands (ending with newline)
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    command = line.decode('utf-8', errors='ignore').strip()
                    
                    if not command:
                        continue
                    
                    self.log(f"Received command: {command}")
                    
                    # Process command in separate thread to avoid blocking
                    threading.Thread(
                        target=self.process_command,
                        args=(command,),
                        daemon=True
                    ).start()
                    
            except socket.timeout:
                # Timeout is normal, just continue
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

    def process_command(self, command):
        """Process received command"""
        if command == "LOCK":
            self.lock_screen()
        elif command == "UNLOCK":
            self.unlock_screen()
        elif command == "REQUEST_SCREEN":
            threading.Thread(target=self.send_screen_once, daemon=True).start()
        elif command == "START_SCREEN_STREAM":
            self.start_streaming_screen()
        elif command == "STOP_SCREEN_STREAM":
            self.stop_streaming_screen()
        elif command.startswith("MESSAGE:"):
            msg = command[8:]  # Remove "MESSAGE:" prefix
            self.signals.show_message.emit("Message from Admin", msg)
        elif command.startswith("SEND_FILE:"):
            filename = command.split(":", 1)[1]
            threading.Thread(target=self.receive_file, args=(filename,), daemon=True).start()

    def lock_screen(self):
        """Lock the screen"""
        self.locked = True
        self.signals.update_status.emit("🔒 Screen is LOCKED by Admin", "red")
        self.log("Screen locked by administrator")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.showFullScreen()

    def unlock_screen(self):
        """Unlock the screen"""
        self.locked = False
        self.signals.update_status.emit("✅ Screen unlocked", "green")
        self.log("Screen unlocked")
        self.setWindowFlags(Qt.Window)
        self.showNormal()

    def display_message(self, title, message):
        """Display message box (thread-safe)"""
        QMessageBox.information(self, title, message)
        self.log(f"Message displayed: {message}")

    def receive_file(self, filename):
        """Receive file from server"""
        try:
            self.signals.file_progress.emit(0, f"Receiving: {filename}")
            self.log(f"Starting file transfer: {filename}")
            
            safe_filename = os.path.basename(filename)
            downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
            filepath = os.path.join(downloads_path, safe_filename)
            
            # Ensure unique filename
            base, ext = os.path.splitext(filepath)
            counter = 1
            while os.path.exists(filepath):
                filepath = f"{base}_{counter}{ext}"
                counter += 1
            
            total_received = 0
            with open(filepath, 'wb') as f:
                while True:
                    chunk = self.client_socket.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    
                    # Check for terminator
                    if b"<END>" in chunk:
                        # Write data before terminator
                        end_pos = chunk.find(b"<END>")
                        if end_pos > 0:
                            f.write(chunk[:end_pos])
                            total_received += end_pos
                        break
                    
                    f.write(chunk)
                    total_received += len(chunk)
                    
                    # Update progress (estimate)
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
        """Stream screen continuously"""
        frame_delay = 1.0 / STREAM_FPS
        
        try:
            while self.screen_sharing and self.connected:
                start_time = time.time()
                
                screenshot = ImageGrab.grab()
                buffer = io.BytesIO()
                screenshot.save(buffer, format='JPEG', quality=SCREENSHOT_QUALITY, optimize=True)
                data = buffer.getvalue()
                
                # Send with protocol
                header = b"FRAME\n"
                size = struct.pack(">Q", len(data))
                self.client_socket.sendall(header + size + data)
                
                # Maintain frame rate
                elapsed = time.time() - start_time
                sleep_time = max(0, frame_delay - elapsed)
                time.sleep(sleep_time)
                
        except Exception as e:
            self.log(f"Streaming error: {e}")
            self.screen_sharing = False
            self.disconnect_socket()

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