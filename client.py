"""
Lab Manager - Student Client - FIXED VERSION
Changes:
- FIXED: Connection works without admin login
- FIXED: Admin login only required for unlocking screen
- FIXED: Better fullscreen UI layout with proper spacing
- NEW: Shutdown/Restart PC functionality
- NEW: Admin password for unlocking
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
import webbrowser
import urllib.parse
from urllib.parse import urlparse

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
    QSystemTrayIcon, QMenu, QAction, QInputDialog, QLineEdit, QProgressDialog
)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QPixmap, QIcon, QFont, QImage, QPainter, QColor
from PyQt5.QtCore import QByteArray

# Configuration - Default values, can be changed from tray
DEFAULT_SERVER_HOST = '192.168.68.106'
DEFAULT_SERVER_PORT = 5001
BUFFER_SIZE = 65536
RECONNECT_DELAY = 5000
SCREENSHOT_QUALITY = 60
CHUNK_SIZE = 8 * 1024 * 1024
SOCKET_SEND_BUFFER = 32 * 1024 * 1024
SOCKET_RECV_BUFFER = 32 * 1024 * 1024
BATCH_ACK_SIZE = 20
RESTORE_TEMP_DIR = os.path.join(os.path.expanduser("~"), "lab_restore_temp")
RESUME_METADATA_DIR = os.path.join(os.path.expanduser("~"), "lab_transfer_cache_client")
os.makedirs(RESUME_METADATA_DIR, exist_ok=True)
os.makedirs(RESTORE_TEMP_DIR, exist_ok=True)

# Config file
CONFIG_FILE = os.path.join(RESUME_METADATA_DIR, "client_config.json")


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
    show_progress_dialog = pyqtSignal(str, int)
    update_progress_dialog = pyqtSignal(int, str)
    close_progress_dialog = pyqtSignal()


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
    def __init__(self, message="🔒 Locked by Administrator", logo_path=None, parent=None, admin_password="admin123"):
        super().__init__()
        self.parent_window = parent
        self.unlocked = False
        self.logo_pixmap = None
        self.admin_password = admin_password
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)
        
        self.setStyleSheet("QWidget { background-color: #000000; }")
        
        if logo_path and os.path.exists(logo_path):
            try:
                self.logo_pixmap = QPixmap(logo_path)
                if self.logo_pixmap.isNull():
                    self.logo_pixmap = None
            except:
                self.logo_pixmap = None
        
        # FIXED: Better layout for fullscreen
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(50)
        layout.setContentsMargins(100, 100, 100, 100)
        
        # Main message - FIXED: Better sizing
        label = QLabel(message)
        label.setStyleSheet("""
            color: white; 
            font-size: 48px;
            font-weight: bold; 
            margin: 40px;
            background-color: rgba(0, 0, 0, 200);
            padding: 40px 60px;
            border-radius: 15px;
        """)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setMinimumHeight(150)
        layout.addWidget(label)
        
        # FIXED: Better instruction display
        instruction = QLabel("Press 'U' key and enter admin password to unlock")
        instruction.setStyleSheet("""
            color: #cccccc; 
            font-size: 20px;
            background-color: rgba(0, 0, 0, 200);
            padding: 20px 40px;
            border-radius: 10px;
            margin-top: 20px;
        """)
        instruction.setAlignment(Qt.AlignCenter)
        instruction.setWordWrap(True)
        instruction.setMinimumHeight(80)
        layout.addWidget(instruction)
        
        layout.addStretch()
        self.setLayout(layout)
    
    def paintEvent(self, event):
        painter = QPainter(self)
        
        if self.logo_pixmap and not self.logo_pixmap.isNull():
            painter.drawPixmap(self.rect(), self.logo_pixmap)
        else:
            painter.fillRect(self.rect(), QColor(0, 0, 0))
        
        painter.end()
    
    def showFullScreen(self):
        super().showFullScreen()
        self.setFocus()
        self.raise_()
        self.activateWindow()
    
    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        
        # FIXED: Use admin password for unlocking
        if event.key() == Qt.Key_U:
            password, ok = QInputDialog.getText(
                self, 
                "Admin Unlock", 
                "Enter admin password to unlock:",
                QLineEdit.Password
            )
            if ok and password == self.admin_password:
                self.unlocked = True
                self.close()
            elif ok:
                QMessageBox.warning(self, "Incorrect", "Incorrect admin password")
        else:
            event.ignore()
    
    def mousePressEvent(self, event):
        pass
    
    def closeEvent(self, event):
        event.accept()


class StudentClient(QWidget):
    def __init__(self):
        super().__init__()
        
        self.load_config()
        
        self.setWindowTitle("Student Client - Enhanced")
        self.setGeometry(100, 100, 900, 650)
        self.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
                color: #e0e0e0;
                font-family: 'Segoe UI', Arial, sans-serif;
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
            QPushButton:disabled {
                background-color: #2a2a2a;
                color: #666;
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
        
        self.client_socket = None
        self.connected = False
        self.screen_sharing = False
        self.locked = False
        self.running = True
        self.sharing_active = False
        self.connecting = False
        self.reconnect_scheduled = False
        
        self.progress_dialog = None
        
        self.custom_pc_name = self.load_custom_pc_name()
        self.setWindowTitle(f"Student Client - {self.custom_pc_name}")
        
        self.signals = SignalHandler()
        self.signals.update_status.connect(self.update_status_label)
        self.signals.show_message.connect(self.display_message)
        self.signals.file_progress.connect(self.update_file_progress)
        self.signals.log_message.connect(self.append_log)
        self.signals.show_progress_dialog.connect(self._show_progress_dialog)
        self.signals.update_progress_dialog.connect(self._update_progress_dialog)
        self.signals.close_progress_dialog.connect(self._close_progress_dialog)
        
        self.lock_signals = LockSignals()
        self.lock_signals.lock_requested.connect(self._create_lock_overlay)
        self.lock_signals.unlock_requested.connect(self.unlock_screen)
        
        self.overlay = None
        self.presentation_overlay = None
        self.presentation_signals = LockSignals()
        self.presentation_signals.lock_requested.connect(self._show_presentation)
        self.presentation_signals.unlock_requested.connect(self._hide_presentation)
        
        self.setup_ui()
        self.setup_system_tray()
        
        self.log("Application started")
        self.log(f"Target server: {self.server_host}:{self.server_port}")
        
        # FIXED: Auto-connect regardless of admin login
        QTimer.singleShot(500, self.attempt_connection)
        
        self.restrictions = {"keywords": [], "sites": []}
        self.restriction_enabled = True
        
        self.update_restriction_indicator()
    
    def load_config(self):
        """Load configuration from file"""
        default_config = {
            'server_host': DEFAULT_SERVER_HOST,
            'server_port': DEFAULT_SERVER_PORT,
            'admin_password': 'admin123'
        }
        
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    self.config = json.load(f)
                    for key, value in default_config.items():
                        if key not in self.config:
                            self.config[key] = value
            else:
                self.config = default_config
                self.save_config()
        except:
            self.config = default_config
        
        self.server_host = self.config.get('server_host', DEFAULT_SERVER_HOST)
        self.server_port = self.config.get('server_port', DEFAULT_SERVER_PORT)
    
    def save_config(self):
        """Save configuration to file"""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to save config: {e}")
            return False
    
    def configure_server_ip(self):
        """Configure server IP address"""
        current_ip = f"{self.server_host}:{self.server_port}"
        
        new_ip, ok = QInputDialog.getText(
            self,
            "Configure Server",
            "Enter server address (IP:PORT):",
            QLineEdit.Normal,
            current_ip
        )
        
        if ok and new_ip:
            try:
                if ':' in new_ip:
                    host, port = new_ip.split(':', 1)
                    port = int(port)
                else:
                    host = new_ip
                    port = DEFAULT_SERVER_PORT
                
                self.server_host = host
                self.server_port = port
                self.config['server_host'] = host
                self.config['server_port'] = port
                self.save_config()
                
                self.log(f"✅ Server configured: {host}:{port}")
                QMessageBox.information(
                    self, "Server Configured",
                    f"Server address updated to:\n{host}:{port}\n\n"
                    "Reconnecting..."
                )
                
                if self.connected:
                    self.disconnect_socket()
                    QTimer.singleShot(500, self.attempt_connection)
                
            except Exception as e:
                QMessageBox.warning(self, "Invalid Address", f"Invalid server address format!\n\nUse: IP:PORT\nExample: 192.168.1.100:5001")
                self.log(f"❌ Invalid server address: {e}")
    
    def change_admin_password(self):
        """Change admin password for unlocking"""
        old_password, ok = QInputDialog.getText(
            self,
            "Change Admin Password",
            "Enter current admin password:",
            QLineEdit.Password
        )
        
        if not ok:
            return
        
        if old_password != self.config.get('admin_password', 'admin123'):
            QMessageBox.warning(self, "Incorrect", "Incorrect current password!")
            return
        
        new_password, ok = QInputDialog.getText(
            self,
            "Change Admin Password",
            "Enter new admin password:",
            QLineEdit.Password
        )
        
        if ok and new_password:
            confirm_password, ok = QInputDialog.getText(
                self,
                "Change Admin Password",
                "Confirm new admin password:",
                QLineEdit.Password
            )
            
            if ok and confirm_password == new_password:
                self.config['admin_password'] = new_password
                self.save_config()
                QMessageBox.information(self, "Success", "Admin password changed successfully!")
                self.log("🔑 Admin password changed")
            elif ok:
                QMessageBox.warning(self, "Mismatch", "Passwords don't match!")
    
    def is_url_blocked(self, url):
        if not self.restriction_enabled:
            return False
        
        try:
            parsed = urlparse(url.lower())
            domain = parsed.netloc or parsed.path
            
            if domain.startswith('www.'):
                domain = domain[4:]
            
            for blocked_site in self.restrictions.get('sites', []):
                if blocked_site.lower() in domain:
                    return True
            
            full_url = url.lower()
            for keyword in self.restrictions.get('keywords', []):
                if keyword.lower() in full_url:
                    return True
            
            return False
        except:
            return False
    
    def is_search_blocked(self, search_query):
        if not self.restriction_enabled:
            return False
        
        query_lower = search_query.lower()
        
        for keyword in self.restrictions.get('keywords', []):
            if keyword.lower() in query_lower:
                return True
        
        return False
    
    def show_blocked_message(self, reason="content"):
        if reason == "keyword":
            title = "🚫 Search Blocked"
            message = ("This search contains restricted keywords.\n\n"
                      "Please contact your teacher if you believe this is an error.")
        elif reason == "site":
            title = "🚫 Website Blocked"
            message = ("This website has been blocked by your administrator.\n\n"
                      "Please contact your teacher if you need access.")
        else:
            title = "🚫 Content Blocked"
            message = ("This content has been restricted by your administrator.\n\n"
                      "Please contact your teacher for assistance.")
        
        self.signals.show_message.emit(title, message)
        self.log(f"🚫 Blocked: {reason}")
    
    def intercept_browser_request(self, url):
        if self.is_url_blocked(url):
            self.show_blocked_message("site")
            return False
        return True
    
    def update_restriction_indicator(self):
        keyword_count = len(self.restrictions.get('keywords', []))
        site_count = len(self.restrictions.get('sites', []))
        
        if keyword_count == 0 and site_count == 0:
            self.restriction_indicator.setText("🔓 No restrictions active")
            self.restriction_indicator.setStyleSheet(
                "background-color: #2d5016; color: #90ee90; "
                "padding: 8px; border-radius: 5px; font-size: 11px;"
            )
        else:
            self.restriction_indicator.setText(
                f"🚫 Content restrictions active: {keyword_count} keywords, {site_count} sites blocked"
            )
            self.restriction_indicator.setStyleSheet(
                "background-color: #5c1919; color: #ff6b6b; "
                "padding: 8px; border-radius: 5px; font-size: 11px; font-weight: bold;"
            )
    
    def apply_restrictions(self):
        try:
            import platform
            if platform.system() != "Windows":
                self.log("⚠️ Website blocking only supported on Windows")
                return
            
            hosts_path = r"C:\Windows\System32\drivers\etc\hosts"
            
            try:
                with open(hosts_path, 'r') as f:
                    hosts_content = f.readlines()
            except PermissionError:
                self.log("❌ Cannot modify hosts file - Administrator rights required")
                self.log("💡 Please run this program as Administrator to enable website blocking")
                return
            except Exception as e:
                self.log(f"❌ Error reading hosts file: {e}")
                return
            
            new_hosts = []
            skip_next = False
            for line in hosts_content:
                if "# LAB_RESTRICTION" in line:
                    continue
                if not skip_next and "# LAB_RESTRICTION" not in line:
                    new_hosts.append(line)
            
            if self.restrictions.get('sites'):
                new_hosts.append("\n# LAB_RESTRICTION - START\n")
                for site in self.restrictions.get('sites', []):
                    site_clean = site.lower().strip()
                    if site_clean:
                        new_hosts.append(f"127.0.0.1 {site_clean} # LAB_RESTRICTION\n")
                        new_hosts.append(f"127.0.0.1 www.{site_clean} # LAB_RESTRICTION\n")
                new_hosts.append("# LAB_RESTRICTION - END\n")
            
            try:
                with open(hosts_path, 'w') as f:
                    f.writelines(new_hosts)
                
                import subprocess
                try:
                    subprocess.run(['ipconfig', '/flushdns'], 
                                 capture_output=True, 
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                    self.log(f"✅ Website blocking applied: {len(self.restrictions.get('sites', []))} sites blocked")
                except:
                    self.log(f"✅ Hosts file updated (DNS cache not flushed)")
                    
            except PermissionError:
                self.log("❌ Cannot write to hosts file - Administrator rights required")
                self.log("💡 Please run this program as Administrator to enable website blocking")
            except Exception as e:
                self.log(f"❌ Error writing hosts file: {e}")
                
        except Exception as e:
            self.log(f"❌ Error applying restrictions: {e}")
            import traceback
            self.log(traceback.format_exc())
    
    def remove_restrictions(self):
        try:
            import platform
            if platform.system() != "Windows":
                return
            
            hosts_path = r"C:\Windows\System32\drivers\etc\hosts"
            
            try:
                with open(hosts_path, 'r') as f:
                    hosts_content = f.readlines()
            except:
                return
            
            new_hosts = [line for line in hosts_content if "# LAB_RESTRICTION" not in line]
            
            try:
                with open(hosts_path, 'w') as f:
                    f.writelines(new_hosts)
                
                import subprocess
                try:
                    subprocess.run(['ipconfig', '/flushdns'], 
                                 capture_output=True, 
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                except:
                    pass
                    
            except:
                pass
                
        except:
            pass
    
    def setup_ui(self):
        main_layout = QVBoxLayout()
        
        header = QLabel("📚 Student Client")
        header.setFont(QFont("Segoe UI", 18, QFont.Bold))
        header.setAlignment(Qt.AlignCenter)
        header.setStyleSheet("color: #0078d4; padding: 15px;")
        main_layout.addWidget(header)
        
        self.status_label = QLabel("🔄 Connecting to server...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 12))
        self.status_label.setStyleSheet("background-color: #3c3c3c; padding: 15px; border-radius: 8px;")
        main_layout.addWidget(self.status_label)
        
        self.connection_info = QLabel(f"Server: {self.server_host}:{self.server_port}")
        self.connection_info.setAlignment(Qt.AlignCenter)
        self.connection_info.setStyleSheet("color: #888; padding: 5px;")
        main_layout.addWidget(self.connection_info)
        
        button_layout = QHBoxLayout()
        
        self.test_button = QPushButton("🔍 Test Connection")
        self.test_button.clicked.connect(self.test_connection)
        button_layout.addWidget(self.test_button)
        
        self.reconnect_button = QPushButton("🔄 Reconnect Now")
        self.reconnect_button.clicked.connect(self.manual_reconnect)
        button_layout.addWidget(self.reconnect_button)
        
        self.share_screen_button = QPushButton("📷 Share Screen")
        self.share_screen_button.clicked.connect(self.toggle_screen_share)
        button_layout.addWidget(self.share_screen_button)
        
        self.change_name_button = QPushButton("✏️ Change PC Name")
        self.change_name_button.clicked.connect(self.change_pc_name)
        button_layout.addWidget(self.change_name_button)
        
        self.minimize_button = QPushButton("➖ Minimize to Tray")
        self.minimize_button.clicked.connect(self.hide)
        button_layout.addWidget(self.minimize_button)
        
        main_layout.addLayout(button_layout)
        
        self.restriction_indicator = QLabel("🔓 No restrictions active")
        self.restriction_indicator.setAlignment(Qt.AlignCenter)
        self.restriction_indicator.setStyleSheet(
            "background-color: #2d5016; color: #90ee90; "
            "padding: 8px; border-radius: 5px; font-size: 11px;"
        )
        main_layout.addWidget(self.restriction_indicator)
        
        progress_layout = QVBoxLayout()
        self.progress_label = QLabel("No active transfers")
        self.progress_label.setStyleSheet("color: #888;")
        progress_layout.addWidget(self.progress_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        
        main_layout.addLayout(progress_layout)
        
        log_label = QLabel("📋 Activity Log:")
        log_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        main_layout.addWidget(log_label)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(200)
        main_layout.addWidget(self.log_text)
        
        footer = QLabel("💡 Auto-reconnects every 5 seconds • Minimizes to system tray")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("color: #666; font-size: 11px; padding: 10px;")
        main_layout.addWidget(footer)
        
        self.setLayout(main_layout)
    
    def setup_system_tray(self):
        try:
            pixmap = QPixmap(32, 32)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setBrush(Qt.blue)
            painter.drawEllipse(4, 4, 24, 24)
            painter.end()
            
            self.tray_icon = QSystemTrayIcon(self)
            self.tray_icon.setIcon(QIcon(pixmap))
            self.tray_icon.setToolTip("Student Client")
            
            self.update_tray_menu()
            
            self.tray_icon.activated.connect(self.tray_icon_activated)
            self.tray_icon.show()
        except:
            pass
    
    def update_tray_menu(self):
        """Update system tray menu"""
        tray_menu = QMenu()
        
        show_action = QAction("Show Window", self)
        show_action.triggered.connect(self.show)
        tray_menu.addAction(show_action)
        
        tray_menu.addSeparator()
        
        config_action = QAction("⚙️ Configure Server IP", self)
        config_action.triggered.connect(self.configure_server_ip)
        tray_menu.addAction(config_action)
        
        password_action = QAction("🔑 Change Admin Password", self)
        password_action.triggered.connect(self.change_admin_password)
        tray_menu.addAction(password_action)
        
        change_name_action = QAction("✏️ Change PC Name", self)
        change_name_action.triggered.connect(self.change_pc_name)
        tray_menu.addAction(change_name_action)
        
        tray_menu.addSeparator()
        
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
    
    def tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()
    
    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.signals.log_message.emit(f"[{timestamp}] {message}")
    
    def append_log(self, message):
        if message == "__RECONNECT_TRIGGER__":
            QTimer.singleShot(0, self.attempt_connection)
            return
        
        self.log_text.append(message)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def update_status_label(self, message, color):
        self.status_label.setText(message)
        if color == "green":
            self.status_label.setStyleSheet("background-color: #2d5016; color: #90ee90; padding: 15px; border-radius: 8px;")
        elif color == "red":
            self.status_label.setStyleSheet("background-color: #5c1919; color: #ff6b6b; padding: 15px; border-radius: 8px;")
        elif color == "yellow":
            self.status_label.setStyleSheet("background-color: #5c4f19; color: #ffd93d; padding: 15px; border-radius: 8px;")
        else:
            self.status_label.setStyleSheet("background-color: #3c3c3c; padding: 15px; border-radius: 8px;")
    
    def _show_progress_dialog(self, title, max_value):
        if self.progress_dialog is not None:
            self.progress_dialog.close()
        
        self.progress_dialog = QProgressDialog(title, "Cancel", 0, max_value, self)
        self.progress_dialog.setWindowTitle("Operation in Progress")
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.show()
        QApplication.processEvents()
    
    def _update_progress_dialog(self, value, label):
        if self.progress_dialog is not None:
            self.progress_dialog.setValue(value)
            if label:
                self.progress_dialog.setLabelText(label)
            QApplication.processEvents()
    
    def _close_progress_dialog(self):
        if self.progress_dialog is not None:
            self.progress_dialog.close()
            self.progress_dialog = None
    
    def schedule_reconnect(self):
        if not self.running or self.connected or self.reconnect_scheduled:
            return
        
        self.reconnect_scheduled = True
        self.log(f"Next reconnect attempt in {RECONNECT_DELAY//1000} seconds...")
        QTimer.singleShot(RECONNECT_DELAY, self._do_scheduled_reconnect)
    
    def _do_scheduled_reconnect(self):
        self.reconnect_scheduled = False
        if not self.connected and self.running:
            self.attempt_connection()
    
    def attempt_connection(self):
        if self.connecting or self.connected:
            return
        
        if not self.running:
            return
        
        self.connecting = True
        self.reconnect_button.setEnabled(False)
        
        self.log("Attempting to connect to server...")
        self.signals.update_status.emit("🔄 Connecting to server...", "")
        
        def connect_thread():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                
                sock.connect((self.server_host, self.server_port))
                
                # FIXED: Configure socket buffers for better reliability
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
                except:
                    pass
                sock.settimeout(None)
                
                self.client_socket = sock
                self.connected = True
                self.connecting = False
                
                self.signals.update_status.emit("✅ Connected to Admin/Teacher Server", "green")
                self.log("✅ Successfully connected to server")
                QTimer.singleShot(0, lambda: self.reconnect_button.setEnabled(False))
                
                QTimer.singleShot(100, self.send_client_info)
                
                threading.Thread(target=self.listen_for_commands, daemon=True).start()
                QTimer.singleShot(0, self.start_heartbeat)
            
            except (socket.timeout, TimeoutError):
                self.connecting = False
                self.connected = False
                self.signals.update_status.emit(f"❌ Connection timeout", "red")
                self.log(f"Connection timeout - Server may be offline")
                QTimer.singleShot(0, lambda: self.reconnect_button.setEnabled(True))
                QTimer.singleShot(0, self.schedule_reconnect)
            
            except ConnectionRefusedError:
                self.connecting = False
                self.connected = False
                self.signals.update_status.emit(f"❌ Connection refused", "red")
                self.log(f"Connection refused - Admin application not running")
                QTimer.singleShot(0, lambda: self.reconnect_button.setEnabled(True))
                QTimer.singleShot(0, self.schedule_reconnect)
            
            except Exception as e:
                self.connecting = False
                self.connected = False
                error_msg = str(e)
                self.signals.update_status.emit(f"❌ Connection failed", "red")
                self.log(f"Connection failed: {error_msg}")
                self.log(f"🔍 Troubleshooting:")
                self.log(f"   1. Is admin server running?")
                self.log(f"   2. Check IP: {self.server_host}:{self.server_port}")
                self.log(f"   3. Try pinging admin server")
                self.log(f"   4. Check firewall on both machines")
                self.log(f"🔍 Troubleshooting:")
                self.log(f"   1. Is admin server running?")
                self.log(f"   2. Check IP: {self.server_host}:{self.server_port}")
                self.log(f"   3. Try pinging admin server")
                self.log(f"   4. Check firewall on both machines")
                QTimer.singleShot(0, lambda: self.reconnect_button.setEnabled(True))
                QTimer.singleShot(0, self.schedule_reconnect)
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def test_connection(self):
        """Test connection to server without actually connecting"""
        self.log("🔍 Testing connection to server...")
        
        def test_thread():
            try:
                test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                test_sock.settimeout(3)
                
                self.log(f"📡 Attempting to reach {self.server_host}:{self.server_port}")
                test_sock.connect((self.server_host, self.server_port))
                test_sock.close()
                
                self.log("✅ Connection test SUCCESS!")
                self.log("💡 Server is reachable. If still can't connect, try manual reconnect.")
                self.signals.show_message.emit(
                    "Connection Test Successful", 
                    f"Server {self.server_host}:{self.server_port} is reachable!\n\n"
                    f"Click 'Reconnect Now' to establish connection."
                )
            except socket.timeout:
                self.log("❌ Connection test FAILED: Timeout")
                self.log("💡 Server not responding. Check if admin is running.")
                self.signals.show_message.emit(
                    "Connection Test Failed",
                    f"Cannot reach {self.server_host}:{self.server_port}\n\n"
                    f"Possible issues:\n"
                    f"• Admin server not running\n"
                    f"• Wrong IP address\n"
                    f"• Network issues"
                )
            except ConnectionRefusedError:
                self.log("❌ Connection test FAILED: Connection refused")
                self.log("💡 Port is closed. Make sure admin is running on port 5001.")
                self.signals.show_message.emit(
                    "Connection Refused",
                    f"Server at {self.server_host}:{self.server_port} refused connection\n\n"
                    f"Make sure admin server is running!"
                )
            except Exception as e:
                self.log(f"❌ Connection test FAILED: {e}")
                self.signals.show_message.emit(
                    "Connection Test Failed",
                    f"Error: {e}\n\nCheck IP address and network settings."
                )
        
        threading.Thread(target=test_thread, daemon=True).start()
    
    def manual_reconnect(self):
        self.log("📍 Manual reconnect requested")
        self.reconnect_button.setEnabled(False)
        self.reconnect_scheduled = False
        self.disconnect_socket()
        QTimer.singleShot(500, self.attempt_connection)
    
    def disconnect_socket(self):
        self.connected = False
        self.connecting = False
        self.screen_sharing = False
        self.sharing_active = False
        self.stop_heartbeat()
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
            self.client_socket = None
    
    def start_heartbeat(self):
        self.stop_heartbeat()
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)
        self.heartbeat_timer.start(10000)
    
    def stop_heartbeat(self):
        if hasattr(self, 'heartbeat_timer') and self.heartbeat_timer:
            try:
                self.heartbeat_timer.stop()
                self.heartbeat_timer.deleteLater()
            except:
                pass
            self.heartbeat_timer = None
    
    def send_heartbeat(self):
        if self.connected and self.client_socket:
            try:
                self.client_socket.sendall(b"HEARTBEAT\n")
            except:
                self.log("💔 Heartbeat failed - connection lost")
                self.disconnect_socket()
                self.signals.update_status.emit("❌ Connection lost", "red")
                self.reconnect_button.setEnabled(True)
                
                if self.running:
                    self.schedule_reconnect()
    
    def listen_for_commands(self):
        buffer = b""
        consecutive_errors = 0
        max_consecutive_errors = 3
        
        self.restore_destination = None
        
        while self.connected and self.running:
            try:
                self.client_socket.settimeout(1.0)
                
                try:
                    data = self.client_socket.recv(BUFFER_SIZE)
                except socket.timeout:
                    consecutive_errors = 0
                    continue
                
                if not data:
                    self.log("Server closed connection")
                    break
                
                consecutive_errors = 0
                buffer += data
                
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    command = line.decode('utf-8', errors='ignore').strip()
                    
                    if not command:
                        continue
                    
                    try:
                        if command.upper() in ["TRANSFER_COMPLETE", "VERIFIED", "CHUNK_OK", "CHUNK_ERROR", "READY", "ERROR"]:
                            continue
                        
                        elif command.upper() == "PRESENT_FRAME":
                            buffer = self._handle_presentation_frame(buffer)
                            continue
                        
                        elif command.upper() == "RESUMABLE_FILE":
                            try:
                                buffer = self._handle_resumable_transfer(buffer)
                            except Exception as e:
                                self.log(f"Transfer error: {e}")
                            continue
                        
                        elif command.startswith("RESTORE_START:"):
                            self.restore_destination = command.split(":", 1)[1]
                            self.log(f"📥 Restore initiated to: {self.restore_destination}")
                            continue
                        
                        else:
                            self.log(f"Received: {command}")
                            threading.Thread(
                                target=self.process_command,
                                args=(command,),
                                daemon=True
                            ).start()
                    
                    except Exception as cmd_error:
                        self.log(f"Command error: {cmd_error}")
                        continue
            
            except (ConnectionResetError, ConnectionAbortedError):
                self.log("Connection closed by server")
                break
            except OSError as e:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    self.log(f"Too many consecutive errors")
                    break
                time.sleep(0.1)
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    self.log(f"Listen error - disconnecting")
                    break
                time.sleep(0.1)
        
        self.disconnect_socket()
        self.signals.update_status.emit("❌ Disconnected", "red")
        self.reconnect_button.setEnabled(True)
        
        if self.running:
            self.log("Connection lost - auto-reconnect scheduled")
            QTimer.singleShot(0, self.schedule_reconnect)
    
    def process_command(self, command):
        if command.upper() in ["TRANSFER_COMPLETE", "VERIFIED", "CHUNK_OK", "CHUNK_ERROR", "READY"]:
            return
        
        if command == "SHUTDOWN_PC":
            self.shutdown_pc()
            return
        elif command == "RESTART_PC":
            self.restart_pc()
            return
        
        if command.startswith("RESTRICTIONS:"):
            try:
                restrictions_json = command.split(":", 1)[1]
                new_restrictions = json.loads(restrictions_json)
                self.restrictions = new_restrictions
                
                keyword_count = len(new_restrictions.get('keywords', []))
                site_count = len(new_restrictions.get('sites', []))
                
                self.log(f"🚫 Restrictions updated: {keyword_count} keywords, {site_count} sites blocked")
                
                self.update_restriction_indicator()
                
                self.apply_restrictions()
                
                self.signals.show_message.emit(
                    "Content Restrictions Updated",
                    f"Your administrator has updated content restrictions.\n\n"
                    f"Blocked keywords: {keyword_count}\n"
                    f"Blocked websites: {site_count}"
                )
            except Exception as e:
                self.log(f"❌ Failed to parse restrictions: {e}")
            return
        
        if command == "LOCK":
            self.lock_screen()
        elif command == "UNLOCK":
            self.unlock_screen()
        elif command == "START_PRESENTATION":
            self.presentation_signals.lock_requested.emit("")
        elif command == "STOP_PRESENTATION":
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
            
        elif command.startswith("COLLECT_FILES:"):
            source_path = command.split(":", 1)[1]
            threading.Thread(
                target=self.collect_and_send_files,
                args=(source_path,),
                daemon=True
            ).start()
        
        elif command.startswith("SEND_FILE_TO_ADMIN:"):
            file_path = command.split(":", 1)[1]
            threading.Thread(
                target=self.send_file_to_admin,
                args=(file_path,),
                daemon=True
            ).start()
        
        elif command.startswith("BACKUP:"):
            parts = command.split(":", 2)
            if len(parts) >= 3:
                mode = parts[1]
                source_path = parts[2]
                move_files = (mode == "MOVE")
                self.log(f"💾 Backup requested ({mode}): {source_path}")
                threading.Thread(
                    target=self.handle_backup_request,
                    args=(source_path, move_files),
                    daemon=True
                ).start()
            else:
                source_path = command.split(":", 1)[1]
                self.log(f"💾 Backup requested: {source_path}")
                threading.Thread(
                    target=self.handle_backup_request,
                    args=(source_path, False),
                    daemon=True
                ).start()
        
        elif command.startswith("BACKUP_REQUEST:"):
            source_path = command.split(":", 1)[1]
            self.log(f"💾 Backup requested: {source_path}")
            threading.Thread(
                target=self.handle_backup_request,
                args=(source_path, False),
                daemon=True
            ).start()
    
    def shutdown_pc(self):
        """Shutdown this PC"""
        self.log("⚠️ SHUTDOWN command received from admin")
        
        reply = QMessageBox.warning(
            self,
            "⚠️ System Shutdown",
            "Your administrator has requested to shutdown this computer.\n\n"
            "The system will shutdown in 10 seconds.\n\n"
            "Click Cancel to abort.",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok
        )
        
        if reply == QMessageBox.Ok:
            self.log("💀 Shutting down system...")
            try:
                import platform
                if platform.system() == "Windows":
                    os.system("shutdown /s /t 10")
                elif platform.system() == "Linux":
                    os.system("shutdown -h +1")
                else:
                    self.log("⚠️ Shutdown not supported on this OS")
            except Exception as e:
                self.log(f"❌ Shutdown failed: {e}")
        else:
            self.log("Shutdown aborted by user")
    
    def restart_pc(self):
        """Restart this PC"""
        self.log("🔄 RESTART command received from admin")
        
        reply = QMessageBox.warning(
            self,
            "🔄 System Restart",
            "Your administrator has requested to restart this computer.\n\n"
            "The system will restart in 10 seconds.\n\n"
            "Click Cancel to abort.",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok
        )
        
        if reply == QMessageBox.Ok:
            self.log("🔄 Restarting system...")
            try:
                import platform
                if platform.system() == "Windows":
                    os.system("shutdown /r /t 10")
                elif platform.system() == "Linux":
                    os.system("shutdown -r +1")
                else:
                    self.log("⚠️ Restart not supported on this OS")
            except Exception as e:
                self.log(f"❌ Restart failed: {e}")
        else:
            self.log("Restart aborted by user")
    
    def _create_lock_overlay(self, logo_path):
        if getattr(self, "overlay", None) is not None:
            return
        
        self.log("🔒 Screen locked by admin")
        self.signals.update_status.emit("🔒 LOCKED", "red")
        
        if getattr(sys, 'frozen', False):
            script_dir = sys._MEIPASS
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        
        logo_path = os.path.join(script_dir, "school_logo.png")
        
        admin_password = self.config.get('admin_password', 'admin123')
        self.overlay = LockOverlay("🔒 Locked by Administrator", logo_path, self, admin_password)
        self.overlay.showFullScreen()
    
    def lock_screen(self):
        if getattr(self, "overlay", None) is not None:
            return
        
        if getattr(sys, 'frozen', False):
            script_dir = sys._MEIPASS
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        
        logo_path = os.path.join(script_dir, "school_logo.png")
        self.lock_signals.lock_requested.emit(logo_path)
    
    def unlock_screen(self):
        if getattr(self, "overlay", None):
            try:
                self.overlay.close()
            except:
                pass
            finally:
                self.overlay = None
        
        self.signals.update_status.emit("✅ Unlocked", "green")
        self.log("🔓 Screen unlocked")
    
    def _show_presentation(self, _):
        if self.presentation_overlay is None:
            self.log("📽️ Presentation mode started")
            self.presentation_overlay = PresentationOverlay(self)
            self.presentation_overlay.showFullScreen()
            self.signals.update_status.emit("📽️ Viewing Presentation", "yellow")
    
    def _hide_presentation(self):
        if self.presentation_overlay:
            self.log("📽️ Presentation mode ended")
            self.presentation_overlay.close()
            self.presentation_overlay = None
            self.signals.update_status.emit("✅ Connected", "green")
    
    def _handle_presentation_frame(self, buffer):
        try:
            while len(buffer) < 8:
                chunk = self.client_socket.recv(BUFFER_SIZE)
                if not chunk:
                    return buffer
                buffer += chunk
            
            size = struct.unpack(">Q", buffer[:8])[0]
            buffer = buffer[8:]
            
            while len(buffer) < size:
                needed = size - len(buffer)
                chunk = self.client_socket.recv(min(BUFFER_SIZE, needed))
                if not chunk:
                    return buffer
                buffer += chunk
            
            if len(buffer) >= size:
                frame_data = buffer[:size]
                buffer = buffer[size:]
                self.update_presentation_frame(frame_data)
        except:
            pass
        
        return buffer
    
    def update_presentation_frame(self, frame_data):
        if self.presentation_overlay:
            self.presentation_overlay.update_frame(frame_data)
    
    def _handle_resumable_transfer(self, buffer):
        def recv_more(timeout=None):
            try:
                if timeout:
                    self.client_socket.settimeout(timeout)
                return self.client_socket.recv(BUFFER_SIZE)
            except (BlockingIOError, socket.timeout):
                return b""
            finally:
                try:
                    self.client_socket.settimeout(1.0)
                except:
                    pass
        
        def ensure_in_buffer(n):
            nonlocal buffer
            attempts = 0
            max_attempts = 300
            while len(buffer) < n:
                if b"TRANSFER_COMPLETE\n" in buffer:
                    return True
                chunk = recv_more(1.0)
                if chunk == b"":
                    attempts += 1
                    if attempts >= max_attempts:
                        self.log(f"❌ Timeout waiting for data (needed {n}, have {len(buffer)})")
                        return False
                    time.sleep(0.01)
                    continue
                buffer += chunk
                attempts = 0
            return True
        
        try:
            self.log("📥 Processing file transfer...")
            
            try:
                self.client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
                self.client_socket.settimeout(30.0)
            except Exception as e:
                self.log(f"⚠️ Socket config warning: {e}")
            
            if not ensure_in_buffer(4):
                self.log("❌ Failed to read header length")
                return buffer
            
            header_len = struct.unpack(">I", buffer[:4])[0]
            buffer = buffer[4:]
            
            self.log(f"📋 Header length: {header_len} bytes")
            
            if not ensure_in_buffer(header_len):
                self.log("❌ Failed to read metadata")
                return buffer
            
            try:
                metadata = json.loads(buffer[:header_len].decode("utf-8"))
                buffer = buffer[header_len:]
            except Exception as e:
                self.log(f"❌ Failed to parse metadata: {e}")
                return buffer
            
            transfer_id = metadata["transfer_id"]
            filename = metadata["filename"]
            destination = metadata.get("destination", "")
            filesize = metadata["filesize"]
            total_chunks = metadata["total_chunks"]
            chunk_size = metadata.get("chunk_size", CHUNK_SIZE)
            batch_ack_size = metadata.get("batch_ack_size", BATCH_ACK_SIZE)
            
            self.log(f"📥 Receiving: {filename} ({format_bytes(filesize)}, {total_chunks} chunks)")
            self.signals.file_progress.emit(0, f"Starting: {filename}")
            
            receiver = ResumableFileReceiver(transfer_id, filename, destination, filesize, total_chunks)
            
            if destination == "RESTORE_TEMP":
                filepath = os.path.join(RESTORE_TEMP_DIR, filename)
                os.makedirs(RESTORE_TEMP_DIR, exist_ok=True)
                self.log(f"💾 Restore mode: saving to {RESTORE_TEMP_DIR}")
            else:
                filepath = self._resolve_destination_path(destination, filename)
                if not filepath:
                    self.log("❌ Failed to resolve destination path")
                    try:
                        self.client_socket.sendall(b"ERROR:Invalid destination\n")
                    except:
                        pass
                    return buffer
            
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            self.log("✅ Ready to receive - sending READY")
            try:
                self.client_socket.sendall(b"READY\n")
                self.client_socket.settimeout(1.0)
            except Exception as e:
                self.log(f"❌ Failed to send READY: {e}")
                return buffer
            
            self.log("📊 Receiving chunks...")
            
            chunk_data_map = {}
            last_update = time.time()
            chunks_since_ack = 0
            chunks_received = 0
            
            try:
                while len(receiver.received_chunks) < total_chunks:
                    if b"TRANSFER_COMPLETE\n" in buffer:
                        idx = buffer.find(b"TRANSFER_COMPLETE\n")
                        buffer = buffer[idx + len(b"TRANSFER_COMPLETE\n"):]
                        self.log(f"📋 Transfer complete marker received")
                        break
                    
                    if not ensure_in_buffer(72):
                        self.log(f"⚠️ Failed to read chunk header")
                        break
                    
                    try:
                        chunk_index, chunk_data_size = struct.unpack(">II", buffer[:8])
                    except Exception as e:
                        self.log(f"❌ Failed to unpack chunk header: {e}")
                        break
                    
                    checksum = buffer[8:72].rstrip(b"\x00").decode("utf-8")
                    buffer = buffer[72:]
                    
                    if chunk_index >= total_chunks:
                        self.log(f"⚠️ Invalid chunk index {chunk_index} (max: {total_chunks})")
                        break
                    
                    if not ensure_in_buffer(chunk_data_size):
                        self.log(f"⚠️ Failed to read chunk {chunk_index} data ({chunk_data_size} bytes)")
                        break
                    
                    chunk_data = buffer[:chunk_data_size]
                    buffer = buffer[chunk_data_size:]
                    
                    if receiver.is_chunk_received(chunk_index):
                        chunks_since_ack += 1
                        if chunks_since_ack >= batch_ack_size:
                            try:
                                self.client_socket.sendall(b"CHUNK_OK\n")
                            except Exception as e:
                                self.log(f"❌ Failed to send ACK: {e}")
                                return buffer
                            chunks_since_ack = 0
                        continue
                    
                    actual_checksum = receiver._calculate_chunk_checksum(chunk_data)
                    if actual_checksum != checksum:
                        self.log(f"❌ Checksum mismatch for chunk {chunk_index}")
                        try:
                            self.client_socket.sendall(b"CHUNK_ERROR\n")
                        except:
                            pass
                        break
                    
                    receiver.received_chunks[chunk_index] = checksum
                    chunk_data_map[chunk_index] = chunk_data
                    chunks_since_ack += 1
                    chunks_received += 1
                    
                    if chunks_since_ack >= batch_ack_size:
                        try:
                            self.client_socket.sendall(b"CHUNK_OK\n")
                        except Exception as e:
                            self.log(f"❌ Failed to send ACK: {e}")
                            return buffer
                        chunks_since_ack = 0
                        try:
                            receiver._save_progress()
                        except:
                            pass
                    
                    if time.time() - last_update >= 1.0:
                        progress = receiver.get_progress()
                        self.signals.file_progress.emit(
                            int(progress), 
                            f"{filename}: {progress:.0f}% ({chunks_received}/{total_chunks} chunks)"
                        )
                        last_update = time.time()
                
                if chunks_since_ack > 0:
                    try:
                        self.client_socket.sendall(b"CHUNK_OK\n")
                    except:
                        pass
            
            except Exception as e:
                self.log(f"❌ Transfer error: {e}")
                import traceback
                self.log(traceback.format_exc())
                raise
            
            if receiver.is_complete():
                self.log(f"💾 Writing {total_chunks} chunks to disk...")
                try:
                    with open(filepath, "wb") as f:
                        for i in range(total_chunks):
                            if i in chunk_data_map:
                                f.write(chunk_data_map[i])
                            else:
                                self.log(f"⚠️ Missing chunk {i}")
                    
                    try:
                        self.client_socket.sendall(b"VERIFIED\n")
                        self.client_socket.sendall(b"TRANSFER_COMPLETE\n")
                    except:
                        pass
                    
                    receiver.cleanup()
                    self.signals.file_progress.emit(100, f"Complete: {filename}")
                    self.log(f"✅ Saved: {filepath}")
                    
                    if destination == "RESTORE_TEMP":
                        self.log(f"🔄 Starting restore extraction...")
                        threading.Thread(
                            target=self._extract_restore,
                            args=(filepath,),
                            daemon=True
                        ).start()
                    else:
                        self.signals.show_message.emit("File Received", f"Saved to:\n{filepath}")
                    
                    QTimer.singleShot(3000, lambda: self.signals.file_progress.emit(0, ""))
                
                except Exception as e:
                    self.log(f"❌ Write error: {e}")
                    import traceback
                    self.log(traceback.format_exc())
                    raise
            else:
                self.log(f"⚠️ Transfer incomplete: {len(receiver.received_chunks)}/{total_chunks} chunks")
        
        except Exception as e:
            self.log(f"❌ Transfer error: {e}")
            self.signals.file_progress.emit(0, f"Error")
        
        return buffer
    
    def _extract_restore(self, zip_filepath):
        """Extract restore archive with proper error handling"""
        try:
            restore_dest = self.restore_destination or "C:\\Users\\Student\\Documents"
            
            self.log(f"📦 Extracting restore to: {restore_dest}")
            self.signals.show_progress_dialog.emit("Restoring files...", 100)
            
            os.makedirs(restore_dest, exist_ok=True)
            
            try:
                with zipfile.ZipFile(zip_filepath, 'r') as zip_ref:
                    file_list = zip_ref.namelist()
                    total_files = len(file_list)
                    
                    if total_files == 0:
                        raise Exception("Backup archive is empty")
                    
                    self.log(f"📊 Extracting {total_files} files...")
                    
                    for idx, file in enumerate(file_list):
                        try:
                            zip_ref.extract(file, restore_dest)
                        except Exception as e:
                            self.log(f"⚠️ Failed to extract {file}: {e}")
                        
                        if (idx + 1) % max(1, total_files // 20) == 0 or (idx + 1) == total_files:
                            progress = int(((idx + 1) / total_files) * 100)
                            self.signals.update_progress_dialog.emit(
                                progress,
                                f"Extracting files...\n{idx + 1} / {total_files}"
                            )
            except zipfile.BadZipFile:
                raise Exception("Corrupted backup file")
            except Exception as e:
                raise Exception(f"Extraction failed: {e}")
            
            try:
                os.remove(zip_filepath)
            except:
                pass
            
            self.signals.close_progress_dialog.emit()
            self.log(f"✅ Restore complete: {total_files} files restored to {restore_dest}")
            
            QTimer.singleShot(500, lambda: self.signals.show_message.emit(
                "Restore Complete",
                f"Successfully restored {total_files} files to:\n{restore_dest}"
            ))
            
        except Exception as e:
            self.log(f"❌ Restore extraction failed: {e}")
            self.signals.close_progress_dialog.emit()
            QTimer.singleShot(500, lambda: self.signals.show_message.emit(
                "Restore Error",
                f"Failed to restore files:\n{str(e)}"
            ))
    
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
    
    def start_streaming_screen(self):
        if not self.screen_sharing:
            self.screen_sharing = True
            self.log("📹 Screen streaming started")
            self.signals.update_status.emit("📹 Streaming...", "yellow")
            threading.Thread(target=self.stream_screen, daemon=True).start()
    
    def stop_streaming_screen(self):
        if self.screen_sharing:
            self.screen_sharing = False
            self.log("🛑 Screen streaming stopped")
            self.signals.update_status.emit("✅ Connected", "green")
    
    def stream_screen(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            fps = 20
            quality = 50
            
            try:
                while self.screen_sharing and self.connected:
                    start = time.time()
                    
                    img = np.array(sct.grab(monitor))
                    frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                    frame = cv2.resize(frame, (1280, 720))
                    
                    ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    if not ret:
                        continue
                    
                    data = buffer.tobytes()
                    size = struct.pack(">Q", len(data))
                    self.client_socket.sendall(b"FRAME\n" + size + data)
                    
                    elapsed = time.time() - start
                    time.sleep(max(0, 1/fps - elapsed))
            except:
                self.screen_sharing = False
    
    def handle_backup_request(self, source_path, move_files=False):
        try:
            self.log(f"💾 Backup requested: {source_path}")
            
            if not os.path.exists(source_path):
                error_msg = f"Path not found: {source_path}"
                self.log(f"❌ {error_msg}")
                self.client_socket.sendall(f"BACKUP_ERROR:{error_msg}\n".encode("utf-8"))
                return
            
            self.signals.show_progress_dialog.emit("Preparing backup...", 100)
            
            self.signals.update_progress_dialog.emit(5, "Scanning files...")
            
            file_list = []
            total_size = 0
            for root, dirs, files in os.walk(source_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        size = os.path.getsize(file_path)
                        file_list.append((file_path, size))
                        total_size += size
                    except:
                        pass
            
            file_count = len(file_list)
            self.log(f"📊 Found {file_count} files ({format_bytes(total_size)})")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_zip = os.path.join(RESUME_METADATA_DIR, f"backup_{timestamp}.zip")
            
            self.signals.update_progress_dialog.emit(10, f"Creating backup archive...\n0 / {file_count} files")
            
            with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if os.path.isfile(source_path):
                    zipf.write(source_path, os.path.basename(source_path))
                    self.signals.update_progress_dialog.emit(60, "Backup created")
                else:
                    processed = 0
                    for file_path, _ in file_list:
                        arcname = os.path.relpath(file_path, source_path)
                        try:
                            zipf.write(file_path, arcname)
                        except Exception as e:
                            self.log(f"⚠️ Skipped: {os.path.basename(file_path)} ({e})")
                        
                        processed += 1
                        if processed % max(1, file_count // 50) == 0:
                            percent = 10 + int((processed / file_count) * 50)
                            self.signals.update_progress_dialog.emit(
                                percent,
                                f"Creating backup archive...\n{processed} / {file_count} files"
                            )
            
            self.signals.update_progress_dialog.emit(65, "Reading backup file...")
            
            with open(temp_zip, 'rb') as f:
                zip_data = f.read()
            
            zip_size = len(zip_data)
            self.log(f"📦 Backup size: {format_bytes(zip_size)}")
            
            self.signals.update_progress_dialog.emit(
                70,
                f"Uploading backup...\n0% ({format_bytes(0)} / {format_bytes(zip_size)})"
            )
            
            header = f"BACKUP_DATA:{zip_size}\n".encode("utf-8")
            self.client_socket.sendall(header)
            
            chunk_size = 1024 * 1024
            sent = 0
            while sent < zip_size:
                chunk = zip_data[sent:sent + chunk_size]
                self.client_socket.sendall(chunk)
                sent += len(chunk)
                
                upload_percent = int((sent / zip_size) * 100)
                overall_percent = 70 + int(upload_percent * 0.30)
                self.signals.update_progress_dialog.emit(
                    overall_percent,
                    f"Uploading backup...\n{upload_percent}% ({format_bytes(sent)} / {format_bytes(zip_size)})"
                )
            
            self.signals.update_progress_dialog.emit(100, "Backup complete!")
            
            try:
                os.remove(temp_zip)
            except:
                pass
            
            self.log(f"✅ Backup sent successfully ({format_bytes(zip_size)})")
            
            if move_files:
                self.signals.update_progress_dialog.emit(100, "Deleting original files...")
                deleted_count = 0
                failed_count = 0
                
                for file_path, _ in file_list:
                    try:
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            deleted_count += 1
                    except Exception as e:
                        failed_count += 1
                        self.log(f"⚠️ Failed to delete: {os.path.basename(file_path)} ({e})")
                
                try:
                    if os.path.isdir(source_path):
                        for root, dirs, files in os.walk(source_path, topdown=False):
                            for dir_name in dirs:
                                dir_path = os.path.join(root, dir_name)
                                try:
                                    if not os.listdir(dir_path):
                                        os.rmdir(dir_path)
                                except:
                                    pass
                except:
                    pass
                
                self.log(f"🗑️ MOVE completed: {deleted_count} files deleted, {failed_count} failed")
            
            self.signals.close_progress_dialog.emit()
            
            mode_text = "moved (deleted)" if move_files else "backed up"
            warning_text = f"\n⚠️ {failed_count} files could not be deleted" if (move_files and failed_count > 0) else ""
            
            QTimer.singleShot(500, lambda: self.signals.show_message.emit(
                "Backup Complete", 
                f"Successfully {mode_text} {file_count} files\n"
                f"Total size: {format_bytes(zip_size)}\n"
                f"From: {source_path}{warning_text}"
            ))
        
        except Exception as e:
            error_msg = f"Backup failed: {e}"
            self.log(f"❌ {error_msg}")
            try:
                self.client_socket.sendall(f"BACKUP_ERROR:{error_msg}\n".encode("utf-8"))
            except:
                pass
            self.signals.close_progress_dialog.emit()
            self.signals.show_message.emit("Backup Error", error_msg)
    
    def load_custom_pc_name(self):
        try:
            import socket
            config_file = os.path.join(RESUME_METADATA_DIR, "pc_name.txt")
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    name = f.read().strip()
                    if name:
                        return name
        except:
            pass
        
        try:
            import socket
            return socket.gethostname()
        except:
            return "Student-PC"
    
    def save_custom_pc_name(self, name):
        try:
            config_file = os.path.join(RESUME_METADATA_DIR, "pc_name.txt")
            with open(config_file, 'w') as f:
                f.write(name)
            return True
        except Exception as e:
            self.log(f"Failed to save PC name: {e}")
            return False
    
    def change_pc_name(self):
        new_name, ok = QInputDialog.getText(
            self,
            "Change PC Name",
            "Enter new PC name for this computer:",
            QLineEdit.Normal,
            self.custom_pc_name
        )
        
        if ok and new_name and new_name.strip():
            new_name = new_name.strip()
            old_name = self.custom_pc_name
            self.custom_pc_name = new_name
            
            if self.save_custom_pc_name(new_name):
                self.setWindowTitle(f"Student Client - {self.custom_pc_name}")
                self.log(f"PC name changed from '{old_name}' to '{new_name}'")
                
                if self.connected:
                    self.send_client_info()
                
                QMessageBox.information(
                    self,
                    "PC Name Changed",
                    f"PC name successfully changed to:\n{new_name}\n\n"
                    f"The admin will see this name for backups and identification."
                )
            else:
                self.custom_pc_name = old_name
                QMessageBox.warning(
                    self,
                    "Error",
                    "Failed to save PC name. Please try again."
                )
    
    def send_client_info(self):
        if not self.connected or not self.client_socket:
            return
        
        try:
            info = {
                "hostname": self.custom_pc_name,
                "status": "connected"
            }
            info_json = json.dumps(info)
            self.client_socket.sendall(f"INFO:{info_json}\n".encode("utf-8"))
            self.log(f"Sent client info to admin: {self.custom_pc_name}")
        except Exception as e:
            self.log(f"Failed to send client info: {e}")
    
    def collect_and_send_files(self, source_path):
        try:
            self.log(f"Collecting files from: {source_path}")
            files_data = []
            
            for root, dirs, files in os.walk(source_path):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    try:
                        file_hash = self._calculate_file_hash(file_path)
                        
                        files_data.append({
                            "path": file_path,
                            "name": filename,
                            "hash": file_hash,
                            "size": os.path.getsize(file_path)
                        })
                    except:
                        pass
            
            file_list_json = json.dumps(files_data)
            header = f"FILE_LIST:{len(file_list_json)}\n"
            
            self.client_socket.sendall(header.encode())
            self.client_socket.sendall(file_list_json.encode())
            
            self.log(f"Sent file list: {len(files_data)} files from {source_path}")
            
        except Exception as e:
            self.log(f"Error collecting files: {e}")
    
    def send_file_to_admin(self, file_path):
        try:
            if not os.path.exists(file_path):
                return
            
            header = b"ADMIN_FILE\n"
            metadata = {
                "path": file_path,
                "name": os.path.basename(file_path)
            }
            meta_json = json.dumps(metadata).encode()
            meta_len = struct.pack(">I", len(meta_json))
            
            self.client_socket.sendall(header)
            self.client_socket.sendall(meta_len)
            self.client_socket.sendall(meta_json)
            
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    self.client_socket.sendall(chunk)
            
            self.client_socket.sendall(b"<END>")
            self.log(f"File sent to admin: {os.path.basename(file_path)}")
            
        except Exception as e:
            self.log(f"Error sending file to admin: {e}")
    
    def _calculate_file_hash(self, file_path):
        sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except:
            return ""
    
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