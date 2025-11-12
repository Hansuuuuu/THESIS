"""
Lab Manager - Admin Server - MODIFIED VERSION
Changes:
- Backup now MOVES files from client (deletes after backup)
- Better backup UI with templates and PC name organization
- Restore with selectable client destination
- Auto-distribution based on PC names
- Right-click context menu on client list
- PC icons in client list
- FIXED: Restore crash issues
- NEW: Shutdown/Restart PC functions
- NEW: HandyCafe-style UI layout
"""
import concurrent.futures
import ctypes
import shutil
import sys
import os
import socket
import threading
import struct
import time
import json
import hashlib
import winreg
import zipfile
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty
from collections import defaultdict, deque
from file_transfer_db_mysql import FileTransferDB


import uuid
import socket
import mss
import cv2
import numpy as np
from login_window import LoginDialog
import socket

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QFileDialog, QMessageBox,
    QTextEdit, QTabWidget, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QInputDialog, QLineEdit,
    QDialog, QCheckBox, QRadioButton, QButtonGroup, QMenu, QSplitter,
    QFrame, QScrollArea, QGridLayout, QListWidgetItem, QTreeWidgetItem
)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QPoint, QSize
from PyQt5.QtGui import QPixmap, QImage, QFont, QIcon, QCursor, QColor, QPainter, QPen, QBrush
from PyQt5.QtCore import QByteArray
import requests

# Configuration
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5001
RECV_BUFFER = 65536
MAX_IMAGE_SIZE = 200 * 1024 * 1024
SOCKET_SEND_BUFFER = 32 * 1024 * 1024
SOCKET_RECV_BUFFER = 32 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024
BATCH_ACK_SIZE = 20
CHUNK_SEND_DELAY = 0.0001

RESTRICTIONS_FILE = "restrictions.txt"   # Where we save restrictions
ADULT_SITES_FILE = "list.txt"

INBOX_DIR = os.path.join(os.path.expanduser("~"), "lab_inbox_admin")
RESUME_METADATA_DIR = os.path.join(os.path.expanduser("~"), "lab_transfer_cache")
os.makedirs(INBOX_DIR, exist_ok=True)
os.makedirs(RESUME_METADATA_DIR, exist_ok=True)
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "ClientBackups")
os.makedirs(BACKUP_DIR, exist_ok=True)

RESTRICTIONS_FILE = os.path.join(os.path.expanduser("~"), "lab_restrictions.json")

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
    statistics_updated = pyqtSignal()  # NEW
    
    
class RestrictionDialog(QDialog):
    def __init__(self, parent=None, current_restrictions=None):
        super().__init__(parent)
        self.setWindowTitle("Site Restrictions")
        self.resize(700, 600)

        # 🎨 Dark mode style
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #e0e0e0;
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
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 12px;
                padding-bottom: 8px;
                font-weight: bold;
                font-size: 11px;
                color: #4EC9B0;
            }
            QLineEdit, QListWidget {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
                color: #e0e0e0;
            }
            QLabel {
                color: #e0e0e0;
            }
        """)

        self.restrictions = current_restrictions or {"sites": []}

        layout = QVBoxLayout(self)

        # Header
        header = QLabel("🚫 Content Restriction Management")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet("color: #d13438; padding: 10px;")
        layout.addWidget(header)

        info = QLabel("Block websites and incognito mode on all client computers")
        info.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(info)

        # 🌐 Blocked Websites
        site_group = QGroupBox("🌐 Blocked Websites")
        site_layout = QVBoxLayout(site_group)

        site_info = QLabel("Domains to block (e.g., 'facebook.com', 'youtube.com', 'reddit.com')")
        site_info.setStyleSheet("color: #888; font-size: 10px;")
        site_layout.addWidget(site_info)

        self.site_list = QListWidget()
        for site in self.restrictions.get("sites", []):
            self.site_list.addItem(site)
        site_layout.addWidget(self.site_list)

        site_btn_layout = QHBoxLayout()
        self.txt_site = QLineEdit()
        self.txt_site.setPlaceholderText("Enter domain to block (e.g., example.com)...")
        site_btn_layout.addWidget(self.txt_site)

        btn_add_site = QPushButton("➕ Add")
        btn_add_site.clicked.connect(self._add_site)
        site_btn_layout.addWidget(btn_add_site)

        btn_remove_site = QPushButton("➖ Remove")
        btn_remove_site.clicked.connect(self._remove_site)
        site_btn_layout.addWidget(btn_remove_site)

        site_layout.addLayout(site_btn_layout)
        layout.addWidget(site_group)

        # ⚙️ Preset Buttons
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("Quick Presets:"))

        btn_preset_social = QPushButton("📱 Block Social Media")
        btn_preset_social.clicked.connect(self._preset_social_media)
        preset_layout.addWidget(btn_preset_social)

        btn_preset_adult = QPushButton("🔞 Block Adult Content")
        btn_preset_adult.clicked.connect(self._preset_adult_content)
        preset_layout.addWidget(btn_preset_adult)

        btn_clear_all = QPushButton("🗑️ Clear All")
        btn_clear_all.clicked.connect(self._clear_all)
        btn_clear_all.setStyleSheet("background-color: #d13438;")
        preset_layout.addWidget(btn_clear_all)

        preset_layout.addStretch()
        layout.addLayout(preset_layout)

        # 🧱 Incognito Controls
        incognito_layout = QHBoxLayout()
        incognito_layout.addWidget(QLabel("Incognito Control:"))

        btn_disable_incognito = QPushButton("🚫 Disable Incognito")
        btn_disable_incognito.clicked.connect(self._on_disable_incognito)
        incognito_layout.addWidget(btn_disable_incognito)

        btn_enable_incognito = QPushButton("✅ Enable Incognito")
        btn_enable_incognito.clicked.connect(self._on_enable_incognito)
        incognito_layout.addWidget(btn_enable_incognito)

        layout.addLayout(incognito_layout)

        # Save/Cancel buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)

        btn_save = QPushButton("💾 Save & Apply")
        btn_save.clicked.connect(self.accept)
        btn_save.setStyleSheet("background-color: #107c10;")
        btn_layout.addWidget(btn_save)

        layout.addLayout(btn_layout)
    def _on_disable_incognito(self):
        try:
            parent = self.parent()
            if hasattr(parent, "server"):
                parent.server._disable_incognito()
            else:
                QMessageBox.warning(self, "Missing Server", "⚠️ AdminServer instance not found in parent.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Failed to disable incognito:\n{e}")

    def _on_enable_incognito(self):
        try:
            parent = self.parent()
            if hasattr(parent, "server"):
                parent.server._enable_incognito()
            else:
                QMessageBox.warning(self, "Missing Server", "⚠️ AdminServer instance not found in parent.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Failed to enable incognito:\n{e}")

    # 🧩 Add & Remove Sites
    def _add_site(self):
        site = self.txt_site.text().strip().lower()
        site = site.replace("http://", "").replace("https://", "").replace("www.", "")
        if site and site not in [self.site_list.item(i).text() for i in range(self.site_list.count())]:
            self.site_list.addItem(site)
            self.txt_site.clear()

    def _remove_site(self):
        current = self.site_list.currentItem()
        if current:
            self.site_list.takeItem(self.site_list.row(current))

    # ⚙️ Presets
    def _preset_social_media(self):
        social_sites = [
            "facebook.com", "twitter.com", "instagram.com",
            "tiktok.com", "snapchat.com", "reddit.com", "openai.com"
        ]
        for site in social_sites:
            if site not in [self.site_list.item(i).text() for i in range(self.site_list.count())]:
                self.site_list.addItem(site)

    def _preset_adult_content(self):
        try:
            ADULT_SITES_FILE = "list.txt"
            if not os.path.exists(ADULT_SITES_FILE):
                QMessageBox.warning(self, "File Missing", f"❌ Adult sites list not found:\n{ADULT_SITES_FILE}")
                return

            with open(ADULT_SITES_FILE, 'r', encoding='utf-8') as f:
                adult_sites = [line.strip() for line in f if line.strip()]

            current_sites = [self.site_list.item(i).text() for i in range(self.site_list.count())]
            combined_sites = sorted(set(current_sites + adult_sites))

            self.restrictions["sites"] = combined_sites
            self.site_list.clear()
            self.site_list.addItems(combined_sites)

            RESTRICTIONS_FILE = "restrictions.txt"
            with open(RESTRICTIONS_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(combined_sites))

            QMessageBox.information(
                self,
                "Adult Sites Added",
                f"✅ Loaded and merged {len(adult_sites)} adult sites.\n\n"
                f"Total blocked: {len(combined_sites)}"
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Failed to load adult sites:\n{e}")

    

    # 🧹 Clear All
    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear All Restrictions",
            "Remove all site restrictions?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.site_list.clear()

    # 📦 Export Current Restrictions
    def get_restrictions(self):
        sites = [self.site_list.item(i).text() for i in range(self.site_list.count())]
        return {"sites": sites}

class BackupConfigDialog(QDialog):
    def __init__(self, parent=None, selected_clients=None):
        super().__init__(parent)
        self.setWindowTitle("Backup Configuration")
        self.resize(600, 500)
        self.selected_clients = selected_clients or []
        
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #e0e0e0;
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
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 12px;
                padding-bottom: 8px;
                font-weight: bold;
                font-size: 11px;
                color: #4EC9B0;
            }
            QLineEdit, QTextEdit {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
                color: #e0e0e0;
            }
            QLabel {
                color: #e0e0e0;
            }
            QRadioButton {
                color: #e0e0e0;
            }
        """)
        
        layout = QVBoxLayout(self)
        
        header = QLabel("📦 Backup Configuration Wizard")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet("color: #0078d4; padding: 10px;")
        layout.addWidget(header)
        
        client_list = "\n".join([f"  • {name}" for name in self.selected_clients])
        info_label = QLabel(f"Selected Clients ({len(self.selected_clients)}):\n{client_list}")
        info_label.setStyleSheet("color: #4EC9B0; font-weight: bold;")
        layout.addWidget(info_label)
        
        source_group = QGroupBox("📁 Source Path (on clients)")
        source_layout = QVBoxLayout(source_group)
        
        template_label = QLabel("Quick Templates:")
        source_layout.addWidget(template_label)
        
        template_btn_layout = QHBoxLayout()
        
        templates = [
            ("📄 Documents", "C:\\Users\\Student\\Documents"),
            ("🖼️ Desktop", "C:\\Users\\Student\\Desktop"),
            ("📥 Downloads", "C:\\Users\\Student\\Downloads"),
            ("📁 User Folder", "C:\\Users\\Student")
        ]
        
        for label, path in templates:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, p=path: self.txt_source.setText(p))
            template_btn_layout.addWidget(btn)
        
        source_layout.addLayout(template_btn_layout)
        
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel("Custom Path:"))
        self.txt_source = QLineEdit()
        self.txt_source.setPlaceholderText("e.g., C:\\Users\\Student\\Documents")
        self.txt_source.setText("C:\\Users\\Student\\Documents")
        path_layout.addWidget(self.txt_source)
        source_layout.addLayout(path_layout)
        
        layout.addWidget(source_group)
        
        options_group = QGroupBox("⚙️ Backup Options")
        options_layout = QVBoxLayout(options_group)
        
        self.chk_move = QCheckBox("🔄 MOVE files (delete from client after backup)")
        self.chk_move.setChecked(True)
        self.chk_move.setStyleSheet("color: #ff6b6b; font-weight: bold;")
        options_layout.addWidget(self.chk_move)
        
        warning_label = QLabel("⚠️ Warning: MOVE will permanently delete files from the client!")
        warning_label.setStyleSheet("color: #ff6b6b; font-style: italic;")
        options_layout.addWidget(warning_label)
        
        layout.addWidget(options_group)
        
        dest_group = QGroupBox("💾 Backup Destination (on this PC)")
        dest_layout = QVBoxLayout(dest_group)
        
        dest_path_layout = QHBoxLayout()
        dest_path_layout.addWidget(QLabel("Destination:"))
        self.txt_destination = QLineEdit()
        self.txt_destination.setText(BACKUP_DIR)
        self.txt_destination.setReadOnly(True)
        dest_path_layout.addWidget(self.txt_destination)
        
        btn_browse_dest = QPushButton("Browse...")
        btn_browse_dest.clicked.connect(self._browse_destination)
        dest_path_layout.addWidget(btn_browse_dest)
        
        dest_layout.addLayout(dest_path_layout)
        
        dest_note = QLabel("📋 Files will be organized in folders named by PC name")
        dest_note.setStyleSheet("color: #4EC9B0; font-style: italic;")
        dest_layout.addWidget(dest_note)
        
        layout.addWidget(dest_group)
        
        summary_group = QGroupBox("📊 Summary")
        summary_layout = QVBoxLayout(summary_group)
        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        summary_layout.addWidget(self.lbl_summary)
        layout.addWidget(summary_group)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        
        btn_ok = QPushButton("✅ Start Backup")
        btn_ok.clicked.connect(self.accept)
        btn_ok.setStyleSheet("background-color: #107c10;")
        btn_layout.addWidget(btn_ok)
        
        layout.addLayout(btn_layout)
        
        self.txt_source.textChanged.connect(self._update_summary)
        self.txt_destination.textChanged.connect(self._update_summary)
        self.chk_move.stateChanged.connect(self._update_summary)
        
        self._update_summary()
    
    def _browse_destination(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Backup Destination Folder",
            self.txt_destination.text()
        )
        if folder:
            self.txt_destination.setText(folder)
    
    def _update_summary(self):
        source = self.txt_source.text()
        dest = self.txt_destination.text()
        move_mode = self.chk_move.isChecked()
        
        mode_text = "MOVE (delete from client)" if move_mode else "COPY (keep on client)"
        
        summary = f"""
<b>Source Path:</b> {source}<br>
<b>Destination:</b> {dest}<br>
<b>Mode:</b> <span style='color: {'#ff6b6b' if move_mode else '#4EC9B0'};'>{mode_text}</span><br>
<b>Clients:</b> {len(self.selected_clients)}<br>
<br>
<b>Organization:</b> Each client's files will be saved in a folder named after their PC name.
        """
        
        self.lbl_summary.setText(summary)
    
    def get_config(self):
        return {
            'source_path': self.txt_source.text(),
            'destination': self.txt_destination.text(),
            'move_files': self.chk_move.isChecked()
        }


class RestoreConfigDialog(QDialog):
    def __init__(self, parent=None, selected_clients=None):
        super().__init__(parent)
        self.setWindowTitle("Restore Configuration")
        self.resize(600, 500)
        self.selected_clients = selected_clients or []
        self.backup_folders = {}
        
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e1e;
                color: #e0e0e0;
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
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 12px;
                padding-bottom: 8px;
                font-weight: bold;
                font-size: 11px;
                color: #4EC9B0;
            }
            QLineEdit {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
                color: #e0e0e0;
            }
            QLabel {
                color: #e0e0e0;
            }
            QRadioButton {
                color: #e0e0e0;
            }
        """)
        
        layout = QVBoxLayout(self)
        
        header = QLabel("📥 Restore Configuration Wizard")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet("color: #107c10; padding: 10px;")
        layout.addWidget(header)
        
        client_list = "\n".join([f"  • {name}" for name in self.selected_clients])
        info_label = QLabel(f"Selected Clients ({len(self.selected_clients)}):\n{client_list}")
        info_label.setStyleSheet("color: #4EC9B0; font-weight: bold;")
        layout.addWidget(info_label)
        
        source_group = QGroupBox("📦 Backup Source")
        source_layout = QVBoxLayout(source_group)
        
        btn_scan = QPushButton("🔍 Scan for Backups")
        btn_scan.clicked.connect(self._scan_backups)
        source_layout.addWidget(btn_scan)
        
        self.lbl_scan_result = QLabel("Click 'Scan for Backups' to find available backup folders")
        self.lbl_scan_result.setWordWrap(True)
        self.lbl_scan_result.setStyleSheet("color: #4EC9B0; font-style: italic;")
        source_layout.addWidget(self.lbl_scan_result)
        
        layout.addWidget(source_group)
        
        dest_group = QGroupBox("📁 Restore Destination (on clients)")
        dest_layout = QVBoxLayout(dest_group)
        
        template_label = QLabel("Quick Templates:")
        dest_layout.addWidget(template_label)
        
        template_btn_layout = QHBoxLayout()
        templates = [
            ("📄 Documents", "C:\\Users\\Student\\Documents"),
            ("🖼️ Desktop", "C:\\Users\\Student\\Desktop"),
            ("📥 Downloads", "C:\\Users\\Student\\Downloads"),
        ]
        
        for label, path in templates:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, p=path: self.txt_restore_dest.setText(p))
            template_btn_layout.addWidget(btn)
        
        dest_layout.addLayout(template_btn_layout)
        
        dest_path_layout = QHBoxLayout()
        dest_path_layout.addWidget(QLabel("Custom Path:"))
        self.txt_restore_dest = QLineEdit()
        self.txt_restore_dest.setText("C:\\Users\\Student\\Documents")
        dest_path_layout.addWidget(self.txt_restore_dest)
        
        dest_layout.addLayout(dest_path_layout)
        
        layout.addWidget(dest_group)
        
        summary_group = QGroupBox("📊 Summary")
        summary_layout = QVBoxLayout(summary_group)
        self.lbl_summary = QLabel("Configure restore options above")
        self.lbl_summary.setWordWrap(True)
        summary_layout.addWidget(self.lbl_summary)
        layout.addWidget(summary_group)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        
        btn_ok = QPushButton("✅ Start Restore")
        btn_ok.clicked.connect(self.accept)
        btn_ok.setStyleSheet("background-color: #107c10;")
        btn_layout.addWidget(btn_ok)
        
        layout.addLayout(btn_layout)
    
    def _scan_backups(self):
        if not os.path.exists(BACKUP_DIR):
            self.lbl_scan_result.setText("❌ Backup directory not found!")
            return
        
        backup_subfolders = [f for f in os.listdir(BACKUP_DIR) 
                            if os.path.isdir(os.path.join(BACKUP_DIR, f))]
        
        backup_dated_folders = [f for f in backup_subfolders if f.startswith("Backup_")]
        
        if not backup_dated_folders:
            self.lbl_scan_result.setText("❌ No backup folders found!")
            return
        
        backup_dated_folders.sort(reverse=True)
        latest_backup = backup_dated_folders[0]
        latest_backup_path = os.path.join(BACKUP_DIR, latest_backup)
        
        pc_folders = {}
        for item in os.listdir(latest_backup_path):
            item_path = os.path.join(latest_backup_path, item)
            if os.path.isdir(item_path):
                files_folder = os.path.join(item_path, "files")
                if os.path.exists(files_folder):
                    pc_folders[item] = files_folder
        
        if not pc_folders:
            self.lbl_scan_result.setText(f"❌ No PC backups found in {latest_backup}")
            return
        
        self.backup_folders = pc_folders
        matched_list = []
        unmatched_list = []
        
        for client in self.selected_clients:
            if client in pc_folders:
                matched_list.append(client)
            else:
                unmatched_list.append(client)
        
        result_text = f"""
<b>✅ Found {len(pc_folders)} PC backup(s) in:</b><br>
{latest_backup}<br><br>
<b>Available PC Backups:</b> {', '.join(pc_folders.keys())}<br><br>
"""
        
        if matched_list:
            result_text += f"""<b>✅ Matched Clients ({len(matched_list)}/{len(self.selected_clients)}):</b><br>
{'<br>'.join([f"&nbsp;&nbsp;✓ {name}" for name in matched_list])}<br><br>
"""
        
        if unmatched_list:
            result_text += f"""<b>⚠️ No Backup Found For ({len(unmatched_list)}/{len(self.selected_clients)}):</b><br>
{'<br>'.join([f"&nbsp;&nbsp;✗ {name}" for name in unmatched_list])}<br><br>
"""
        
        result_text += "<span style='color: #4EC9B0;'>✓ Files will be automatically distributed to matching PC names</span>"
        
        self.lbl_scan_result.setText(result_text)
    
    def get_config(self):
        return {
            'restore_destination': self.txt_restore_dest.text(),
            'backup_folders': self.backup_folders
        }

class FileSyncManager:
    def __init__(self, server, base_sync_dir=None):
        self.server = server
        self.base_sync_dir = base_sync_dir or os.path.join(os.path.expanduser("~"), "lab_auto_backup")
        os.makedirs(self.base_sync_dir, exist_ok=True)
        
        # Enhanced tracking with more metadata
        self.file_metadata = {}  # {client_key: {file_path: {hash, size, mtime, version}}}
        self.client_configs = {}  # {client_key: {"source_path": path, "enabled": bool}}
        self.sync_thread = None
        self.sync_running = False
        self.sync_interval = 30  # seconds
        self.last_sync_time = {}
        self.sync_stats = defaultdict(lambda: {
            "files_synced": 0, 
            "bytes_synced": 0, 
            "last_sync": None,
            "content_changes": 0,
            "new_files": 0,
            "deleted_files": 0
        })
        
        # Version control settings
        self.keep_versions = 5  # Keep last 5 versions
        self.version_dir = os.path.join(self.base_sync_dir, "_versions")
        os.makedirs(self.version_dir, exist_ok=True)
        
        self.server.log(f"📁 Auto-backup system initialized at {self.base_sync_dir}")
        self.server.log(f"📚 File versioning enabled (keeping {self.keep_versions} versions)")
    
    def enable_auto_sync_for_client(self, client_key, source_path):
        """Enable auto-sync for a specific client"""
        self.client_configs[client_key] = {
            "source_path": source_path,
            "enabled": True,
            "last_check": None
        }
        self.server.log(f"✅ Auto-sync enabled for {client_key}: {source_path}")
        
        if not self.sync_running:
            self.start_sync_cycle()
    
    def disable_auto_sync_for_client(self, client_key):
        """Disable auto-sync for a specific client"""
        if client_key in self.client_configs:
            self.client_configs[client_key]["enabled"] = False
            self.server.log(f"⏸️ Auto-sync disabled for {client_key}")
    
    def enable_auto_sync_all(self, source_path):
        """Enable auto-sync for all connected clients"""
        clients = self.server.list_clients()
        for client_key in clients:
            self.enable_auto_sync_for_client(client_key, source_path)
        
        self.server.log(f"✅ Auto-sync enabled for {len(clients)} client(s)")
    
    def set_sync_interval(self, seconds):
        """Change the sync interval"""
        self.sync_interval = max(10, seconds)  # Minimum 10 seconds
        self.server.log(f"⏱️ Sync interval set to {self.sync_interval} seconds")
    
    def start_sync_cycle(self):
        """Start the automatic sync cycle"""
        if self.sync_running:
            self.server.log("⚠️ Sync cycle already running")
            return
        
        self.sync_running = True
        self.server.log(f"🔄 Auto-sync started (interval: {self.sync_interval}s)")
        
        def sync_loop():
            while self.sync_running:
                try:
                    # Check each configured client
                    for client_key, config in list(self.client_configs.items()):
                        if not config.get("enabled", False):
                            continue
                        
                        # Check if client is still connected
                        with self.server.clients_lock:
                            if client_key not in self.server.clients:
                                continue
                        
                        source_path = config.get("source_path")
                        if source_path:
                            self._sync_client_files(client_key, source_path)
                    
                    time.sleep(self.sync_interval)
                
                except Exception as e:
                    self.server.log(f"❌ Sync cycle error: {e}")
                    time.sleep(5)
        
        self.sync_thread = threading.Thread(target=sync_loop, daemon=True)
        self.sync_thread.start()
    
    def stop_sync_cycle(self):
        """Stop the automatic sync cycle"""
        self.sync_running = False
        self.server.log("⏹️ Auto-sync stopped")
    
    def _sync_client_files(self, client_key, source_path):
        """Request file list from client to check for changes"""
        try:
            cmd = f"COLLECT_FILES:{source_path}"
            
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    handler.send_command(cmd)
                    
                    # Update last check time
                    if client_key in self.client_configs:
                        self.client_configs[client_key]["last_check"] = time.time()
        
        except Exception as e:
            self.server.log(f"❌ Sync error for {client_key}: {e}")
    
    def receive_file_list(self, client_key, files_data):
        """Process file list from client and detect ALL changes including content changes"""
        try:
            files = json.loads(files_data)
            
            # Get client hostname for better organization
            hostname = client_key.split(":")[0]
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    hostname = handler.client_info.get("hostname", hostname)
            
            client_sync_dir = os.path.join(self.base_sync_dir, hostname)
            os.makedirs(client_sync_dir, exist_ok=True)
            
            # Initialize metadata tracking for this client
            if client_key not in self.file_metadata:
                self.file_metadata[client_key] = {}
            
            current_files = set()
            new_files = []
            content_changed_files = []
            size_changed_files = []
            
            # Detect new and modified files with detailed change tracking
            for file_info in files:
                file_path = file_info["path"]
                file_hash = file_info["hash"]
                file_size = file_info.get("size", 0)
                file_name = file_info.get("name", os.path.basename(file_path))
                
                current_files.add(file_path)
                old_metadata = self.file_metadata[client_key].get(file_path)
                
                if old_metadata is None:
                    # Completely NEW file
                    new_files.append((file_path, file_size, file_name))
                    self.server.log(f"🆕 NEW FILE: {hostname} -> {file_name} ({format_bytes(file_size)})")
                    self.sync_stats[client_key]["new_files"] += 1
                
                else:
                    # File exists, check for changes
                    old_hash = old_metadata.get("hash")
                    old_size = old_metadata.get("size", 0)
                    
                    # CONTENT CHANGE: Hash is different
                    if old_hash != file_hash:
                        size_diff = file_size - old_size
                        change_type = "grew" if size_diff > 0 else "shrunk" if size_diff < 0 else "modified"
                        
                        content_changed_files.append((file_path, file_size, file_name))
                        
                        self.server.log(
                            f"📝 CONTENT CHANGED: {hostname} -> {file_name}\n"
                            f"   Old: {format_bytes(old_size)} (hash: {old_hash[:8]}...)\n"
                            f"   New: {format_bytes(file_size)} (hash: {file_hash[:8]}...)\n"
                            f"   Change: {change_type} by {format_bytes(abs(size_diff))}"
                        )
                        
                        self.sync_stats[client_key]["content_changes"] += 1
                    
                    # SIZE CHANGE without hash (shouldn't happen but catch it)
                    elif old_size != file_size:
                        size_changed_files.append((file_path, file_size, file_name))
                        self.server.log(
                            f"⚠️ SIZE CHANGED: {hostname} -> {file_name}\n"
                            f"   Size: {format_bytes(old_size)} -> {format_bytes(file_size)}"
                        )
                
                # Update metadata with version tracking
                version = old_metadata.get("version", 0) + 1 if old_metadata else 1
                
                self.file_metadata[client_key][file_path] = {
                    "hash": file_hash,
                    "size": file_size,
                    "name": file_name,
                    "version": version,
                    "last_update": time.time(),
                    "last_update_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            
            # Detect DELETED files
            old_files = set(self.file_metadata[client_key].keys())
            deleted_files = old_files - current_files
            
            if deleted_files:
                for file_path in deleted_files:
                    old_meta = self.file_metadata[client_key][file_path]
                    file_name = old_meta.get("name", os.path.basename(file_path))
                    
                    self.server.log(
                        f"🗑️ DELETED: {hostname} -> {file_name}\n"
                        f"   Last version: v{old_meta.get('version', 1)}\n"
                        f"   Last seen: {old_meta.get('last_update_str', 'Unknown')}"
                    )
                    
                    # Archive the metadata before removing
                    self._archive_deleted_file_metadata(client_key, file_path, old_meta)
                    
                    # Remove from tracking
                    del self.file_metadata[client_key][file_path]
                    
                    self.sync_stats[client_key]["deleted_files"] += 1
                    
                    # Keep the backed up file but rename it to show it's deleted
                    local_path = os.path.join(client_sync_dir, file_name)
                    if os.path.exists(local_path):
                        deleted_path = local_path + ".DELETED"
                        try:
                            shutil.move(local_path, deleted_path)
                            self.server.log(f"   📦 Archived to: {os.path.basename(deleted_path)}")
                        except:
                            pass
            
            # Request ALL changed files (new + content changed + size changed)
            files_to_request = new_files + content_changed_files + size_changed_files
            
            if files_to_request:
                total_size = sum(size for _, size, _ in files_to_request)
                
                self.server.log(
                    f"📊 SYNC SUMMARY for {hostname}:\n"
                    f"   🆕 New: {len(new_files)}\n"
                    f"   📝 Content Changed: {len(content_changed_files)}\n"
                    f"   ⚠️ Size Changed: {len(size_changed_files)}\n"
                    f"   🗑️ Deleted: {len(deleted_files)}\n"
                    f"   💾 Total to sync: {format_bytes(total_size)}"
                )
                
                # Request each changed file
                for file_path, file_size, file_name in files_to_request:
                    self._request_file_from_client(client_key, file_path)
                
                # Update sync stats
                self.sync_stats[client_key]["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            else:
                # No changes detected
                if len(files) > 0:
                    self.server.log(
                        f"✅ {hostname}: No changes detected\n"
                        f"   📁 Monitoring {len(files)} file(s)"
                    )
        
        except Exception as e:
            self.server.log(f"❌ Error processing file list from {client_key}: {e}")
            import traceback
            self.server.log(traceback.format_exc())
    
    def _archive_deleted_file_metadata(self, client_key, file_path, metadata):
        """Archive metadata of deleted files for history"""
        try:
            archive_file = os.path.join(self.version_dir, "deleted_files_log.json")
            
            archive_data = {}
            if os.path.exists(archive_file):
                with open(archive_file, 'r') as f:
                    archive_data = json.load(f)
            
            if client_key not in archive_data:
                archive_data[client_key] = []
            
            archive_data[client_key].append({
                "file_path": file_path,
                "metadata": metadata,
                "deleted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            with open(archive_file, 'w') as f:
                json.dump(archive_data, f, indent=2)
        
        except:
            pass
    
    def _request_file_from_client(self, client_key, file_path):
        """Request a specific file from client"""
        try:
            cmd = f"SEND_FILE_TO_ADMIN:{file_path}"
            
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    handler.send_command(cmd)
        
        except Exception as e:
            self.server.log(f"❌ Error requesting file from {client_key}: {e}")
    
    def receive_file_from_client(self, client_key, file_info, file_data):
        """Save received file with versioning support"""
        try:
            file_path = file_info.get("path", "unknown")
            file_name = file_info.get("name", "file")
            
            # Get client hostname
            hostname = client_key.split(":")[0]
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    hostname = handler.client_info.get("hostname", hostname)
            
            client_dir = os.path.join(self.base_sync_dir, hostname)
            os.makedirs(client_dir, exist_ok=True)
            
            # Get current version
            metadata = self.file_metadata.get(client_key, {}).get(file_path, {})
            version = metadata.get("version", 1)
            
            # Main file path
            save_path = os.path.join(client_dir, file_name)
            
            # Create versioned backup if file already exists
            if os.path.exists(save_path):
                # Create version directory for this client
                version_client_dir = os.path.join(self.version_dir, hostname)
                os.makedirs(version_client_dir, exist_ok=True)
                
                # Create versioned filename
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base_name, ext = os.path.splitext(file_name)
                version_filename = f"{base_name}_v{version-1}_{timestamp}{ext}"
                version_path = os.path.join(version_client_dir, version_filename)
                
                try:
                    # Copy existing file to version history
                    shutil.copy2(save_path, version_path)
                    self.server.log(f"   📚 Version saved: {version_filename}")
                    
                    # Clean old versions (keep only last N versions)
                    self._cleanup_old_versions(version_client_dir, file_name)
                
                except Exception as e:
                    self.server.log(f"   ⚠️ Version backup failed: {e}")
            
            # Save new file
            with open(save_path, "wb") as f:
                f.write(file_data)
            
            # Calculate hash for verification
            received_hash = hashlib.sha256(file_data).hexdigest()
            
            # Update stats
            self.sync_stats[client_key]["files_synced"] += 1
            self.sync_stats[client_key]["bytes_synced"] += len(file_data)
            
            self.server.log(
                f"💾 SAVED: {hostname} -> {file_name}\n"
                f"   Version: v{version}\n"
                f"   Size: {format_bytes(len(file_data))}\n"
                f"   Hash: {received_hash[:16]}...\n"
                f"   Location: {save_path}"
            )
        
        except Exception as e:
            self.server.log(f"❌ Error saving file from {client_key}: {e}")
            import traceback
            self.server.log(traceback.format_exc())
    
    def _cleanup_old_versions(self, version_dir, file_name):
        """Keep only the last N versions of a file"""
        try:
            base_name, ext = os.path.splitext(file_name)
            
            # Find all versions of this file
            all_versions = []
            for f in os.listdir(version_dir):
                if f.startswith(base_name) and f.endswith(ext):
                    full_path = os.path.join(version_dir, f)
                    mtime = os.path.getmtime(full_path)
                    all_versions.append((full_path, mtime))
            
            # Sort by modification time (newest first)
            all_versions.sort(key=lambda x: x[1], reverse=True)
            
            # Delete old versions beyond the limit
            if len(all_versions) > self.keep_versions:
                for old_file, _ in all_versions[self.keep_versions:]:
                    try:
                        os.remove(old_file)
                        self.server.log(f"   🗑️ Cleaned old version: {os.path.basename(old_file)}")
                    except:
                        pass
        
        except Exception as e:
            self.server.log(f"⚠️ Version cleanup error: {e}")
    
    def get_sync_status(self):
        """Get current sync status for all clients"""
        status = {
            "running": self.sync_running,
            "interval": self.sync_interval,
            "clients_syncing": len([c for c in self.client_configs.values() if c.get("enabled", False)]),
            "total_files": sum(len(files) for files in self.file_metadata.values()),
            "sync_dir": self.base_sync_dir,
            "version_dir": self.version_dir,
            "keep_versions": self.keep_versions,
            "client_stats": dict(self.sync_stats)
        }
        return status
    
    def get_client_sync_info(self, client_key):
        """Get detailed sync info for a specific client"""
        config = self.client_configs.get(client_key, {})
        stats = self.sync_stats.get(client_key, {})
        file_count = len(self.file_metadata.get(client_key, {}))
        
        return {
            "enabled": config.get("enabled", False),
            "source_path": config.get("source_path", ""),
            "last_check": config.get("last_check"),
            "files_tracked": file_count,
            "files_synced": stats.get("files_synced", 0),
            "bytes_synced": stats.get("bytes_synced", 0),
            "content_changes": stats.get("content_changes", 0),
            "new_files": stats.get("new_files", 0),
            "deleted_files": stats.get("deleted_files", 0),
            "last_sync": stats.get("last_sync")
        }
    
    def get_file_history(self, client_key, file_name):
        """Get version history for a specific file"""
        try:
            hostname = client_key.split(":")[0]
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    hostname = handler.client_info.get("hostname", hostname)
            
            version_client_dir = os.path.join(self.version_dir, hostname)
            
            if not os.path.exists(version_client_dir):
                return []
            
            base_name, ext = os.path.splitext(file_name)
            versions = []
            
            for f in os.listdir(version_client_dir):
                if f.startswith(base_name) and f.endswith(ext):
                    full_path = os.path.join(version_client_dir, f)
                    size = os.path.getsize(full_path)
                    mtime = datetime.fromtimestamp(os.path.getmtime(full_path))
                    
                    versions.append({
                        "filename": f,
                        "path": full_path,
                        "size": size,
                        "modified": mtime.strftime("%Y-%m-%d %H:%M:%S")
                    })
            
            # Sort by modification time (newest first)
            versions.sort(key=lambda x: x["modified"], reverse=True)
            
            return versions
        
        except:
            return []

class ResumableFileTransfer:
    def __init__(self, filepath, destination, transfer_id=None):
        self.filepath = filepath
        self.destination = destination
        self.filesize = os.path.getsize(filepath)
        self.basename = os.path.basename(filepath)
        self.transfer_id = transfer_id or self._generate_transfer_id()
        
        if self.filesize > 1024 * 1024 * 1024:
            self.chunk_size = 16 * 1024 * 1024
        elif self.filesize > 100 * 1024 * 1024:
            self.chunk_size = 8 * 1024 * 1024
        else:
            self.chunk_size = 4 * 1024 * 1024
        
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
        self.process_stats = {}  # NEW
        self.last_image_ts = None
        self.connected_time = time.time()
        self.frames_received = 0
        self.bytes_received = 0
        self.last_heartbeat = time.time()
        self.client_info = {"hostname": "Connecting...", "status": "connected"}
        self.is_teacher = False   
    def send_restrictions(self, restrictions):
        try:
            if not self.sock or not self.running.is_set():
                return False
                
            restrictions_data = {
                'keywords': restrictions.get('keywords', []),
                'sites': restrictions.get('sites', [])
            }
            
            cmd = f"RESTRICTIONS:{json.dumps(restrictions_data)}"
            return self.send_command(cmd)
            
        except (ConnectionError, BrokenPipeError):
            print(f"Connection lost with client {self.key}")
            return False
        except Exception as e:
            print(f"Error sending restrictions to {self.key}: {e}")
            return False
    
    def start(self):
        self.thread.start()  # ✅ CORRECT
    
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
                self.sock.setblocking(True)
                self.sock.settimeout(5.0)
                self.sock.sendall(data)
                self.sock.setblocking(False)
                self.sock.settimeout(0.1)  # FIXED: Restore correct timeout for _reader_loop
            self.server.log(f"✉️ Sent to {self.key}: {cmd_str}")
            return True
        except socket.timeout:
            self.server.log(f"⏱️ Send timeout to {self.key}: {cmd_str}")
            return False
        except Exception as e:
            self.server.log(f"❌ Send error to {self.key}: {e}")
            return False
    
    def request_backup(self, source_path, move_files=False):
        try:
            mode = "MOVE" if move_files else "COPY"
            cmd = f"BACKUP:{mode}:{source_path}"
            return self.send_command(cmd)
        except Exception as e:
            self.server.log(f"❌ Backup request error: {e}")
            return False
    
    def send_restore(self, pc_name, restore_path):
        """FIXED: Improved restore with better error handling"""
        try:
            backup_dir = BACKUP_DIR
            
            if not os.path.exists(backup_dir):
                self.server.log(f"❌ Backup directory not found: {backup_dir}")
                return False
            
            try:
                backup_folders = [f for f in os.listdir(backup_dir) 
                                if f.startswith("Backup_") and os.path.isdir(os.path.join(backup_dir, f))]
            except Exception as e:
                self.server.log(f"❌ Error reading backup directory: {e}")
                return False
            
            if not backup_folders:
                self.server.log(f"❌ No backup folders found in {backup_dir}")
                self.server.log(f"💡 Tip: Create a backup first before attempting restore")
                return False
            
            backup_folders.sort(reverse=True)
            latest_backup = backup_folders[0]
            latest_backup_path = os.path.join(backup_dir, latest_backup)
            
            self.server.log(f"📦 Using backup: {latest_backup}")
            
            pc_folder = os.path.join(latest_backup_path, pc_name)
            
            if not os.path.exists(pc_folder):
                self.server.log(f"❌ No backup found for PC: {pc_name}")
                self.server.log(f"📋 Available PCs in {latest_backup}:")
                try:
                    available_pcs = [d for d in os.listdir(latest_backup_path) 
                                   if os.path.isdir(os.path.join(latest_backup_path, d))]
                    for pc in available_pcs:
                        self.server.log(f"   - {pc}")
                except:
                    pass
                return False
            
            files_folder = os.path.join(pc_folder, "files")
            if not os.path.exists(files_folder):
                self.server.log(f"❌ No files folder in backup for: {pc_name}")
                self.server.log(f"💡 Backup structure may be corrupted")
                return False
            
            file_count = sum(len(files) for _, _, files in os.walk(files_folder))
            if file_count == 0:
                self.server.log(f"⚠️ No files found in backup for: {pc_name}")
                return False
            
            temp_zip = os.path.join(RESUME_METADATA_DIR, f"restore_{pc_name}_{int(time.time())}.zip")
            
            self.server.log(f"📦 Creating restore package for {pc_name} ({file_count} files)...")
            
            try:
                with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zipf:
                    files_added = 0
                    for root, dirs, files in os.walk(files_folder):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, files_folder)
                            try:
                                zipf.write(file_path, arcname)
                                files_added += 1
                                if files_added % 100 == 0:
                                    self.server.log(f"📊 Packaged {files_added}/{file_count} files...")
                            except Exception as e:
                                self.server.log(f"⚠️ Skipped {file}: {e}")
                
                if files_added == 0:
                    self.server.log(f"❌ No files could be added to restore package")
                    try:
                        os.remove(temp_zip)
                    except:
                        pass
                    return False
                
            except Exception as e:
                self.server.log(f"❌ Error creating restore package: {e}")
                try:
                    os.remove(temp_zip)
                except:
                    pass
                return False
            
            zip_size = os.path.getsize(temp_zip)
            self.server.log(f"📦 Restore package created: {format_bytes(zip_size)}")
            
            # FIXED: Send restore destination FIRST before file transfer
            cmd = f"RESTORE_START:{restore_path}"
            if not self.send_command(cmd):
                self.server.log(f"❌ Failed to send restore command to {pc_name}")
                try:
                    os.remove(temp_zip)
                except:
                    pass
                return False
            
            # FIXED: Wait longer for client to process command
            time.sleep(1.0)
            
            self.server.log(f"📤 Sending restore package to {pc_name}...")
            success = self.send_file_resumable(temp_zip, "RESTORE_TEMP")
            
            try:
                os.remove(temp_zip)
            except:
                pass
            
            if success:
                self.server.log(f"✅ Restore completed for {pc_name}")
            else:
                self.server.log(f"❌ Restore transfer failed for {pc_name}")
            
            return success
            
        except Exception as e:
            self.server.log(f"❌ Restore error: {e}")
            import traceback
            self.server.log(traceback.format_exc())
            return False
    
    def send_file_resumable(self, filepath, destination=None):
        """OPTIMIZED: Faster resumable file transfer with database tracking"""
        if not os.path.exists(filepath):
            self.server.log(f"❌ File not found: {filepath}")
            return False
        
        if not self.sock or not self.running.is_set():
            self.server.log(f"⚠️ Client {self.key} not connected")
            return False
        
        self.transferring.set()
        time.sleep(0.15)
        
        try:
            transfer = ResumableFileTransfer(filepath, destination or "Downloads")
            basename = transfer.basename
            filesize = transfer.filesize
            chunk_size = transfer.chunk_size
            transfer_id = transfer.transfer_id
            
            self.server.log(f"📤 Starting: {basename} ({format_bytes(filesize)})")
            
            # DATABASE: Create transfer record
            if self.server.db:
                try:
                    file_hash = hashlib.md5()
                    with open(filepath, 'rb') as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            file_hash.update(chunk)
                    
                    self.server.db.create_transfer(
                        transfer_id=transfer_id,
                        filename=basename,
                        file_path=filepath,
                        file_size=filesize,
                        source_type="admin",
                        source_id=self.server.host,
                        dest_type="client",
                        dest_id=self.key,
                        total_chunks=transfer.total_chunks,
                        transfer_type="file_send",
                        file_hash=file_hash.hexdigest()
                    )
                    self.server.log(f"📊 Transfer tracked in database: {transfer_id}")
                except Exception as e:
                    self.server.log(f"⚠️ Database tracking failed: {e}")
            
            pending_chunks = transfer.get_pending_chunks()
            if len(pending_chunks) < transfer.total_chunks:
                self.server.log(f"🔄 Resume: {len(transfer.completed_chunks)}/{transfer.total_chunks} done")
            
            # Configure socket for optimal performance
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_SEND_BUFFER)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_RECV_BUFFER)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception as e:
                self.server.log(f"⚠️ Socket config warning: {e}")
            
            # Send transfer initialization header
            init_header = {
                "command": "RESUMABLE_TRANSFER_START",
                "transfer_id": transfer_id,
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
                except OSError as e:
                    self.server.log(f"❌ Cannot start transfer: {e}")
                    if self.server.db:
                        try:
                            self.server.db.complete_transfer(transfer_id, success=False, 
                                                            error_msg="Connection error")
                        except:
                            pass
                    return False
                
                # Wait for READY confirmation from client
                self.sock.settimeout(2.0)
                buffer = b""
                ready_received = False
                start_wait = time.time()
                
                self.server.log(f"⏳ Waiting for client READY...")
                
                while not ready_received:
                    if (time.time() - start_wait) >= 30:
                        self.server.log(f"❌ Timeout waiting for READY after 30 seconds")
                        if self.server.db:
                            try:
                                self.server.db.complete_transfer(transfer_id, success=False,
                                                                error_msg="Client timeout")
                            except:
                                pass
                        return False
                    
                    try:
                        chunk = self.sock.recv(1024)
                        if not chunk:
                            self.server.log(f"❌ Connection closed while waiting for READY")
                            return False
                        buffer += chunk
                    except socket.timeout:
                        continue
                    except OSError as e:
                        self.server.log(f"⚠️ Socket error waiting for READY: {e}")
                        return False
                    
                    # Process received messages
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        msg = line.decode('utf-8', errors='ignore').strip()
                        
                        if not msg:
                            continue
                        
                        msg_upper = msg.upper()
                        if "READY" in msg_upper:
                            ready_received = True
                            self.server.log(f"✅ READY received - starting chunk transfer")
                            break
                        elif "HEARTBEAT" in msg_upper:
                            continue
                        elif "ERROR" in msg_upper:
                            error_detail = msg.split(":", 1)[1] if ":" in msg else "Unknown"
                            self.server.log(f"❌ Client error: {error_detail}")
                            return False
                    
                    if ready_received:
                        break
                
                if not ready_received:
                    self.server.log(f"❌ READY not received")
                    return False
                
                # Start sending chunks
                self.server.log("✅ Client READY confirmed - Starting chunk transfer")
                self.sock.settimeout(5.0)
                
                start_time = time.time()
                sent_bytes = 0
                last_log = start_time
                chunks_in_batch = []
                chunk_delay = CHUNK_SEND_DELAY
                last_db_update = start_time
                
                with open(filepath, "rb") as f:
                    for idx, chunk_index in enumerate(pending_chunks):
                        if not self.running.is_set():
                            self.server.log(f"⚠️ Transfer stopped")
                            break
                        
                        # Check connection health every 10 chunks
                        if idx > 0 and idx % 10 == 0:
                            try:
                                self.sock.getpeername()
                            except (OSError, socket.error):
                                self.server.log(f"❌ Connection lost at chunk {chunk_index}")
                                transfer.save_progress_batch()
                                return False
                        
                        try:
                            # Read chunk data
                            f.seek(chunk_index * chunk_size)
                            chunk_data = f.read(chunk_size)
                            if not chunk_data:
                                break
                            
                            checksum = transfer._calculate_chunk_checksum(chunk_data)
                            
                            # Build chunk header
                            chunk_header = struct.pack(">II", chunk_index, len(chunk_data))
                            chunk_header += checksum.encode('utf-8').ljust(64, b'\x00')
                            
                            # Send chunk with retry logic
                            max_retries = 3
                            retry_count = 0
                            chunk_sent = False
                            
                            while not chunk_sent and retry_count < max_retries:
                                try:
                                    self.sock.sendall(chunk_header + chunk_data)
                                    chunk_sent = True
                                except (OSError, BrokenPipeError, ConnectionResetError) as e:
                                    retry_count += 1
                                    self.server.log(f"⚠️ Chunk {chunk_index} send failed (attempt {retry_count}/{max_retries}): {e}")
                                    
                                    if retry_count >= max_retries:
                                        self.server.log(f"❌ Failed to send chunk {chunk_index} after {max_retries} retries")
                                        transfer.save_progress_batch()
                                        return False
                                    
                                    time.sleep(0.5 * retry_count)
                                    
                                    if not self.running.is_set():
                                        return False
                            
                            if not chunk_sent:
                                self.server.log(f"❌ Failed to send chunk {chunk_index}")
                                transfer.save_progress_batch()
                                return False
                            
                            if chunk_delay > 0:
                                time.sleep(chunk_delay)
                            
                            sent_bytes += len(chunk_data)
                            chunks_in_batch.append((chunk_index, checksum))
                            
                            # Save chunk status to database
                            if self.server.db:
                                try:
                                    self.server.db.save_chunk_status(
                                        transfer_id=transfer_id,
                                        chunk_index=chunk_index,
                                        chunk_size=len(chunk_data),
                                        chunk_hash=checksum,
                                        status='sent'
                                    )
                                except:
                                    pass
                            
                            # Wait for batch acknowledgment
                            if len(chunks_in_batch) >= BATCH_ACK_SIZE or idx == len(pending_chunks) - 1:
                                self.sock.settimeout(30.0)
                                ack_buffer = b""
                                ack_received = False
                                ack_start = time.time()
                                
                                while not ack_received and (time.time() - ack_start) < 60:
                                    try:
                                        chunk_ack = self.sock.recv(4096)
                                    except socket.timeout:
                                        continue
                                    except OSError:
                                        self.server.log(f"⚠️ Socket closed waiting for ACK")
                                        transfer.save_progress_batch()
                                        return False
                                    
                                    if not chunk_ack:
                                        self.server.log(f"❌ Connection closed during ACK")
                                        return False
                                    
                                    ack_buffer += chunk_ack
                                    while b'\n' in ack_buffer:
                                        line, ack_buffer = ack_buffer.split(b'\n', 1)
                                        msg = line.decode('utf-8', errors='ignore').strip().upper()
                                        
                                        if "CHUNK_OK" in msg or "OK" in msg:
                                            ack_received = True
                                            break
                                        elif "HEARTBEAT" in msg:
                                            continue
                                        elif "CHUNK_ERROR" in msg or "ERROR" in msg:
                                            self.server.log(f"❌ Client reported error at chunk {chunk_index}")
                                            return False
                                
                                if not ack_received:
                                    self.server.log(f"❌ Timeout waiting for ACK at chunk {chunk_index}")
                                    return False
                                
                                # Mark chunks as complete
                                for c_idx, c_sum in chunks_in_batch:
                                    transfer.mark_chunk_complete(c_idx, c_sum)
                                
                                transfer.save_progress_batch()
                                chunks_in_batch = []
                                
                                # Update database progress
                                if time.time() - last_db_update >= 2.0 and self.server.db:
                                    try:
                                        progress = transfer.get_progress()
                                        self.server.db.update_transfer_progress(
                                            transfer_id=transfer_id,
                                            chunks_completed=len(transfer.completed_chunks),
                                            bytes_transferred=sent_bytes,
                                            status='in_progress'
                                        )
                                        last_db_update = time.time()
                                    except Exception as e:
                                        self.server.log(f"⚠️ DB progress update failed: {e}")
                                
                                # Log progress
                                progress = transfer.get_progress()
                                if time.time() - last_log >= 2.0:
                                    elapsed = time.time() - start_time
                                    rate = sent_bytes / elapsed if elapsed > 0 else 0
                                    eta = ((filesize - sent_bytes) / rate) if rate > 0 else 0
                                    self.server.log(f"📊 {basename}: {progress:.1f}% | {format_bytes(rate)}/s | ETA: {int(eta)}s")
                                    last_log = time.time()
                        
                        except Exception as e:
                            self.server.log(f"⚠️ Chunk error: {e}")
                            transfer.save_progress_batch()
                            return False
                
                # Wait for final transfer complete confirmation
                self.sock.settimeout(30.0)
                complete_buffer = b""
                complete_received = False
                complete_start = time.time()
                
                while not complete_received and (time.time() - complete_start) < 30:
                    try:
                        chunk = self.sock.recv(1024)
                    except socket.timeout:
                        elapsed = time.time() - complete_start
                        if int(elapsed) % 5 == 0:
                            self.server.log(f"⏳ Waiting for client confirmation... ({int(elapsed)}s)")
                        continue
                    except OSError:
                        break
                    
                    if not chunk:
                        break
                    
                    complete_buffer += chunk
                    while b'\n' in complete_buffer:
                        line, complete_buffer = complete_buffer.split(b'\n', 1)
                        msg = line.decode('utf-8', errors='ignore').strip().upper()
                        
                        if "TRANSFER_COMPLETE" in msg:
                            complete_received = True
                            break
                        elif "VERIFIED" in msg:
                            continue
                        elif "HEARTBEAT" in msg:
                            continue
                
                self.sock.settimeout(None)
                
                # Calculate transfer statistics
                elapsed = time.time() - start_time
                rate = filesize / elapsed if elapsed > 0 else 0
                speed_mbps = (filesize * 8 / 1_000_000) / elapsed if elapsed > 0 else 0
                
                if complete_received:
                    self.server.log(f"✅ {basename} complete | {format_bytes(rate)}/s | {elapsed:.1f}s")
                    
                    if self.server.db:
                        try:
                            self.server.db.complete_transfer(
                                transfer_id=transfer_id,
                                success=True,
                                transfer_speed=speed_mbps
                            )
                            self.server.log(f"📊 Transfer completed in database")
                        except Exception as e:
                            self.server.log(f"⚠️ DB completion failed: {e}")
                    
                    transfer.cleanup()
                    return True
                else:
                    # No final confirmation but all chunks were ACKed
                    self.server.log(f"⚠️ No completion confirmation (timeout after 30s)")
                    self.server.log(f"📊 All chunks were sent and acknowledged - marking as complete")
                    
                    if self.server.db:
                        try:
                            self.server.db.complete_transfer(
                                transfer_id=transfer_id,
                                success=True,
                                transfer_speed=speed_mbps,
                                error_msg="No final confirmation (timeout)"
                            )
                            self.server.log(f"📊 Transfer marked complete (with timeout note)")
                        except:
                            pass
                    
                    transfer.cleanup()
                    return True
        
        except Exception as e:
            self.server.log(f"❌ Transfer error: {e}")
            import traceback
            self.server.log(traceback.format_exc())
            
            if self.server.db:
                try:
                    self.server.db.complete_transfer(
                        transfer_id=transfer_id,
                        success=False,
                        error_msg=str(e)
                    )
                except:
                    pass
            
            return False
        
        finally:
            self.transferring.clear()
            try:
                self.sock.setblocking(False)
                self.sock.settimeout(0.1)
            except:
                pass
            
    # In ClientHandler class - FIXED _reader_loop (syntax error fix)
    def _reader_loop(self):
        try:
            self.sock.setblocking(False)
            self.sock.settimeout(0.1)
            buffer = b""
            consecutive_errors = 0
            max_consecutive_errors = 10
            
            while self.running.is_set():
                if self.transferring.is_set():
                    time.sleep(0.1)
                    continue

                try:
                    chunk = self.sock.recv(RECV_BUFFER)
                    if not chunk:
                        self.server.log(f"⚠️ Client {self.key} closed connection")
                        break

                    consecutive_errors = 0
                    buffer += chunk
                    self.bytes_received += len(chunk)
                    self.last_heartbeat = time.time()

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

                        if header.upper() == "FRAME":
                            while len(buffer) < 8:
                                chunk = self.sock.recv(RECV_BUFFER)
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

                            while len(buffer) < frame_size:
                                chunk = self.sock.recv(min(RECV_BUFFER, frame_size - len(buffer)))
                                if not chunk:
                                    raise ConnectionError("Connection closed reading frame data")
                                buffer += chunk
                                self.bytes_received += len(chunk)
                                self.last_heartbeat = time.time()
                            
                            frame_data = buffer[:frame_size]
                            buffer = buffer[frame_size:]
                            
                            self._process_frame(frame_data)

                        elif header.upper() == "HEARTBEAT":
                            self.last_heartbeat = time.time()
                            
                        elif header.startswith("TRANSFER_STATUS:"):
                            try:
                                status_json = header.split(":", 1)[1]
                                status_data = json.loads(status_json)
                                
                                if self.server.db:
                                    transfer_id = status_data.get('transfer_id')
                                    status = status_data.get('status')
                                    
                                    if status == 'completed':
                                        self.server.db.complete_transfer(transfer_id, success=True)
                                        self.server.log(f"📊 Transfer {transfer_id} marked complete by client")
                                    elif status == 'failed':
                                        error_msg = status_data.get('error', 'Unknown error')
                                        self.server.db.complete_transfer(transfer_id, success=False, 
                                                                        error_msg=error_msg)
                                        self.server.log(f"📊 Transfer {transfer_id} marked failed by client")
                            except Exception as e:
                                self.server.log(f"⚠️ Transfer status update failed: {e}")
        
                        elif header.upper().startswith("STATUS"):
                            self.server.log(f"📊 {self.key}: {header}")

                        elif header.upper().startswith("MSG"):
                            self.server.log(f"💬 {self.key}: {header}")

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
                                
                                hostname = self.client_info.get("hostname", "Unknown")
                                client_type = self.client_info.get("type", "student")
                                
                                if client_type == "teacher":
                                    self.is_teacher = True
                                    with self.server.teacher_lock:
                                        self.server.teacher_clients[self.key] = True
                                    self.server.log(f"👨‍🏫 Teacher client connected: {hostname} ({self.key})")
                                    threading.Thread(target=self.server.broadcast_client_list_to_teachers, daemon=True).start()
                                    continue
                                else:
                                    self.is_teacher = False
                                
                                # FIXED: Register client connection in database with correct method
                                if self.server.db and self.server.admin_ip:
                                    client_ip = self.key.split(":")[0]
                                    self.server.db.add_or_update_client(
                                        admin_ip=self.server.admin_ip,
                                        admin_username=self.server.admin_user.get('username', 'admin'),
                                        admin_type='admin',
                                        client_ip=client_ip,
                                        client_name=hostname,
                                        client_hostname=hostname
                                    )
                                    self.server.log(f"📊 Client registered in database: {hostname} ({client_ip})")
                                
                                # For student clients: Check for duplicate hostnames
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
                                    self.server.log(f"📋 Student client identified: {hostname} ({self.key})")
                                
                                # Notify teachers in separate thread
                                threading.Thread(target=self.server.broadcast_client_list_to_teachers, daemon=True).start()
                                
                            except Exception as e:
                                self.server.log(f"⚠️ Failed to parse INFO from {self.key}: {e}")

                        elif header.startswith("PROCESS_STATS:"):
                            try:
                                stats_json = header.split(":", 1)[1].strip()
                                
                                try:
                                    stats_data = json.loads(stats_json)
                                except json.JSONDecodeError:
                                    decoder = json.JSONDecoder()
                                    stats_data, idx = decoder.raw_decode(stats_json)
                                
                                with self.server.statistics_lock:
                                    self.server.statistics[self.key] = stats_data
                                    self.process_stats = stats_data
                                
                                self.server.signals.statistics_updated.emit()
                                
                                self.server.log(f"📊 Stats from {self.key}: {len(stats_data)} apps")
                            except Exception as e:
                                self.server.log(f"⚠️ Stats parse error from {self.key}: {e}")

                        elif header.startswith("TEACHER_FILE"):
                            while len(buffer) < 4:
                                chunk = self.sock.recv(RECV_BUFFER)
                                if not chunk:
                                    raise ConnectionError("Connection closed reading metadata length")
                                buffer += chunk
                            
                            metadata_len = struct.unpack(">I", buffer[:4])[0]
                            buffer = buffer[4:]
                            
                            while len(buffer) < metadata_len:
                                chunk = self.sock.recv(min(RECV_BUFFER, metadata_len - len(buffer)))
                                if not chunk:
                                    raise ConnectionError("Connection closed reading metadata")
                                buffer += chunk
                            
                            metadata_json = buffer[:metadata_len]
                            buffer = buffer[metadata_len:]
                            
                            try:
                                metadata = json.loads(metadata_json.decode('utf-8'))
                                filename = metadata.get('filename', 'unknown')
                                filesize = metadata.get('filesize', 0)
                                filepath = metadata.get('filepath', 'unknown')
                                
                                self.server.log(f"📥 Receiving file from teacher: {filename}")
                                self.server.log(f"📁 Source path: {filepath}")
                                
                                file_data = b""
                                remaining = filesize
                                
                                while remaining > 0:
                                    chunk = self.sock.recv(min(RECV_BUFFER, remaining))
                                    if not chunk:
                                        raise ConnectionError("Connection closed reading file data")
                                    file_data += chunk
                                    remaining -= len(chunk)
                                    buffer = b""
                                
                                end_marker = b""
                                while not end_marker.endswith(b"<FILE_END>"):
                                    chunk = self.sock.recv(1024)
                                    if not chunk:
                                        break
                                    end_marker += chunk
                                    if len(end_marker) > 20:
                                        end_marker = end_marker[-20:]
                                
                                save_path = os.path.join(INBOX_DIR, f"teacher_{filename}")
                                with open(save_path, 'wb') as f:
                                    f.write(file_data)
                                
                                metadata_path = save_path + ".meta.json"
                                with open(metadata_path, 'w') as f:
                                    json.dump(metadata, f, indent=2)
                                
                                self.server.log(f"✅ Teacher file saved: {save_path}")
                                self.server.log(f"📋 Original path: {filepath}")
                                self.server.log(f"ℹ️ Metadata saved to: {metadata_path}")
                                
                            except Exception as e:
                                self.server.log(f"❌ Error receiving teacher file: {e}")
                        
                        elif header.startswith("TEACHER_START_PRESENTATION:"):
                            try:
                                targets_json = header.split(":", 1)[1]
                                target_clients = json.loads(targets_json)
                                self.server.log(f"📽️ Teacher {self.key} starting presentation to {len(target_clients)} client(s)")
                                
                                self.teacher_presentation_targets = target_clients
                                
                                with self.server.clients_lock:
                                    for target in target_clients:
                                        target_key = target
                                        if "(" in target and ")" in target:
                                            start = target.rfind("(")
                                            end = target.rfind(")")
                                            target_key = target[start+1:end]
                                        
                                        for client_key, handler in self.server.clients.items():
                                            client_hostname = handler.client_info.get("hostname", "")
                                            
                                            if client_key == target_key or client_hostname in target:
                                                handler.send_command("START_PRESENTATION")
                                                self.server.log(f"   ▸ Presentation started for {client_key} ({client_hostname})")
                                                break
                                
                            except Exception as e:
                                self.server.log(f"❌ Error starting teacher presentation: {e}")

                        elif header.startswith("TEACHER_STOP_PRESENTATION"):
                            self.server.log(f"⏹️ Teacher {self.key} stopped presenting")
                            
                            if hasattr(self, 'teacher_presentation_targets'):
                                with self.server.clients_lock:
                                    for target in self.teacher_presentation_targets:
                                        target_key = target
                                        if "(" in target and ")" in target:
                                            start = target.rfind("(")
                                            end = target.rfind(")")
                                            target_key = target[start+1:end]
                                        
                                        for client_key, handler in self.server.clients.items():
                                            client_hostname = handler.client_info.get("hostname", "")
                                            
                                            if client_key == target_key or client_hostname in target:
                                                handler.send_command("STOP_PRESENTATION")
                                                self.server.log(f"   ▸ Presentation stopped for {client_key} ({client_hostname})")
                                                break
                                
                                delattr(self, 'teacher_presentation_targets')

                        elif header.startswith("TEACHER_PRESENT_FRAME"):
                            while len(buffer) < 8:
                                chunk = self.sock.recv(RECV_BUFFER)
                                if not chunk:
                                    raise ConnectionError("Connection closed")
                                buffer += chunk
                            
                            frame_size = struct.unpack(">Q", buffer[:8])[0]
                            buffer = buffer[8:]

                            if frame_size > 0 and frame_size < MAX_IMAGE_SIZE:
                                while len(buffer) < frame_size:
                                    chunk = self.sock.recv(min(RECV_BUFFER, frame_size - len(buffer)))
                                    if not chunk:
                                        raise ConnectionError("Connection closed")
                                    buffer += chunk
                                
                                frame_data = buffer[:frame_size]
                                buffer = buffer[frame_size:]
                                
                                if hasattr(self, 'teacher_presentation_targets'):
                                    header_to_send = b"PRESENT_FRAME\n" + struct.pack(">Q", frame_size)
                                    
                                    with self.server.clients_lock:
                                        for target in self.teacher_presentation_targets:
                                            target_key = target
                                            if "(" in target and ")" in target:
                                                start = target.rfind("(")
                                                end = target.rfind(")")
                                                target_key = target[start+1:end]
                                            
                                            for client_key, handler in self.server.clients.items():
                                                client_hostname = handler.client_info.get("hostname", "")
                                                
                                                if client_key == target_key or client_hostname in target:
                                                    try:
                                                        with handler.lock:
                                                            handler.sock.sendall(header_to_send + frame_data)
                                                    except Exception as e:
                                                        self.server.log(f"⚠️ Failed to send frame to {client_key}: {e}")
                                                    break
                        
                        elif header.startswith("START_TEACHER_PRESENTATION"):
                            self.server.log(f"📽️ Teacher {self.key} started presenting")
                            
                        elif header.startswith("STOP_TEACHER_PRESENTATION"):
                            self.server.log(f"⏹️ Teacher {self.key} stopped presenting")
                            
                        elif header.startswith("PRESENT_FRAME"):
                            while len(buffer) < 8:
                                chunk = self.sock.recv(RECV_BUFFER)
                                if not chunk:
                                    raise ConnectionError("Connection closed")
                                buffer += chunk
                            
                            frame_size = struct.unpack(">Q", buffer[:8])[0]
                            buffer = buffer[8:]

                            if frame_size > 0 and frame_size < MAX_IMAGE_SIZE:
                                while len(buffer) < frame_size:
                                    chunk = self.sock.recv(min(RECV_BUFFER, frame_size - len(buffer)))
                                    if not chunk:
                                        raise ConnectionError("Connection closed")
                                    buffer += chunk
                                
                                frame_data = buffer[:frame_size]
                                buffer = buffer[frame_size:]
                                self._process_frame(frame_data)
                            
                        elif header.startswith("MONITOR_CLIENT:"):
                            client_key = header.split(":", 1)[1].strip()
                            self.server.log(f"👨‍🏫 Teacher {self.key} requesting to monitor: {client_key}")
                            
                            target_key = client_key
                            if "(" in client_key and ")" in client_key:
                                start = client_key.rfind("(")
                                end = client_key.rfind(")")
                                target_key = client_key[start+1:end].strip()
                            
                            self.monitoring_target = target_key
                            
                            with self.server.clients_lock:
                                if target_key in self.server.clients:
                                    target_handler = self.server.clients[target_key]
                                    target_handler.send_command("START_SCREEN_STREAM")
                                    
                                    if not hasattr(target_handler, 'monitoring_teachers'):
                                        target_handler.monitoring_teachers = set()
                                    target_handler.monitoring_teachers.add(self.key)
                                    
                                    self.server.log(f"✅ Teacher {self.key} now monitoring {target_key}")
                                else:
                                    self.server.log(f"❌ Client {target_key} not found for monitoring")
                                    self.send_command("MONITOR_ERROR:Client not found")

                        elif header.startswith("STOP_MONITOR_CLIENT"):
                            if hasattr(self, 'monitoring_target'):
                                target_key = self.monitoring_target
                                self.server.log(f"👨‍🏫 Teacher {self.key} stopped monitoring {target_key}")
                                
                                with self.server.clients_lock:
                                    if target_key in self.server.clients:
                                        target_handler = self.server.clients[target_key]
                                        if hasattr(target_handler, 'monitoring_teachers'):
                                            target_handler.monitoring_teachers.discard(self.key)
                                            
                                            if len(target_handler.monitoring_teachers) == 0:
                                                target_handler.send_command("STOP_SCREEN_STREAM")
                                                delattr(target_handler, 'monitoring_teachers')
                                
                                delattr(self, 'monitoring_target')
                            
                        elif header.startswith("FILE_LIST:"):
                            try:
                                list_size = int(header.split(":", 1)[1])
                                
                                while len(buffer) < list_size:
                                    chunk = self.sock.recv(min(RECV_BUFFER, list_size - len(buffer)))
                                    if not chunk:
                                        raise ConnectionError("Connection closed reading file list")
                                    buffer += chunk
                                
                                file_list_json = buffer[:list_size].decode('utf-8')
                                buffer = buffer[list_size:]
                                
                                if hasattr(self.server, 'sync_manager'):
                                    self.server.sync_manager.receive_file_list(self.key, file_list_json)
                            
                            except Exception as e:
                                self.server.log(f"❌ Error receiving file list: {e}")

                        elif header.upper() == "ADMIN_FILE":
                            try:
                                while len(buffer) < 4:
                                    chunk = self.sock.recv(RECV_BUFFER)
                                    if not chunk:
                                        raise ConnectionError("Connection closed")
                                    buffer += chunk
                                
                                meta_len = struct.unpack(">I", buffer[:4])[0]
                                buffer = buffer[4:]
                                
                                while len(buffer) < meta_len:
                                    chunk = self.sock.recv(min(RECV_BUFFER, meta_len - len(buffer)))
                                    if not chunk:
                                        raise ConnectionError("Connection closed")
                                    buffer += chunk
                                
                                metadata_json = buffer[:meta_len]
                                buffer = buffer[meta_len:]
                                
                                metadata = json.loads(metadata_json.decode('utf-8'))
                                file_size = metadata.get('size', 0)
                                
                                file_data = b""
                                while len(file_data) < file_size:
                                    chunk = self.sock.recv(min(RECV_BUFFER, file_size - len(file_data)))
                                    if not chunk:
                                        break
                                    file_data += chunk
                                
                                end_buffer = b""
                                while not end_buffer.endswith(b"<END>"):
                                    chunk = self.sock.recv(1024)
                                    if not chunk:
                                        break
                                    end_buffer += chunk
                                    if len(end_buffer) > 10:
                                        end_buffer = end_buffer[-10:]
                                
                                if hasattr(self.server, 'sync_manager'):
                                    self.server.sync_manager.receive_file_from_client(self.key, metadata, file_data)
                            
                            except Exception as e:
                                self.server.log(f"❌ Error receiving admin file: {e}")
                                
                                
                        elif header.startswith("DIR_LISTING:"):
                            try:
                                size = int(header.split(":", 1)[1])
                                while len(buffer) < size:
                                    chunk = self.sock.recv(min(RECV_BUFFER, size - len(buffer)))
                                    if not chunk:
                                        break
                                    buffer += chunk
                                
                                data_json = buffer[:size].decode('utf-8')
                                buffer = buffer[size:]
                                
                                dir_data = json.loads(data_json)
                                self.directory_data = dir_data
                                self.server.log(f"📁 Received directory listing from {self.key}")
                            except Exception as e:
                                self.server.log(f"❌ Directory listing error: {e}")
                                
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
        """Process backup data with database tracking"""
        backup_id = None
        try:
            import re
            hostname = self.client_info.get("hostname", self.key.replace(":", "_"))
            hostname = re.sub(r'[<>:"/\\|?*]', '_', hostname)
            
            backup_dir = getattr(self.server, 'custom_backup_dir', BACKUP_DIR)
            client_folder = os.path.join(backup_dir, hostname)

            if os.path.exists(client_folder):
                shutil.rmtree(client_folder)
            os.makedirs(client_folder, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_path = os.path.join(client_folder, f"backup_{timestamp}.zip")

            total_size = self.expected_backup_size
            bytes_written = 0
            
            # DATABASE: Create backup record
            if self.server.db:
                try:
                    backup_id = f"backup_{hostname}_{timestamp}"
                    self.server.db.create_backup(
                        backup_id=backup_id,
                        operation_type="backup",
                        client_key=self.key,
                        client_hostname=hostname,
                        backup_path=client_folder,
                        backup_name=f"backup_{timestamp}.zip",
                        backup_size=total_size
                    )
                    self.server.log(f"📊 Backup operation tracked: {backup_id}")
                except Exception as e:
                    self.server.log(f"⚠️ Database backup tracking failed: {e}")
            
            self.server.log(f"💾 Saving backup from {hostname} ({format_bytes(total_size)})...")
            
            with open(zip_path, 'wb') as f:
                chunk_size = 1024 * 1024
                buffer_data = self.backup_buffer[:self.expected_backup_size]
                
                for i in range(0, len(buffer_data), chunk_size):
                    chunk = buffer_data[i:i+chunk_size]
                    f.write(chunk)
                    bytes_written += len(chunk)
                    
                    progress = int((bytes_written / total_size) * 100)
                    if progress > 0 and progress % 20 == 0:
                        self.server.log(f"📊 Saving {hostname}: {progress}% ({format_bytes(bytes_written)}/{format_bytes(total_size)})")

            extract_folder = os.path.join(client_folder, "files")
            os.makedirs(extract_folder, exist_ok=True)

            self.server.log(f"📦 Extracting backup from {hostname}...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                file_list = zip_ref.namelist()
                total_files = len(file_list)
                
                for idx, file in enumerate(file_list):
                    zip_ref.extract(file, extract_folder)
                    
                    if (idx + 1) % 100 == 0 or (idx + 1) == total_files:
                        progress = int(((idx + 1) / total_files) * 100)
                        self.server.log(f"📂 Extracting: {progress}% ({idx + 1}/{total_files} files)")

            self.server.log(f"✅ Backup received from {hostname}: {total_files} files ({format_bytes(len(self.backup_buffer))})")
            self.server.log(f"📁 Saved to: {client_folder}")

            # DATABASE: Mark backup complete
            if self.server.db and backup_id:
                try:
                    # Update with file count
                    conn = self.server.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE backup_operations SET file_count = %s WHERE backup_id = %s",
                        (total_files, backup_id)
                    )
                    conn.commit()
                    cursor.close()
                    conn.close()
                    
                    self.server.db.complete_backup(backup_id, success=True)
                    self.server.log(f"📊 Backup completed in database")
                except Exception as e:
                    self.server.log(f"⚠️ Database backup completion failed: {e}")

        except Exception as e:
            self.server.log(f"❌ Failed to process backup: {e}")
            import traceback
            self.server.log(traceback.format_exc())
            
            # DATABASE: Mark backup failed
            if self.server.db and backup_id:
                try:
                    self.server.db.complete_backup(backup_id, success=False, error_msg=str(e))
                except:
                    pass

    def _process_frame(self, data):
        self.last_image = data
        self.last_image_ts = time.time()
        self.frames_received += 1
        
        # FIXED: Add frame to buffer for monitoring
        with self.server.frame_locks[self.key]:
            self.server.frame_buffers[self.key].append(data)
        
        self.server.signals.new_frame.emit(self.key, data)
        
        # Forward frames to any teachers monitoring this client
        if hasattr(self, 'monitoring_teachers') and self.monitoring_teachers:
            frame_size = len(data)
            header_to_send = b"MONITORED_FRAME\n" + struct.pack(">Q", frame_size)
            
            with self.server.clients_lock:
                for teacher_key in list(self.monitoring_teachers):
                    if teacher_key in self.server.clients:
                        teacher_handler = self.server.clients[teacher_key]
                        try:
                            with teacher_handler.lock:
                                teacher_handler.sock.sendall(header_to_send + data)
                        except Exception as e:
                            self.server.log(f"⚠️ Failed to send monitored frame to teacher {teacher_key}: {e}")
                            self.monitoring_teachers.discard(teacher_key)
        
    

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
        self.admin_ip = None
        self.admin_user = None
        
        self.heartbeat_checker_thread = threading.Thread(target=self._heartbeat_checker, daemon=True)
        self.heartbeat_checker_thread.start()
        
        self.frame_buffers = defaultdict(lambda: deque(maxlen=3))
        self.frame_locks = defaultdict(threading.Lock)
        
        self.signals = ServerSignals()
        self.total_connections = 0
        self.start_time = None
        
        self.presenting = False
        self.presentation_thread = None
        
        self.restrictions = self.load_restrictions()
        
        # Teacher client support
        self.teacher_clients = {}
        self.teacher_lock = threading.Lock()
        # Statistics storage (per-client application/process counts)
        self.statistics = {}  # { client_key: {app_name: count, ...}, ... }
        self.statistics_lock = threading.Lock()
        try:
            self.db = FileTransferDB(
                host='localhost',
                port=3306,
                user='root',
                password='',
                database='lab_manager',
                pool_size=5
            )
            if self.db.test_connection():
                self.log("📊 MySQL Database connected successfully")
            else:
                self.log("⚠️ MySQL Database connection test failed")
                self.db = None
        except Exception as e:
            self.log(f"⚠️ MySQL Database initialization failed: {e}")
            self.log("   System will work without database tracking")
            self.db = None
            
    def _heartbeat_checker(self):
        """Check client heartbeats every 20 seconds"""
        while self.running.is_set():
            time.sleep(20)
            
            if not self.admin_ip or not self.db:
                continue
            
            try:
                with self.clients_lock:
                    for key, handler in list(self.clients.items()):
                        client_ip = key.split(":")[0]
                        client_name = handler.client_info.get("hostname", "Unknown")
                        client_type = handler.client_info.get("type", "student")
                        
                        # Only track student clients
                        if client_type != "teacher":
                            # Check if heartbeat is recent (within 60 seconds)
                            if time.time() - handler.last_heartbeat < 60:
                                # Client is alive, update heartbeat
                                self.db.update_client_heartbeat(
                                    self.admin_ip,
                                    client_ip,
                                    client_name
                                )
                            else:
                                # Client missed heartbeat, mark disconnected
                                self.db.mark_client_disconnected(
                                    self.admin_ip,
                                    client_ip
                                )
            except Exception as e:
                self.log(f"Heartbeat checker error: {e}")
            
    def load_restrictions(self):
        """Loads restricted sites from restrictions.txt."""
        try:
            RESTRICTIONS_FILE = "restrictions.txt"  # Make sure this constant is defined
            if os.path.exists(RESTRICTIONS_FILE):
                with open(RESTRICTIONS_FILE, 'r', encoding='utf-8') as f:
                    sites = [line for line in f.read().splitlines() if line.strip()]
                return {"keywords": [], "sites": sites}
        except Exception as e:
            self.log(f"❌ Failed to load restrictions: {e}")
        return {"keywords": [], "sites": []}


    def save_restrictions(self, sites):
        """Saves restricted sites to restrictions.txt."""
        try:
            RESTRICTIONS_FILE = "restrictions.txt"
            # Handle both list and dict formats
            if isinstance(sites, dict):
                sites_list = sites.get("sites", [])
            else:
                sites_list = sites
            
            # Use set to remove duplicates, then sort for consistency
            unique_sites = sorted(set(sites_list))
            
            with open(RESTRICTIONS_FILE, 'w', encoding='utf-8') as f:
                f.write("\n".join(unique_sites))
            
            # Update in-memory restrictions to match saved data
            self.restrictions["sites"] = unique_sites
            
            self.log(f"💾 Restrictions saved: {len(unique_sites)} sites")
            return True
        except Exception as e:
            self.log(f"❌ Failed to save restrictions: {e}")
            return False

    # 🧱 Disable / Enable Incognito
    def _disable_incognito(self):
        if not ctypes.windll.shell32.IsUserAnAdmin():
            QMessageBox.warning(None, "Admin Rights Required", "⚠️ Please run as Administrator to disable incognito.")
            return

        try:
            # Chrome
            chrome = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Policies\\Google\\Chrome")
            winreg.SetValueEx(chrome, "IncognitoModeAvailability", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(chrome)

            # Edge
            edge = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Policies\\Microsoft\\Edge")
            winreg.SetValueEx(edge, "InPrivateModeAvailability", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(edge)

            QMessageBox.information(None, "Incognito Disabled", "🚫 Incognito/InPrivate mode has been disabled.\nRestart browsers to apply.")
        except Exception as e:
            QMessageBox.critical(None, "Error", f"❌ Failed to disable incognito:\n{e}")

    def _enable_incognito(self):
        if not ctypes.windll.shell32.IsUserAnAdmin():
            QMessageBox.warning(None, "Admin Rights Required", "⚠️ Please run as Administrator to enable incognito.")
            return

        try:
            # Chrome
            chrome = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Policies\\Google\\Chrome")
            winreg.DeleteValue(chrome, "IncognitoModeAvailability")
            winreg.CloseKey(chrome)

            # Edge
            edge = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Policies\\Microsoft\\Edge")
            winreg.DeleteValue(edge, "InPrivateModeAvailability")
            winreg.CloseKey(edge)

            QMessageBox.information(None, "Incognito Enabled", "✅ Incognito/InPrivate mode has been re-enabled.\nRestart browsers to apply.")
        except FileNotFoundError:
            QMessageBox.information(None, "Already Enabled", "ℹ️ Incognito/InPrivate mode was already enabled.")
        except Exception as e:
            QMessageBox.critical(None, "Error", f"❌ Failed to enable incognito:\n{e}")
            
    def broadcast_restrictions(self):
        with self.clients_lock:
            clients = list(self.clients.values())
        
        success = 0
        for handler in clients:
            if handler.send_restrictions(self.restrictions):
                success += 1
        
        self.log(f"📢 Restrictions sent to {success}/{len(clients)} clients")
        return success
    
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
            
            # CREATE THE THREAD FIRST
            self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            
            # THEN START IT
            self.accept_thread.start()
            
            self.log(f"🚀 Server started on {self.host}:{self.port}")
            return True
        except Exception as e:
            self.log(f"❌ Failed to start server: {e}")
            return False
    
    def stop(self):
        """Stop client handler and end session"""
        self.running.clear()
        
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            self.sock.close()
        except:
            pass
        
        # DATABASE: End session
        if hasattr(self, 'session_id') and self.server.db:
            try:
                self.server.db.end_session(self.session_id)
                self.server.log(f"📊 Session ended: {self.session_id}")
            except Exception as e:
                self.server.log(f"⚠️ Session end failed: {e}")
    
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
                
                QTimer.singleShot(500, lambda h=handler: h.send_restrictions(self.restrictions))
            
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
            
            # Handle teacher client removal
            with self.teacher_lock:
                if key in self.teacher_clients:
                    del self.teacher_clients[key]
                    self.log(f"👨‍🏫 Teacher client removed: {key}")
                else:
                    # Regular client disconnected, notify teachers in separate thread
                    threading.Thread(target=self.broadcast_client_list_to_teachers, daemon=True).start()
                self.log(f"🗑️ Removed client: {key} (Remaining: {len(self.clients)})")
    
    def broadcast_command(self, cmd_str):
        with self.clients_lock:
            clients = list(self.clients.values())
        
        success = 0
        for handler in clients:
            if handler.send_command(cmd_str):
                success += 1
        
        self.log(f"📢 Broadcast '{cmd_str}' to {success}/{len(clients)} clients")
    
    def request_backup_from_clients(self, client_keys, source_path, move_files=False):
        success_count = 0
        with self.clients_lock:
            for key in client_keys:
                if key in self.clients:
                    if self.clients[key].request_backup(source_path, move_files):
                        success_count += 1
        return success_count
    
    def restore_to_clients(self, client_keys, restore_path, backup_folders=None):
        success_count = 0
        
        with self.clients_lock:
            for key in client_keys:
                if key in self.clients:
                    handler = self.clients[key]
                    hostname = handler.client_info.get("hostname", key.replace(":", "_"))
                    
                    if backup_folders and hostname not in backup_folders:
                        self.log(f"⚠️ No backup found for {hostname}, skipping")
                        continue
                    
                    if handler.send_restore(hostname, restore_path):
                        success_count += 1
                    else:
                        self.log(f"❌ Restore failed for {hostname}")
        
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
    

    def broadcast_client_list_to_teachers(self):
        """Send the list of regular clients to all connected teacher clients"""
        try:
            with self.teacher_lock:
                if not self.teacher_clients:
                    self.log("No teacher clients connected - skipping broadcast")
                    return
                teacher_count = len(self.teacher_clients)
            
            # Get list of regular clients (not teachers)
            regular_clients = []
            with self.clients_lock:
                for key, handler in self.clients.items():
                    with self.teacher_lock:
                        if key not in self.teacher_clients:
                            hostname = handler.client_info.get("hostname", key)
                            regular_clients.append(f"{hostname} ({key})")
            
            self.log(f"📋 Broadcasting {len(regular_clients)} clients to {teacher_count} teacher(s)")
            
            client_list_json = json.dumps(regular_clients)
            
            with self.teacher_lock:
                teacher_keys = list(self.teacher_clients.keys())
            
            success_count = 0
            for teacher_key in teacher_keys:
                try:
                    with self.clients_lock:
                        if teacher_key in self.clients:
                            handler = self.clients[teacher_key]
                            if handler.send_command(f"CLIENT_LIST:{client_list_json}"):
                                success_count += 1
                                self.log(f"✅ Sent client list to teacher {teacher_key}")
                            else:
                                self.log(f"⚠️ Failed to send to teacher {teacher_key}")
                except Exception as e:
                    self.log(f"❌ Error sending to teacher {teacher_key}: {e}")
            
            self.log(f"Broadcast complete: {success_count}/{teacher_count} teachers notified")
                    
        except Exception as e:
            self.log(f"❌ Error broadcasting client list: {e}")
            import traceback
            self.log(traceback.format_exc())

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


    
    def get_statistics_summary(self):
        """Get aggregated statistics from database or memory"""
        # Try database first
        if self.db:
            try:
                stats = self.db.get_top_transferred_files(limit=20)
                if stats:
                    self.log("📊 Statistics loaded from database")
                    return [(s['filename'], s['transfer_count']) for s in stats]
            except Exception as e:
                self.log(f"⚠️ Database stats failed, using memory: {e}")
        
        # Fallback to in-memory statistics (process/app statistics)
        summary = defaultdict(int)
        with self.statistics_lock:
            for client_stats in self.statistics.values():
                for app_name, count in client_stats.items():
                    summary[app_name] += count
        return sorted(summary.items(), key=lambda x: x[1], reverse=True)[:20]
    
    def get_client_statistics(self, client_key):
        """Get per-client statistics from database or memory"""
        # Try database first
        if self.db:
            try:
                db_stats = self.db.get_client_transfer_stats(client_key)
                if db_stats and db_stats.get('total_transfers', 0) > 0:
                    # Format for display
                    return [
                        ("Total Transfers", db_stats.get('total_transfers', 0)),
                        ("Successful", db_stats.get('successful', 0)),
                        ("Failed", db_stats.get('failed', 0)),
                        ("Total Data", format_bytes(db_stats.get('total_bytes', 0) or 0)),
                        ("Avg Speed", f"{db_stats.get('avg_speed', 0) or 0:.1f} Mbps")
                    ]
            except Exception as e:
                self.log(f"⚠️ Database client stats failed: {e}")
        
        # Fallback to in-memory process statistics
        with self.statistics_lock:
            stats = self.statistics.get(client_key, {})
            return sorted(stats.items(), key=lambda x: x[1], reverse=True)[:10]

class AdminWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # Initialize database first
        try:
            self.db = FileTransferDB(
                host='localhost',
                port=3306,
                user='root',
                password='',
                database='lab_manager',
                pool_size=5
            )
            if not self.db.test_connection():
                QMessageBox.critical(
                    None,
                    "Database Error",
                    "Cannot connect to MySQL database.\n\n"
                    "Please ensure:\n"
                    "1. XAMPP MySQL is running\n"
                    "2. Database 'lab_manager' exists\n"
                    "3. SQL schema has been imported"
                )
                sys.exit(1)
        except Exception as e:
            QMessageBox.critical(
                None,
                "Database Error",
                f"Database initialization failed:\n{e}"
            )
            sys.exit(1)
        
        # Show login dialog
        login_dialog = LoginDialog(self.db)
        if login_dialog.exec_() != QDialog.Accepted:
            sys.exit(0)
        
        # Get authenticated user
        self.admin_user = login_dialog.user_data
        self.user_type = login_dialog.user_type
        
        if self.user_type != "admin":
            QMessageBox.critical(
                None,
                "Access Denied",
                "This application requires admin privileges"
            )
            sys.exit(1)
        
        # Get admin IP
        try:
            hostname = socket.gethostname()
            self.admin_ip = socket.gethostbyname(hostname)
        except:
            self.admin_ip = "127.0.0.1"
        
        # Initialize server
        self.server = AdminServer()
        self.server.db = self.db
        self.server.admin_ip = self.admin_ip
        self.server.admin_user = self.admin_user
        self.server.sync_manager = FileSyncManager(self.server)
        self.setWindowTitle("Lab Manager - Admin Server")
        self.resize(1600, 900)
        
        # NEW: HandyCafe-style dark theme
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1a1a1a;
                color: #e0e0e0;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QPushButton {
                background-color: #2d2d2d;
                color: white;
                border: 1px solid #404040;
                padding: 12px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                border: 1px solid #0078d4;
            }
            QPushButton:pressed {
                background-color: #0078d4;
            }
            QPushButton:disabled {
                background-color: #1a1a1a;
                color: #666;
                border: 1px solid #2a2a2a;
            }
            QListWidget, QTextEdit {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
            }
            QListWidget::item {
                padding: 8px;
                border-radius: 3px;
            }
            QListWidget::item:selected {
                background-color: #094771;
            }
            QListWidget::item:hover {
                background-color: #2a2d2e;
            }
            QGroupBox {
                border: 1px solid #3c3c3c;
                border-radius: 5px;
                margin-top: 12px;
                padding-top: 12px;
                padding-bottom: 8px;
                font-weight: bold;
                font-size: 11px;
                color: #4EC9B0;
            }
            QLabel {
                color: #e0e0e0;
            }
            QFrame {
                border: none;
            }
            QScrollArea {
                border: none;
                background-color: #1a1a1a;
            }
        """)
        
        self.server = AdminServer()
        self.server.sync_manager = FileSyncManager(self.server)
        self.selected_preview_client = None
        self.view_mode = "monitor"  # "monitor" or "clients"
        
        self._build_ui()
        self._start_timers()
    def configure_auto_sync(self):
        """Configure auto-sync settings"""
        # Ask for source path
        templates = [
            "C:\\Users\\Student\\Documents",
            "C:\\Users\\Student\\Desktop",
            "C:\\Users\\Student\\Downloads",
            "Custom Path..."
        ]
        
        source_path, ok = QInputDialog.getItem(
            self, "Auto-Sync Configuration",
            "Select folder to monitor on clients:",
            templates, 0, False
        )
        
        if not ok:
            return
        
        if source_path == "Custom Path...":
            source_path, ok = QInputDialog.getText(
                self, "Custom Path",
                "Enter full path to monitor:",
                text="C:\\Users\\Student\\Documents"
            )
            if not ok or not source_path:
                return
        
        # Ask for sync interval
        interval, ok = QInputDialog.getInt(
            self, "Sync Interval",
            "Check for changes every (seconds):",
            30, 10, 3600, 10
        )
        
        if not ok:
            return
        
        # Enable for all clients or selected?
        reply = QMessageBox.question(
            self, "Enable Auto-Sync",
            f"Enable auto-sync for:\n\n"
            f"Path: {source_path}\n"
            f"Interval: Every {interval} seconds\n\n"
            f"Enable for ALL clients or SELECTED clients only?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes
        )
        
        if reply == QMessageBox.Cancel:
            return
        
        # Initialize sync manager if not exists
        if not hasattr(self.server, 'sync_manager'):
            self.server.sync_manager = FileSyncManager(self.server)
        
        self.server.sync_manager.set_sync_interval(interval)
        
        if reply == QMessageBox.Yes:
            # Enable for all
            self.server.sync_manager.enable_auto_sync_all(source_path)
            QMessageBox.information(
                self, "Auto-Sync Enabled",
                f"Auto-sync enabled for ALL connected clients\n\n"
                f"Monitoring: {source_path}\n"
                f"Check interval: {interval}s\n\n"
                f"Files will be automatically backed up to:\n"
                f"{self.server.sync_manager.base_sync_dir}"
            )
        else:
            # Enable for selected
            keys = self._get_selected_keys()
            if not keys:
                QMessageBox.warning(self, "No Selection", "Please select clients first")
                return
            
            for key in keys:
                self.server.sync_manager.enable_auto_sync_for_client(key, source_path)
            
            QMessageBox.information(
                self, "Auto-Sync Enabled",
                f"Auto-sync enabled for {len(keys)} selected client(s)\n\n"
                f"Monitoring: {source_path}\n"
                f"Check interval: {interval}s"
            )
        
        self._update_autosync_ui()

    def toggle_auto_sync(self):
        """Start/stop auto-sync"""
        if not hasattr(self.server, 'sync_manager'):
            QMessageBox.warning(
                self, "Not Configured",
                "Please configure auto-sync first"
            )
            return
        
        if self.server.sync_manager.sync_running:
            self.server.sync_manager.stop_sync_cycle()
            self.btn_toggle_autosync.setText("▶️ Start Auto-Sync")
            self.lbl_autosync_status.setText("⚫ Auto-Sync: Stopped")
            self.lbl_autosync_status.setStyleSheet("color: #888; padding: 5px;")
        else:
            self.server.sync_manager.start_sync_cycle()
            self.btn_toggle_autosync.setText("⏹️ Stop Auto-Sync")
            self.lbl_autosync_status.setText("🟢 Auto-Sync: Running")
            self.lbl_autosync_status.setStyleSheet("color: #90ee90; padding: 5px;")

    def show_sync_status(self):
        """Show detailed sync status with change tracking"""
        if not hasattr(self.server, 'sync_manager'):
            QMessageBox.information(
                self, "Auto-Sync Status",
                "Auto-sync is not configured"
            )
            return
        
        status = self.server.sync_manager.get_sync_status()
        
        status_text = f"""
    <h3>🔄 Auto-Sync Status</h3>
    <p><b>Status:</b> {'🟢 Running' if status['running'] else '⚫ Stopped'}</p>
    <p><b>Check Interval:</b> {status['interval']} seconds</p>
    <p><b>Clients Syncing:</b> {status['clients_syncing']}</p>
    <p><b>Total Files Tracked:</b> {status['total_files']}</p>
    <p><b>Backup Location:</b> {status['sync_dir']}</p>
    <p><b>Version History:</b> {status['version_dir']}</p>
    <p><b>Versions Kept:</b> {status['keep_versions']} per file</p>
    <hr>
    <h4>📊 Client Statistics:</h4>
    """
        
        for client_key, stats in status.get('client_stats', {}).items():
            hostname = client_key.split(":")[0]
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    hostname = handler.client_info.get("hostname", hostname)
            
            status_text += f"""
    <p><b>💻 {hostname}:</b><br>
    &nbsp;&nbsp;📝 Content changes detected: {stats.get('content_changes', 0)}<br>
    &nbsp;&nbsp;🆕 New files: {stats.get('new_files', 0)}<br>
    &nbsp;&nbsp;🗑️ Deleted files: {stats.get('deleted_files', 0)}<br>
    &nbsp;&nbsp;💾 Total files synced: {stats.get('files_synced', 0)}<br>
    &nbsp;&nbsp;📦 Data synced: {format_bytes(stats.get('bytes_synced', 0))}<br>
    &nbsp;&nbsp;🕒 Last sync: {stats.get('last_sync', 'Never')}</p>
    """
        
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Auto-Sync Status")
        msg_box.setTextFormat(Qt.RichText)
        msg_box.setText(status_text)
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.setIcon(QMessageBox.Information)
        
        # Add button to view version history
        btn_view_versions = msg_box.addButton("📚 View File Versions", QMessageBox.ActionRole)
        
        result = msg_box.exec_()
        
        if msg_box.clickedButton() == btn_view_versions:
            self.show_file_versions()
            
            
    def show_file_versions(self):
        """Show file version history browser"""
        if not hasattr(self.server, 'sync_manager'):
            return
        
        # Create dialog
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QLabel, QPushButton, QTextEdit
        
        dialog = QDialog(self)
        dialog.setWindowTitle("📚 File Version History")
        dialog.resize(800, 600)
        dialog.setStyleSheet(self.styleSheet())
        
        layout = QVBoxLayout(dialog)
        
        title = QLabel("Select a client to view file versions:")
        title.setFont(QFont("Segoe UI", 12, QFont.Bold))
        layout.addWidget(title)
        
        # Client selection
        client_list = QListWidget()
        client_list.setMaximumHeight(150)
        
        for client_key in self.server.list_clients():
            hostname = client_key.split(":")[0]
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    handler = self.server.clients[client_key]
                    hostname = handler.client_info.get("hostname", hostname)
            
            item = client_list.addItem(f"💻 {hostname} ({client_key})")
            item = client_list.item(client_list.count() - 1)
            item.setData(Qt.UserRole, client_key)
        
        layout.addWidget(client_list)
        
        # Version info display
        info_label = QLabel("Select a client to see tracked files")
        layout.addWidget(info_label)
        
        version_text = QTextEdit()
        version_text.setReadOnly(True)
        version_text.setFont(QFont("Consolas", 9))
        layout.addWidget(version_text)
        
        def show_client_versions():
            selected = client_list.currentItem()
            if not selected:
                return
            
            client_key = selected.data(Qt.UserRole)
            
            # Get all tracked files for this client
            metadata = self.server.sync_manager.file_metadata.get(client_key, {})
            
            if not metadata:
                version_text.setText("No files tracked for this client yet.")
                return
            
            output = f"📁 Files tracked for this client: {len(metadata)}\n\n"
            
            for file_path, meta in metadata.items():
                file_name = meta.get("name", os.path.basename(file_path))
                version = meta.get("version", 1)
                size = meta.get("size", 0)
                last_update = meta.get("last_update_str", "Unknown")
                file_hash = meta.get("hash", "")[:16]
                
                output += f"📄 {file_name}\n"
                output += f"   Current Version: v{version}\n"
                output += f"   Size: {format_bytes(size)}\n"
                output += f"   Hash: {file_hash}...\n"
                output += f"   Last Updated: {last_update}\n"
                
                # Get version history
                versions = self.server.sync_manager.get_file_history(client_key, file_name)
                
                if versions:
                    output += f"   📚 Version History: {len(versions)} backup(s)\n"
                    for v in versions[:5]:  # Show last 5 versions
                        output += f"      • {v['filename']} - {format_bytes(v['size'])} - {v['modified']}\n"
                
                output += "\n"
            
            version_text.setText(output)
            info_label.setText(f"✅ Showing {len(metadata)} tracked file(s)")
        
        client_list.currentItemChanged.connect(lambda: show_client_versions())
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dialog.close)
        btn_layout.addWidget(btn_close)
        
        layout.addLayout(btn_layout)
        
        dialog.exec_()

    def _update_autosync_ui(self):
        """Update auto-sync UI status"""
        if hasattr(self.server, 'sync_manager'):
            if self.server.sync_manager.sync_running:
                self.lbl_autosync_status.setText("🟢 Auto-Sync: Running")
                self.lbl_autosync_status.setStyleSheet("color: #90ee90; padding: 5px;")
                self.btn_toggle_autosync.setText("⏹️ Stop Auto-Sync")
            else:
                self.lbl_autosync_status.setText("⚫ Auto-Sync: Stopped")
                self.lbl_autosync_status.setStyleSheet("color: #888; padding: 5px;")
                self.btn_toggle_autosync.setText("▶️ Start Auto-Sync")
                
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # NEW: Left panel (HandyCafe style)
        left_panel = self._create_left_panel()
        left_panel.setFixedWidth(300)  # FIXED: Set fixed width
        main_layout.addWidget(left_panel)
        
        # NEW: Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet("background-color: #3c3c3c;")
        divider.setFixedWidth(1)
        main_layout.addWidget(divider)
        
        # NEW: Right panel (switchable view)
        right_panel = self._create_right_panel()
        main_layout.addWidget(right_panel, 1)
    def _create_pc_icon(self):
        """Create a PC icon for the client list"""
        from PyQt5.QtGui import QPainter, QColor, QBrush, QPen, QFont
        
        # Create a 48x48 PC icon
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw monitor (main rectangle)
        painter.setBrush(QBrush(QColor(70, 130, 180)))  # Steel blue
        painter.setPen(QPen(QColor(40, 90, 140), 2))
        painter.drawRoundedRect(4, 4, 40, 28, 3, 3)
        
        # Draw monitor stand
        painter.setBrush(QBrush(QColor(60, 60, 60)))
        painter.setPen(QPen(QColor(40, 40, 40), 1))
        painter.drawRect(20, 32, 8, 6)
        painter.drawRect(14, 38, 20, 4)
        
        # Draw screen (lighter blue)
        painter.setBrush(QBrush(QColor(100, 160, 210)))
        painter.setPen(Qt.NoPen)
        painter.drawRect(8, 8, 32, 20)
        
        # Draw power indicator (green dot)
        painter.setBrush(QBrush(QColor(0, 255, 0)))
        painter.drawEllipse(38, 28, 4, 4)
        
        painter.end()
        
        return QIcon(pixmap)

    def _create_left_panel(self):
        """NEW: Create left control panel (HandyCafe style) - WITH SCROLL AREA"""
        # Main panel container
        panel = QWidget()
        panel.setFixedWidth(350)
        panel.setStyleSheet("background-color: #1e1e1e;")
        
        # Create scroll area
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: #1e1e1e;
            }
            QScrollBar:vertical {
                background-color: #2d2d2d;
                width: 12px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #555;
                min-height: 20px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #666;
            }
        """)
        
        # Inner widget that will contain all the controls
        inner_widget = QWidget()
        inner_widget.setStyleSheet("background-color: #1e1e1e;")
        layout = QVBoxLayout(inner_widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)  # Increased spacing
        
        # Header
        header = QLabel("🎓 Lab Manager")
        header.setFont(QFont("Segoe UI", 16, QFont.Bold))
        header.setStyleSheet("color: #0078d4; padding: 10px;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)
        
        # Server status
        self.lbl_server_status = QLabel("⚫ Server: Stopped")
        self.lbl_server_status.setStyleSheet("background-color: #2d2d2d; padding: 10px; border-radius: 5px;")
        self.lbl_server_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_server_status)
        
        # Server controls
        server_group = QGroupBox("Server Controls")
        server_layout = QVBoxLayout(server_group)
        
        self.btn_start_server = QPushButton("▶️ Start Server")
        self.btn_start_server.clicked.connect(self.start_server)
        server_layout.addWidget(self.btn_start_server)
        
        self.btn_stop_server = QPushButton("⏹️ Stop Server")
        self.btn_stop_server.clicked.connect(self.stop_server)
        self.btn_stop_server.setEnabled(False)
        server_layout.addWidget(self.btn_stop_server)
        
        layout.addWidget(server_group)
        
        # Stats
        stats_group = QGroupBox("Statistics")
        stats_layout = QVBoxLayout(stats_group)
        
        self.lbl_clients_count = QLabel("👥 Clients: 0")
        stats_layout.addWidget(self.lbl_clients_count)
        
        self.lbl_uptime = QLabel("⏱️ Uptime: 00:00:00")
        stats_layout.addWidget(self.lbl_uptime)
        
        layout.addWidget(stats_group)
        
        # Control Actions
        control_group = QGroupBox("Quick Actions")
        control_layout = QVBoxLayout(control_group)
        
        btn_lock_all = QPushButton("🔒 Lock All")
        btn_lock_all.clicked.connect(lambda: self.server.broadcast_command("LOCK"))
        control_layout.addWidget(btn_lock_all)
        
        btn_unlock_all = QPushButton("🔓 Unlock All")
        btn_unlock_all.clicked.connect(lambda: self.server.broadcast_command("UNLOCK"))
        control_layout.addWidget(btn_unlock_all)
        
        # NEW: Shutdown/Restart buttons
        btn_shutdown_all = QPushButton("⚠️ Shutdown All PCs")
        btn_shutdown_all.clicked.connect(self.shutdown_all_clients)
        btn_shutdown_all.setStyleSheet("background-color: #d13438;")
        control_layout.addWidget(btn_shutdown_all)
        
        btn_restart_all = QPushButton("🔄 Restart All PCs")
        btn_restart_all.clicked.connect(self.restart_all_clients)
        btn_restart_all.setStyleSheet("background-color: #ff8c00;")
        control_layout.addWidget(btn_restart_all)
        
        layout.addWidget(control_group)
        
        # File Transfer
        file_group = QGroupBox("File Transfer")
        file_layout = QVBoxLayout(file_group)
        
        # Send File Section
        send_label = QLabel("Send File:")
        send_label.setStyleSheet("color: #4EC9B0; font-size: 10px; font-weight: bold; margin-top: 5px;")
        file_layout.addWidget(send_label)
        
        send_layout = QHBoxLayout()
        btn_send_file = QPushButton("📤 Selected")
        btn_send_file.clicked.connect(self.send_file_to_selected)
        send_layout.addWidget(btn_send_file)
        
        btn_send_file_all = QPushButton("📤 All Clients")
        btn_send_file_all.clicked.connect(self.send_file_to_all)
        btn_send_file_all.setStyleSheet("background-color: #107c10;")
        send_layout.addWidget(btn_send_file_all)
        file_layout.addLayout(send_layout)
        
        # Backup Section
        backup_label = QLabel("Backup:")
        backup_label.setStyleSheet("color: #4EC9B0; font-size: 10px; font-weight: bold; margin-top: 5px;")
        file_layout.addWidget(backup_label)
        
        backup_layout = QHBoxLayout()
        btn_backup = QPushButton("💾 Selected")
        btn_backup.clicked.connect(self.backup_client_files)
        backup_layout.addWidget(btn_backup)
        
        btn_backup_all = QPushButton("💾 All Clients")
        btn_backup_all.clicked.connect(self.backup_all_clients)
        btn_backup_all.setStyleSheet("background-color: #107c10;")
        backup_layout.addWidget(btn_backup_all)
        file_layout.addLayout(backup_layout)
        
        # Restore Section
        restore_label = QLabel("Restore:")
        restore_label.setStyleSheet("color: #4EC9B0; font-size: 10px; font-weight: bold; margin-top: 5px;")
        file_layout.addWidget(restore_label)
        
        restore_layout = QHBoxLayout()
        btn_restore = QPushButton("📥 Selected")
        btn_restore.clicked.connect(self.restore_client_files)
        restore_layout.addWidget(btn_restore)
        
        btn_restore_all = QPushButton("📥 All Clients")
        btn_restore_all.clicked.connect(self.restore_all_clients)
        btn_restore_all.setStyleSheet("background-color: #107c10;")
        restore_layout.addWidget(btn_restore_all)
        file_layout.addLayout(restore_layout)
        
        layout.addWidget(file_group)
        
        # Auto-Sync Controls
        autosync_group = QGroupBox("🔄 Auto-Sync Backup")
        autosync_layout = QVBoxLayout(autosync_group)

        self.lbl_autosync_status = QLabel("⚫ Auto-Sync: Disabled")
        self.lbl_autosync_status.setStyleSheet("color: #888; padding: 5px;")
        self.lbl_autosync_status.setAlignment(Qt.AlignCenter)
        autosync_layout.addWidget(self.lbl_autosync_status)

        btn_configure_autosync = QPushButton("⚙️ Configure Auto-Sync")
        btn_configure_autosync.clicked.connect(self.configure_auto_sync)
        autosync_layout.addWidget(btn_configure_autosync)

        self.btn_toggle_autosync = QPushButton("▶️ Start Auto-Sync")
        self.btn_toggle_autosync.clicked.connect(self.toggle_auto_sync)
        autosync_layout.addWidget(self.btn_toggle_autosync)

        btn_autosync_status = QPushButton("📊 View Sync Status")
        btn_autosync_status.clicked.connect(self.show_sync_status)
        autosync_layout.addWidget(btn_autosync_status)

        layout.addWidget(autosync_group)

        # Monitoring
        monitor_group = QGroupBox("Monitoring")
        monitor_layout = QVBoxLayout(monitor_group)
        
        btn_start_stream = QPushButton("▶️ Start Live View")
        btn_start_stream.clicked.connect(lambda: self.send_to_selected("START_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_start_stream)
        
        btn_stop_stream = QPushButton("⏹️ Stop Live View")
        btn_stop_stream.clicked.connect(lambda: self.send_to_selected("STOP_SCREEN_STREAM"))
        monitor_layout.addWidget(btn_stop_stream)
        
        self.btn_present = QPushButton("📽️ Present My Screen")
        self.btn_present.clicked.connect(self.toggle_presentation)
        monitor_layout.addWidget(self.btn_present)
        
        layout.addWidget(monitor_group)
        
        # Additional Actions
        actions_group = QGroupBox("Additional Actions")
        actions_layout = QVBoxLayout(actions_group)
        
        btn_message = QPushButton("💬 Send Message")
        btn_message.clicked.connect(self.send_message_to_selected)
        actions_layout.addWidget(btn_message)
        
        btn_broadcast = QPushButton("📢 Broadcast Message")
        btn_broadcast.clicked.connect(self.broadcast_message)
        actions_layout.addWidget(btn_broadcast)
        
        btn_restrictions = QPushButton("🚫 Manage Restrictions")
        btn_restrictions.clicked.connect(self.manage_restrictions)
        actions_layout.addWidget(btn_restrictions)
        
        self.restriction_indicator = QLabel("No restrictions")
        self.restriction_indicator.setStyleSheet("color: #888; font-size: 10px; padding: 5px;")
        self.restriction_indicator.setAlignment(Qt.AlignCenter)
        actions_layout.addWidget(self.restriction_indicator)
        
        layout.addWidget(actions_group)
        
        layout.addStretch()

        teacher_group = QGroupBox("👩‍🏫 Teacher Management")
        teacher_layout = QVBoxLayout(teacher_group)

        btn_manage_teachers = QPushButton("Manage Teachers")
        btn_manage_teachers.clicked.connect(self.show_teacher_management)
        teacher_layout.addWidget(btn_manage_teachers)

        layout.addWidget(teacher_group)
        
        # View Tabs
        tabs_group = QGroupBox("View")
        tabs_layout = QGridLayout(tabs_group)
        tabs_layout.setSpacing(10)

        self.btn_view_monitor = QPushButton("📺 Monitor")
        self.btn_view_monitor.clicked.connect(lambda: self.switch_view("monitor"))
        self.btn_view_monitor.setStyleSheet("background-color: #0078d4;")
        tabs_layout.addWidget(self.btn_view_monitor, 0, 0)

        self.btn_view_clients = QPushButton("👥 Clients")
        self.btn_view_clients.clicked.connect(lambda: self.switch_view("clients"))
        tabs_layout.addWidget(self.btn_view_clients, 0, 1)

        self.btn_view_logs = QPushButton("📋 Logs")
        self.btn_view_logs.clicked.connect(lambda: self.switch_view("logs"))
        tabs_layout.addWidget(self.btn_view_logs, 1, 0)

        self.btn_view_stats = QPushButton("📊 Statistics")
        self.btn_view_stats.clicked.connect(lambda: self.switch_view("statistics"))
        tabs_layout.addWidget(self.btn_view_stats, 1, 1)

        layout.addWidget(tabs_group)
        
        # Set the inner widget to the scroll area
        scroll_area.setWidget(inner_widget)
        
        # Create the panel's main layout and add scroll area
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        panel_layout.addWidget(scroll_area)
        
        return panel
    
    def _create_right_panel(self):
        """NEW: Create right panel with stackable views"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Create stacked views
        from PyQt5.QtWidgets import QStackedWidget
        self.stacked_views = QStackedWidget()
        
        # Monitor view
        monitor_view = self._create_monitor_view()
        self.stacked_views.addWidget(monitor_view)
        
        # Clients view
        clients_view = self._create_clients_view()
        self.stacked_views.addWidget(clients_view)
        
        # Logs view
        logs_view = self._create_logs_view()
        self.stacked_views.addWidget(logs_view)

        # Statistics view
        stats_view = self._create_statistics_view()
        self.stacked_views.addWidget(stats_view)
        
        
        layout.addWidget(self.stacked_views)
        
        return panel
    
    def _create_monitor_view(self):
        """Create monitor preview view"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        title = QLabel("📺 Live Screen Preview")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)
        
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
        
        controls.addStretch()
        
        self.lbl_preview_info = QLabel("No client selected")
        controls.addWidget(self.lbl_preview_info)
        
        layout.addLayout(controls)
        
        return widget
    
    def _create_clients_view(self):
        """Create clients view with grid layout - UPDATED with manual refresh"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        title = QLabel("👥 Connected Clients")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)
        
        # View mode toggle
        view_toggle_layout = QHBoxLayout()
        view_toggle_layout.addStretch()
        
        self.btn_grid_view = QPushButton("🔲 Grid View")
        self.btn_grid_view.setFixedSize(120, 35)
        self.btn_grid_view.clicked.connect(lambda: self.switch_client_view("grid"))
        view_toggle_layout.addWidget(self.btn_grid_view)
        
        self.btn_list_view = QPushButton("📋 List View")
        self.btn_list_view.setFixedSize(120, 35)
        self.btn_list_view.clicked.connect(lambda: self.switch_client_view("list"))
        view_toggle_layout.addWidget(self.btn_list_view)
        
        layout.addLayout(view_toggle_layout)
        
        # Stacked widget for both views
        from PyQt5.QtWidgets import QStackedWidget
        self.client_view_stack = QStackedWidget()
        
        # GRID VIEW
        grid_scroll = QScrollArea()
        grid_scroll.setWidgetResizable(True)
        grid_scroll.setStyleSheet("""
            QScrollArea { 
                border: none; 
                background-color: #1a1a1a;
            }
            QScrollBar:vertical {
                background-color: #2d2d2d;
                width: 12px;
            }
            QScrollBar::handle:vertical {
                background-color: #555;
                border-radius: 6px;
            }
        """)
        
        grid_container = QWidget()
        grid_container.setStyleSheet("background-color: #1a1a1a;")
        self.clients_grid = QGridLayout(grid_container)
        self.clients_grid.setSpacing(15)
        self.clients_grid.setContentsMargins(20, 20, 20, 20)
        self.clients_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        grid_scroll.setWidget(grid_container)
        self.client_view_stack.addWidget(grid_scroll)
        
        # LIST VIEW
        self.lst_clients = QListWidget()
        self.lst_clients.setSelectionMode(QListWidget.MultiSelection)
        self.lst_clients.itemSelectionChanged.connect(self._on_client_selection_changed)
        self.lst_clients.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lst_clients.customContextMenuRequested.connect(self._show_client_context_menu)
        
        self.lst_clients.setIconSize(QSize(48, 48))
        self.lst_clients.setSpacing(5)
        self.lst_clients.setStyleSheet("""
            QListWidget {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px;
            }
            QListWidget::item {
                padding: 10px;
                border-radius: 4px;
                margin: 2px;
            }
            QListWidget::item:selected {
                background-color: #094771;
            }
            QListWidget::item:hover {
                background-color: #2a2d2e;
            }
        """)
        
        self.client_view_stack.addWidget(self.lst_clients)
        
        layout.addWidget(self.client_view_stack)
        
        # Controls
        btn_layout = QHBoxLayout()
        
        # FIXED: Use manual refresh instead
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self.manual_refresh_clients)
        btn_layout.addWidget(btn_refresh)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # Default to grid view
        self.current_client_view = "grid"
        self.client_view_stack.setCurrentIndex(0)
        self.btn_grid_view.setStyleSheet("background-color: #0078d4;")
        self.btn_list_view.setStyleSheet("background-color: #2d2d2d;")
        self.selected_client_cards = set()
        
        return widget

    def switch_client_view(self, view_mode):
        """FIXED: Switch between grid and list view with refresh"""
        self.current_client_view = view_mode
        
        if view_mode == "grid":
            self.client_view_stack.setCurrentIndex(0)
            self.btn_grid_view.setStyleSheet("background-color: #0078d4; color: white;")
            self.btn_list_view.setStyleSheet("background-color: #2d2d2d; color: #e0e0e0;")
        else:
            self.client_view_stack.setCurrentIndex(1)
            self.btn_grid_view.setStyleSheet("background-color: #2d2d2d; color: #e0e0e0;")
            self.btn_list_view.setStyleSheet("background-color: #0078d4; color: white;")
        
        # FIXED: Force immediate refresh after view switch
        QTimer.singleShot(100, self.refresh_clients)

    def create_client_card(self, client_key, hostname, connected=True):
        """Create a client card widget - UPDATED for better layout"""
        card = QFrame()
        card.setFixedSize(120, 140)  # CHANGED: Smaller, more compact
        card.setStyleSheet("""
            QFrame {
                background-color: #2a2a2a;
                border: 2px solid #3c3c3c;
                border-radius: 8px;
            }
            QFrame:hover {
                border-color: #0078d4;
                background-color: #2d2d2d;
            }
        """)
        card.setProperty("selected", False)
        card.setProperty("client_key", client_key)
        
        card_layout = QVBoxLayout(card)
        card_layout.setAlignment(Qt.AlignCenter)
        card_layout.setSpacing(5)  # CHANGED: Tighter spacing
        card_layout.setContentsMargins(5, 5, 5, 5)
        
        # PC Icon - CHANGED: Smaller
        icon_label = QLabel()
        pc_icon = self._create_large_pc_icon()
        scaled_icon = pc_icon.scaled(50, 50, Qt.KeepAspectRatio, Qt.SmoothTransformation)  # CHANGED: Smaller icon
        icon_label.setPixmap(scaled_icon)
        icon_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(icon_label)
        
        # Status indicator - CHANGED: Smaller
        status = QLabel("🟢" if connected else "🔴")
        status.setAlignment(Qt.AlignCenter)
        status.setFont(QFont("Segoe UI", 12))  # CHANGED: Smaller font
        card_layout.addWidget(status)
        
        # Hostname - CHANGED: Smaller font
        name_label = QLabel(hostname)
        name_label.setAlignment(Qt.AlignCenter)
        name_label.setWordWrap(True)
        name_label.setStyleSheet("font-weight: bold; color: #e0e0e0; font-size: 10px;")  # CHANGED: Smaller font
        card_layout.addWidget(name_label)
        
        # IP - CHANGED: Smaller font
        ip_label = QLabel(client_key.split(":")[0])
        ip_label.setAlignment(Qt.AlignCenter)
        ip_label.setStyleSheet("color: #888; font-size: 9px;")  # CHANGED: Smaller font
        card_layout.addWidget(ip_label)
        
        # Click handler
        card.mousePressEvent = lambda e: self.on_client_card_clicked(card, e)
        
        return card

    def on_client_card_clicked(self, card, event):
        """FIXED: Handle client card click with monitoring state management"""
        client_key = card.property("client_key")
        
        if not client_key or ":disconnected" in client_key:
            return  # Ignore disconnected clients
        
        if event.button() == Qt.LeftButton:
            # Clear all selections first
            for i in range(self.clients_grid.count()):
                item = self.clients_grid.itemAt(i)
                if item and item.widget():
                    other_card = item.widget()
                    if other_card and other_card != card:
                        other_card.setProperty("selected", False)
                        other_card.setStyleSheet("""
                            QFrame {
                                background-color: #2a2a2a;
                                border: 2px solid #3c3c3c;
                                border-radius: 8px;
                            }
                            QFrame:hover {
                                border-color: #0078d4;
                                background-color: #2d2d2d;
                            }
                        """)
            
            # Toggle selection for clicked card
            is_selected = card.property("selected")
            
            if not is_selected:
                # Select and start monitoring
                card.setProperty("selected", True)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2d2d2d;
                        border: 3px solid #0078d4;
                        border-radius: 8px;
                    }
                """)
                self.selected_client_cards.clear()
                self.selected_client_cards.add(client_key)
                
                # Set as preview client for monitor view
                self.selected_preview_client = client_key
                
                # Get hostname for display
                hostname = client_key.split(":")[0]
                with self.server.clients_lock:
                    if client_key in self.server.clients:
                        handler = self.server.clients[client_key]
                        hostname = handler.client_info.get("hostname", hostname)
                
                self.lbl_preview_info.setText(f"🔴 LIVE: {hostname}")
                
                # Always request screen stream when selecting
                with self.server.clients_lock:
                    if client_key in self.server.clients:
                        self.server.clients[client_key].send_command("START_SCREEN_STREAM")
                        self.server.log(f"📺 Started monitoring: {hostname}")
                
                # FIXED: Pause refresh timer during monitoring
                if hasattr(self, 'timer_clients'):
                    self.timer_clients.stop()
                    self.server.log(f"⏸️ Paused client refresh during monitoring")
            else:
                # Deselect and stop monitoring
                card.setProperty("selected", False)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2a2a2a;
                        border: 2px solid #3c3c3c;
                        border-radius: 8px;
                    }
                    QFrame:hover {
                        border-color: #0078d4;
                        background-color: #2d2d2d;
                    }
                """)
                self.selected_client_cards.discard(client_key)
                
                # Clear preview client and stop stream
                if self.selected_preview_client == client_key:
                    with self.server.clients_lock:
                        if client_key in self.server.clients:
                            self.server.clients[client_key].send_command("STOP_SCREEN_STREAM")
                    
                    self.selected_preview_client = None
                    self.lbl_preview_info.setText("No client selected")
                    self.lbl_preview.setText("Select a client and request screen")
                    
                    # FIXED: Resume refresh timer when stopping monitoring
                    if hasattr(self, 'timer_clients'):
                        self.timer_clients.start()
                        self.server.log(f"▶️ Resumed client refresh")
        
        elif event.button() == Qt.RightButton:
            # Select card on right-click if not already selected
            if client_key not in self.selected_client_cards:
                # Clear other selections
                for i in range(self.clients_grid.count()):
                    item = self.clients_grid.itemAt(i)
                    if item and item.widget():
                        other_card = item.widget()
                        if other_card:
                            other_card.setProperty("selected", False)
                            other_card.setStyleSheet("""
                                QFrame {
                                    background-color: #2a2a2a;
                                    border: 2px solid #3c3c3c;
                                    border-radius: 8px;
                                }
                                QFrame:hover {
                                    border-color: #0078d4;
                                    background-color: #2d2d2d;
                                }
                            """)
                
                # Select this card
                card.setProperty("selected", True)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2d2d2d;
                        border: 3px solid #0078d4;
                        border-radius: 8px;
                    }
                """)
                self.selected_client_cards.clear()
                self.selected_client_cards.add(client_key)
            
            # Show context menu
            self._show_client_context_menu_for_card(card.mapToGlobal(event.pos()), client_key)
            
    def _show_client_context_menu_for_card(self, position, client_key):
        """Show context menu for card - UPDATED with remote control"""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #252526;
                color: #e0e0e0;
                border: 1px solid #3c3c3c;
            }
            QMenu::item:selected {
                background-color: #094771;
            }
        """)
        
        lock_action = menu.addAction("🔒 Lock Screen")
        lock_action.triggered.connect(lambda: self.send_to_selected("LOCK"))
        
        unlock_action = menu.addAction("🔓 Unlock Screen")
        unlock_action.triggered.connect(lambda: self.send_to_selected("UNLOCK"))
        
        menu.addSeparator()
        
        # ADDED: Remote Control option
        remote_control_action = menu.addAction("🖱️ Remote Control")
        remote_control_action.triggered.connect(self.start_remote_control_for_selected)
        
        menu.addSeparator()
        
        shutdown_action = menu.addAction("⚠️ Shutdown PC")
        shutdown_action.triggered.connect(self.shutdown_selected_clients)
        
        restart_action = menu.addAction("🔄 Restart PC")
        restart_action.triggered.connect(self.restart_selected_clients)
        
        menu.addSeparator()
        
        start_monitor_action = menu.addAction("▶️ Start Live View")
        start_monitor_action.triggered.connect(lambda: self.send_to_selected("START_SCREEN_STREAM"))
        
        stop_monitor_action = menu.addAction("⏹️ Stop Live View")
        stop_monitor_action.triggered.connect(lambda: self.send_to_selected("STOP_SCREEN_STREAM"))
        
        menu.addSeparator()
        
        send_file_action = menu.addAction("📤 Send File")
        send_file_action.triggered.connect(self.send_file_to_selected)
        
        backup_action = menu.addAction("💾 Backup Files")
        backup_action.triggered.connect(self.backup_client_files)
        
        restore_action = menu.addAction("📥 Restore Files")
        restore_action.triggered.connect(self.restore_client_files)
        
        menu.addSeparator()
        
        message_action = menu.addAction("💬 Send Message")
        message_action.triggered.connect(self.send_message_to_selected)
        
        menu.exec_(position)
    def _show_client_context_menu(self, position: QPoint):
        item = self.lst_clients.itemAt(position)
        if not item:
            return
        
        if not item.isSelected():
            self.lst_clients.clearSelection()
            item.setSelected(True)
        
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #252526;
                color: #e0e0e0;
                border: 1px solid #3c3c3c;
            }
            QMenu::item:selected {
                background-color: #094771;
            }
        """)
        
        lock_action = menu.addAction("🔒 Lock Screen")
        lock_action.triggered.connect(lambda: self.send_to_selected("LOCK"))
        
        unlock_action = menu.addAction("🔓 Unlock Screen")
        unlock_action.triggered.connect(lambda: self.send_to_selected("UNLOCK"))
        
        menu.addSeparator()
        
        # ADDED: Remote Control option
        remote_control_action = menu.addAction("🖱️ Remote Control")
        remote_control_action.triggered.connect(self.start_remote_control_for_selected)
        
        menu.addSeparator()
        
        # NEW: Shutdown/Restart in context menu
        shutdown_action = menu.addAction("⚠️ Shutdown PC")
        shutdown_action.triggered.connect(self.shutdown_selected_clients)
        
        restart_action = menu.addAction("🔄 Restart PC")
        restart_action.triggered.connect(self.restart_selected_clients)
        
        menu.addSeparator()
        
        start_monitor_action = menu.addAction("▶️ Start Live View")
        start_monitor_action.triggered.connect(lambda: self.send_to_selected("START_SCREEN_STREAM"))
        
        stop_monitor_action = menu.addAction("⏹️ Stop Live View")
        stop_monitor_action.triggered.connect(lambda: self.send_to_selected("STOP_SCREEN_STREAM"))
        
        menu.addSeparator()
        
        send_file_action = menu.addAction("📤 Send File")
        send_file_action.triggered.connect(self.send_file_to_selected)
        
        menu.addSeparator()
        
        backup_action = menu.addAction("💾 Backup Files")
        backup_action.triggered.connect(self.backup_client_files)
        
        restore_action = menu.addAction("📥 Restore Files")
        restore_action.triggered.connect(self.restore_client_files)
        
        menu.addSeparator()
        
        message_action = menu.addAction("💬 Send Message")
        message_action.triggered.connect(self.send_message_to_selected)
        
        menu.addSeparator()

        remove_action = menu.addAction("🗑️ Remove from List")
        remove_action.triggered.connect(self.remove_selected_clients)
        
        menu.exec_(self.lst_clients.mapToGlobal(position))

    def _create_large_pc_icon(self):
        """Create larger PC icon for grid view"""
        pixmap = QPixmap(80, 80)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Monitor
        painter.setBrush(QBrush(QColor(70, 130, 180)))
        painter.setPen(QPen(QColor(40, 90, 140), 3))
        painter.drawRoundedRect(8, 8, 64, 45, 5, 5)
        
        # Screen
        painter.setBrush(QBrush(QColor(100, 160, 210)))
        painter.setPen(Qt.NoPen)
        painter.drawRect(14, 14, 52, 33)
        
        # Stand
        painter.setBrush(QBrush(QColor(60, 60, 60)))
        painter.setPen(QPen(QColor(40, 40, 40), 2))
        painter.drawRect(32, 53, 16, 10)
        painter.drawRect(22, 63, 36, 6)
        
        # Power LED
        painter.setBrush(QBrush(QColor(0, 255, 0)))
        painter.drawEllipse(60, 48, 6, 6)
        
        painter.end()
        return pixmap

    def show_remote_control(self, client_key):
        """Show remote control window"""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"🖱️ Remote Control - {client_key}")
        dialog.resize(1024, 768)
        
        layout = QVBoxLayout(dialog)
        
        # Screen display
        self.remote_screen = QLabel()
        self.remote_screen.setStyleSheet("background-color: black; border: 2px solid #0078d4;")
        self.remote_screen.setAlignment(Qt.AlignCenter)
        self.remote_screen.setMinimumSize(800, 600)
        
        # Install event filter for mouse/keyboard
        self.remote_screen.setMouseTracking(True)
        self.remote_screen.mouseMoveEvent = lambda e: self.send_mouse_event(client_key, "MOVE", e)
        self.remote_screen.mousePressEvent = lambda e: self.send_mouse_event(client_key, "CLICK", e)
        self.remote_screen.mouseDoubleClickEvent = lambda e: self.send_mouse_event(client_key, "DOUBLE_CLICK", e)
        
        layout.addWidget(self.remote_screen)
        
        # Controls
        control_layout = QHBoxLayout()
        
        btn_start = QPushButton("▶️ Start Control")
        btn_start.clicked.connect(lambda: self.start_remote_control(client_key))
        control_layout.addWidget(btn_start)
        
        btn_stop = QPushButton("⏹️ Stop Control")
        btn_stop.clicked.connect(lambda: self.stop_remote_control(client_key))
        control_layout.addWidget(btn_stop)
        
        control_layout.addStretch()
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dialog.close)
        control_layout.addWidget(btn_close)
        
        layout.addLayout(control_layout)
        
        # Request screen stream
        with self.server.clients_lock:
            if client_key in self.server.clients:
                self.server.clients[client_key].send_command("START_SCREEN_STREAM")
        
        # Store for frame updates
        self.remote_control_client = client_key
        self.remote_control_screen = self.remote_screen
        
        dialog.exec_()
        
        # Cleanup
        self.stop_remote_control(client_key)
        self.remote_control_client = None

    def send_mouse_event(self, client_key, action, event):
        """Send mouse event to client"""
        # Scale coordinates to client resolution
        x = int(event.x() * (1920 / self.remote_screen.width()))
        y = int(event.y() * (1080 / self.remote_screen.height()))
        
        cmd = f"REMOTE_MOUSE:{action}:{x}:{y}"
        
        with self.server.clients_lock:
            if client_key in self.server.clients:
                self.server.clients[client_key].send_command(cmd)

    def start_remote_control_for_selected(self):
        """Start remote control for selected client"""
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Please select a client first")
            return
        
        if len(keys) > 1:
            QMessageBox.warning(self, "Multiple Selection", "Please select only one client for remote control")
            return
        
        client_key = keys[0]
        
        # Show remote control window
        self.show_remote_control(client_key)
    def start_remote_control(self, client_key):
        """Enable remote control on client"""
        try:
            with self.server.clients_lock:
                if client_key in self.server.clients:
                    self.server.clients[client_key].send_command("START_REMOTE_CONTROL")
                    self.server.log(f"🖱️ Remote control enabled for {client_key}")
                    return True
            return False
        except Exception as e:
            self.server.log(f"❌ Failed to start remote control: {e}")
            return False
    
    def stop_remote_control(self, client_key):
        """Disable remote control on client"""
        with self.server.clients_lock:
            if client_key in self.server.clients:
                self.server.clients[client_key].send_command("STOP_REMOTE_CONTROL")
                self.server.clients[client_key].send_command("STOP_SCREEN_STREAM")
                self.server.log(f"🛑 Remote control stopped for {client_key}")
            


    def refresh_clients(self):
        """FIXED: Refresh without disrupting active monitoring"""
        keys = self.server.list_clients()
        
        # Get database client status
        db_clients = {}
        if self.server.db and self.server.admin_ip:
            try:
                for client in self.server.db.get_admin_clients(self.server.admin_ip):
                    db_clients[client['client_ip']] = client
            except:
                pass
        
        # Update LIST view
        selected = set([it.data(Qt.UserRole) for it in self.lst_clients.selectedItems() if it.data(Qt.UserRole)])
        
        self.lst_clients.clear()
        pc_icon = self._create_pc_icon()
        
        for k in keys:
            hostname = k.split(":")[0]
            with self.server.clients_lock:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    hostname = handler.client_info.get("hostname", hostname)
            
            client_ip = k.split(":")[0]
            display_text = f"🟢 {hostname}\nIP: {client_ip}"
            
            item = QListWidgetItem(pc_icon, display_text)
            item.setData(Qt.UserRole, k)
            item.setFont(QFont("Segoe UI", 11))
            item.setSizeHint(QSize(200, 60))
            
            if k in selected:
                item.setSelected(True)
            
            self.lst_clients.addItem(item)
        
        # Add disconnected clients from database
        for client_ip, client_data in db_clients.items():
            if client_data['status'] == 'disconnected':
                if not any(item.data(Qt.UserRole).startswith(client_ip) for item in [self.lst_clients.item(i) for i in range(self.lst_clients.count())]):
                    display_text = f"❌ {client_data['client_name']}\nIP: {client_ip} (Disconnected)"
                    
                    item = QListWidgetItem(pc_icon, display_text)
                    item.setData(Qt.UserRole, f"{client_ip}:disconnected")
                    item.setFont(QFont("Segoe UI", 11))
                    item.setSizeHint(QSize(200, 60))
                    item.setForeground(QColor("#888"))
                    
                    self.lst_clients.addItem(item)
        
        # FIXED: Only update grid if NOT actively monitoring
        if self.current_client_view == "grid":
            # Check if actively monitoring
            actively_monitoring = (self.selected_preview_client and 
                                self.view_mode == "monitor" and
                                self.selected_preview_client in keys)
            
            if not actively_monitoring:
                # Safe to refresh grid
                self._refresh_grid_view(keys, db_clients)
            else:
                # Just update existing cards without rebuild
                self._update_grid_cards(keys, db_clients)

    # In AdminWindow class - NEW METHOD to update cards without rebuilding
    def _update_grid_cards(self, keys, db_clients):
        """Update grid cards without rebuilding (preserves selection during monitoring)"""
        # Get all current card client keys
        existing_cards = {}
        for i in range(self.clients_grid.count()):
            item = self.clients_grid.itemAt(i)
            if item and item.widget():
                card = item.widget()
                client_key = card.property("client_key")
                if client_key:
                    existing_cards[client_key] = card
        
        # Add any new clients that don't have cards
        current_keys = set(keys)
        existing_keys = set(existing_cards.keys()) - {k for k in existing_cards.keys() if ":disconnected" in k}
        
        new_keys = current_keys - existing_keys
        
        if new_keys:
            # Calculate next position
            row = self.clients_grid.rowCount() - 1  # Don't count the stretch row
            col = 0
            max_cols = 6
            
            # Find last occupied position
            for i in range(self.clients_grid.count()):
                item = self.clients_grid.itemAt(i)
                if item and item.widget():
                    r, c, _, _ = self.clients_grid.getItemPosition(i)
                    if r > row or (r == row and c > col):
                        row = r
                        col = c
            
            # Add new cards
            for k in new_keys:
                hostname = k.split(":")[0]
                with self.server.clients_lock:
                    if k in self.server.clients:
                        handler = self.server.clients[k]
                        hostname = handler.client_info.get("hostname", hostname)
                        # Skip teacher clients
                        if handler.client_info.get("type", "student") == "teacher":
                            continue
                
                col += 1
                if col >= max_cols:
                    col = 0
                    row += 1
                
                card = self.create_client_card(k, hostname, connected=True)
                
                # Restore selection if needed
                if k in self.selected_client_cards:
                    card.setProperty("selected", True)
                    card.setStyleSheet("""
                        QFrame {
                            background-color: #2d2d2d;
                            border: 3px solid #0078d4;
                            border-radius: 8px;
                        }
                    """)
                
                self.clients_grid.addWidget(card, row, col)
    
    def _refresh_grid_view(self, keys, db_clients):
        """FIXED: Refresh grid view with better selection preservation"""
        # Store currently selected client
        selected_client = self.selected_preview_client
        
        # Clear grid completely
        while self.clients_grid.count():
            item = self.clients_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # Remove any stretch
        for i in reversed(range(self.clients_grid.rowCount())):
            self.clients_grid.setRowStretch(i, 0)
        
        row, col = 0, 0
        max_cols = 6
        
        # Add connected clients
        for k in keys:
            hostname = k.split(":")[0]
            with self.server.clients_lock:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    hostname = handler.client_info.get("hostname", hostname)
                    # Skip teacher clients
                    if handler.client_info.get("type", "student") == "teacher":
                        continue
            
            card = self.create_client_card(k, hostname, connected=True)
            
            # Restore selection state
            if k in self.selected_client_cards or k == selected_client:
                card.setProperty("selected", True)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2d2d2d;
                        border: 3px solid #0078d4;
                        border-radius: 8px;
                    }
                """)
                # Ensure it's in the selected set
                self.selected_client_cards.add(k)
            
            self.clients_grid.addWidget(card, row, col)
            
            col += 1
            if col >= max_cols:
                col = 0
                row += 1
        
        # Add disconnected clients
        for client_ip, client_data in db_clients.items():
            if client_data['status'] == 'disconnected':
                if not any(k.startswith(client_ip) for k in keys):
                    card = self.create_client_card(
                        f"{client_ip}:disconnected", 
                        client_data['client_name'],
                        connected=False
                    )
                    self.clients_grid.addWidget(card, row, col)
                    
                    col += 1
                    if col >= max_cols:
                        col = 0
                        row += 1
        
        # Add vertical stretch to push content to top
        self.clients_grid.setRowStretch(row + 1, 1)
        
        # Force layout update
        self.clients_grid.update()

    def on_client_card_clicked(self, card, event):
        """FIXED: Handle client card click with proper monitoring setup"""
        client_key = card.property("client_key")
        
        if not client_key or ":disconnected" in client_key:
            return  # Ignore disconnected clients
        
        if event.button() == Qt.LeftButton:
            # Clear all selections first
            for i in range(self.clients_grid.count()):
                item = self.clients_grid.itemAt(i)
                if item and item.widget():
                    other_card = item.widget()
                    if other_card and other_card != card:
                        other_card.setProperty("selected", False)
                        other_card.setStyleSheet("""
                            QFrame {
                                background-color: #2a2a2a;
                                border: 2px solid #3c3c3c;
                                border-radius: 8px;
                            }
                            QFrame:hover {
                                border-color: #0078d4;
                                background-color: #2d2d2d;
                            }
                        """)
            
            # Toggle selection for clicked card
            is_selected = card.property("selected")
            
            if not is_selected:
                card.setProperty("selected", True)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2d2d2d;
                        border: 3px solid #0078d4;
                        border-radius: 8px;
                    }
                """)
                self.selected_client_cards.clear()
                self.selected_client_cards.add(client_key)
                
                # Set as preview client for monitor view
                self.selected_preview_client = client_key
                
                # Get hostname for display
                hostname = client_key.split(":")[0]
                with self.server.clients_lock:
                    if client_key in self.server.clients:
                        handler = self.server.clients[client_key]
                        hostname = handler.client_info.get("hostname", hostname)
                
                self.lbl_preview_info.setText(f"Monitoring: {hostname}")
                
                # FIXED: Always request screen stream when selecting
                with self.server.clients_lock:
                    if client_key in self.server.clients:
                        self.server.clients[client_key].send_command("START_SCREEN_STREAM")
                        self.server.log(f"📺 Started monitoring: {hostname}")
            else:
                # Deselect
                card.setProperty("selected", False)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2a2a2a;
                        border: 2px solid #3c3c3c;
                        border-radius: 8px;
                    }
                    QFrame:hover {
                        border-color: #0078d4;
                        background-color: #2d2d2d;
                    }
                """)
                self.selected_client_cards.discard(client_key)
                
                # Clear preview client
                if self.selected_preview_client == client_key:
                    # Stop stream
                    with self.server.clients_lock:
                        if client_key in self.server.clients:
                            self.server.clients[client_key].send_command("STOP_SCREEN_STREAM")
                    
                    self.selected_preview_client = None
                    self.lbl_preview_info.setText("No client selected")
                    self.lbl_preview.setText("Select a client and request screen")
        
        elif event.button() == Qt.RightButton:
            # Select card on right-click if not already selected
            if client_key not in self.selected_client_cards:
                # Clear other selections
                for i in range(self.clients_grid.count()):
                    item = self.clients_grid.itemAt(i)
                    if item and item.widget():
                        other_card = item.widget()
                        if other_card:
                            other_card.setProperty("selected", False)
                            other_card.setStyleSheet("""
                                QFrame {
                                    background-color: #2a2a2a;
                                    border: 2px solid #3c3c3c;
                                    border-radius: 8px;
                                }
                                QFrame:hover {
                                    border-color: #0078d4;
                                    background-color: #2d2d2d;
                                }
                            """)
                
                # Select this card
                card.setProperty("selected", True)
                card.setStyleSheet("""
                    QFrame {
                        background-color: #2d2d2d;
                        border: 3px solid #0078d4;
                        border-radius: 8px;
                    }
                """)
                self.selected_client_cards.clear()
                self.selected_client_cards.add(client_key)
            
            # Show context menu
            self._show_client_context_menu_for_card(card.mapToGlobal(event.pos()), client_key)
        
    def _create_logs_view(self):
            """Create logs view"""
            widget = QWidget()
            layout = QVBoxLayout(widget)
            
            title = QLabel("📋 Server Activity Log")
            title.setFont(QFont("Segoe UI", 14, QFont.Bold))
            layout.addWidget(title)
            
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
        
    def switch_view(self, view_name):
        """FIXED: Switch between views with proper timer management"""
        self.view_mode = view_name
        
        # Update button styles
        self.btn_view_monitor.setStyleSheet("background-color: #2d2d2d;" if view_name != "monitor" else "background-color: #0078d4;")
        self.btn_view_clients.setStyleSheet("background-color: #2d2d2d;" if view_name != "clients" else "background-color: #0078d4;")
        self.btn_view_logs.setStyleSheet("background-color: #2d2d2d;" if view_name != "logs" else "background-color: #0078d4;")
        self.btn_view_stats.setStyleSheet("background-color: #2d2d2d;" if view_name != "statistics" else "background-color: #0078d4;")
        
        # Switch view
        if view_name == "monitor":
            self.stacked_views.setCurrentIndex(0)
            # If client selected, start monitoring and pause refresh
            if self.selected_preview_client:
                with self.server.clients_lock:
                    if self.selected_preview_client in self.server.clients:
                        self.server.clients[self.selected_preview_client].send_command("START_SCREEN_STREAM")
                        # Pause refresh timer
                        if hasattr(self, 'timer_clients'):
                            self.timer_clients.stop()
        elif view_name == "clients":
            self.stacked_views.setCurrentIndex(1)
            # Resume refresh timer when leaving monitor view
            if hasattr(self, 'timer_clients') and not self.timer_clients.isActive():
                self.timer_clients.start()
            # Force refresh when switching to clients view
            QTimer.singleShot(100, self.refresh_clients)
        elif view_name == "logs":
            self.stacked_views.setCurrentIndex(2)
            # Resume refresh timer
            if hasattr(self, 'timer_clients') and not self.timer_clients.isActive():
                self.timer_clients.start()
        elif view_name == "statistics":
            self.stacked_views.setCurrentIndex(3)
            # Resume refresh timer
            if hasattr(self, 'timer_clients') and not self.timer_clients.isActive():
                self.timer_clients.start()
            self.update_statistics()
    
    def _start_timers(self):
        self.timer_log = QTimer(self)
        self.timer_log.setInterval(200)
        self.timer_log.timeout.connect(self._drain_logs)
        self.timer_log.start()
        
        # FIXED: Slower client refresh to avoid disrupting monitoring
        self.timer_clients = QTimer(self)
        self.timer_clients.setInterval(5000)  # Changed from 2000 to 5000ms (5 seconds)
        self.timer_clients.timeout.connect(self.refresh_clients)
        self.timer_clients.start()
        
        self.timer_frames = QTimer(self)
        self.timer_frames.setInterval(16)  # ~60 FPS
        self.timer_frames.timeout.connect(self._update_frames)
        self.timer_frames.start()
        
        self.timer_status = QTimer(self)
        self.timer_status.setInterval(1000)
        self.timer_status.timeout.connect(self._update_status)
        self.timer_status.start()
        
        self.server.signals.new_frame.connect(self._on_new_frame)
        self.server.signals.statistics_updated.connect(self.update_statistics)
    
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
        """FIXED: Update preview with better error handling and logging"""
        if not self.selected_preview_client:
            return
        
        try:
            # Check if client is still connected
            with self.server.clients_lock:
                if self.selected_preview_client not in self.server.clients:
                    self.server.log(f"⚠️ Preview client {self.selected_preview_client} disconnected")
                    self.selected_preview_client = None
                    self.lbl_preview.setText("Client disconnected")
                    self.lbl_preview_info.setText("No client selected")
                    return
            
            # Get frame from buffer
            if self.selected_preview_client in self.server.frame_buffers:
                with self.server.frame_locks[self.selected_preview_client]:
                    buffer = self.server.frame_buffers.get(self.selected_preview_client)
                    if buffer and len(buffer) > 0:
                        frame_data = buffer[-1]
                        self._display_image_bytes(frame_data)
        except Exception as e:
            # Don't log every frame error, just clear selection
            if self.selected_preview_client:
                self.server.log(f"⚠️ Frame update error for {self.selected_preview_client}: {e}")
                self.selected_preview_client = None
                self.lbl_preview.setText("Error displaying frame")
    
    def _update_status(self):
        if self.server.running.is_set():
            stats = self.server.get_server_stats()
            
            hours, remainder = divmod(int(stats['uptime']), 3600)
            minutes, seconds = divmod(remainder, 60)
            self.lbl_uptime.setText(f"⏱️ Uptime: {hours:02d}:{minutes:02d}:{seconds:02d}")
            
            self.lbl_clients_count.setText(f"👥 Clients: {stats['active_clients']}")
    
    def _on_new_frame(self, client_key, frame_data):
        """FIXED: Handle new frames with immediate display for selected client"""
        if client_key == self.selected_preview_client:
            # Immediate display for monitored client
            self._display_image_bytes(frame_data)
        
        # Update remote control screen if active
        if hasattr(self, 'remote_control_client') and client_key == self.remote_control_client:
            if hasattr(self, 'remote_control_screen'):
                self._display_image_on_label(self.remote_control_screen, frame_data)

    def _display_image_on_label(self, label, image_data):
        """Display image on any QLabel"""
        try:
            qimg = QImage.fromData(QByteArray(image_data))
            if not qimg.isNull():
                pixmap = QPixmap.fromImage(qimg)
                scaled = pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                label.setPixmap(scaled)
        except:
            pass
    def manual_refresh_clients(self):
        """Force refresh clients list (bypasses monitoring check)"""
        # Temporarily clear monitoring state
        temp_client = self.selected_preview_client
        self.selected_preview_client = None
        
        # Do refresh
        self.refresh_clients()
        
        # Restore monitoring state
        self.selected_preview_client = temp_client
    
    def _on_client_selection_changed(self):
        """FIXED: Handle list view selection with timer management"""
        keys = self._get_selected_keys()
        if keys:
            self.selected_preview_client = keys[0]
            
            # Get hostname
            hostname = keys[0].split(":")[0]
            with self.server.clients_lock:
                if keys[0] in self.server.clients:
                    handler = self.server.clients[keys[0]]
                    hostname = handler.client_info.get("hostname", hostname)
            
            self.lbl_preview_info.setText(f"🔴 LIVE: {hostname}")
            
            # Pause refresh timer during monitoring
            if hasattr(self, 'timer_clients'):
                self.timer_clients.stop()
        else:
            self.selected_preview_client = None
            self.lbl_preview_info.setText("No client selected")
            
            # Resume refresh timer when no selection
            if hasattr(self, 'timer_clients') and not self.timer_clients.isActive():
                self.timer_clients.start()
    
    def start_server(self):
        if self.server.start():
            self.lbl_server_status.setText(f"🟢 Server: Running on {self.server.host}:{self.server.port}")
            self.lbl_server_status.setStyleSheet("background-color: #2d5016; color: #90ee90; padding: 10px; border-radius: 5px;")
            self.btn_start_server.setEnabled(False)
            self.btn_stop_server.setEnabled(True)
            
            self._update_restriction_label()
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
            self.lbl_server_status.setStyleSheet("background-color: #2d2d2d; padding: 10px; border-radius: 5px;")
            self.btn_start_server.setEnabled(True)
            self.btn_stop_server.setEnabled(False)
            self.lst_clients.clear()
            
            
    def show_file_browser(self, client_key):
        """Show file browser dialog for client"""
        from PyQt5.QtWidgets import QTreeWidget, QTreeWidgetItem
        
        dialog = QDialog(self)
        dialog.setWindowTitle(f"📁 Browse Files - {client_key}")
        dialog.resize(700, 500)
        dialog.setStyleSheet(self.styleSheet())
        
        layout = QVBoxLayout(dialog)
        
        # Path bar
        path_layout = QHBoxLayout()
        path_label = QLabel("Path:")
        path_layout.addWidget(path_label)
        
        self.path_edit = QLineEdit("C:\\Users\\Student")
        path_layout.addWidget(self.path_edit)
        
        btn_go = QPushButton("Go")
        btn_go.clicked.connect(lambda: self.request_directory(client_key, self.path_edit.text()))
        path_layout.addWidget(btn_go)
        
        layout.addLayout(path_layout)
        
        # File tree
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels(["Name", "Size", "Type"])
        self.file_tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.file_tree)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        btn_select = QPushButton("✅ Select for Backup")
        btn_select.clicked.connect(lambda: dialog.done(1))
        btn_layout.addWidget(btn_select)
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(dialog.reject)
        btn_layout.addWidget(btn_cancel)
        
        layout.addLayout(btn_layout)
        
        # Initial load
        self.request_directory(client_key, "C:\\Users\\Student")
        
        if dialog.exec_() == 1:
            selected = self.path_edit.text()
            return selected
        return None

    def request_directory(self, client_key, path):
        """Request directory listing from client"""
        with self.server.clients_lock:
            if client_key in self.server.clients:
                handler = self.server.clients[client_key]
                handler.send_command(f"BROWSE_FILES:{path}")
                
                # Wait for response
                QTimer.singleShot(500, lambda: self.update_file_tree(handler))

    def update_file_tree(self, handler):
        """Update file tree with received data"""
        if not hasattr(handler, 'directory_data'):
            return
        
        self.file_tree.clear()
        dir_data = handler.directory_data
        
        # Add parent directory
        parent_item = QTreeWidgetItem(["📁 ..", "", "Directory"])
        self.file_tree.addTopLevelItem(parent_item)
        
        # Add items
        for item in dir_data.get('items', []):
            if item['is_dir']:
                tree_item = QTreeWidgetItem([
                    f"📁 {item['name']}",
                    "",
                    "Directory"
                ])
            else:
                tree_item = QTreeWidgetItem([
                    f"📄 {item['name']}",
                    format_bytes(item['size']),
                    "File"
                ])
            tree_item.setData(0, Qt.UserRole, item['path'])
            self.file_tree.addTopLevelItem(tree_item)

    def on_item_double_clicked(self, item, column):
        """Navigate into directory"""
        path = item.data(0, Qt.UserRole)
        if path:
            self.path_edit.setText(path)
            # Re-request
            
    def refresh_clients(self):
        """FIXED: Skip refresh entirely during active monitoring"""
        # CRITICAL: Don't refresh if actively monitoring
        if self.selected_preview_client and self.view_mode == "monitor":
            return  # Skip refresh to avoid disrupting monitoring
        
        keys = self.server.list_clients()
        
        # Get database client status
        db_clients = {}
        if self.server.db and self.server.admin_ip:
            try:
                for client in self.server.db.get_admin_clients(self.server.admin_ip):
                    db_clients[client['client_ip']] = client
            except:
                pass
        
        # Update LIST view
        selected = set([it.data(Qt.UserRole) for it in self.lst_clients.selectedItems() if it.data(Qt.UserRole)])
        
        self.lst_clients.clear()
        pc_icon = self._create_pc_icon()
        
        for k in keys:
            hostname = k.split(":")[0]
            with self.server.clients_lock:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    hostname = handler.client_info.get("hostname", hostname)
            
            client_ip = k.split(":")[0]
            display_text = f"🟢 {hostname}\nIP: {client_ip}"
            
            item = QListWidgetItem(pc_icon, display_text)
            item.setData(Qt.UserRole, k)
            item.setFont(QFont("Segoe UI", 11))
            item.setSizeHint(QSize(200, 60))
            
            if k in selected:
                item.setSelected(True)
            
            self.lst_clients.addItem(item)
        
        # Add disconnected clients from database
        for client_ip, client_data in db_clients.items():
            if client_data['status'] == 'disconnected':
                if not any(item.data(Qt.UserRole).startswith(client_ip) for item in [self.lst_clients.item(i) for i in range(self.lst_clients.count())]):
                    display_text = f"❌ {client_data['client_name']}\nIP: {client_ip} (Disconnected)"
                    
                    item = QListWidgetItem(pc_icon, display_text)
                    item.setData(Qt.UserRole, f"{client_ip}:disconnected")
                    item.setFont(QFont("Segoe UI", 11))
                    item.setSizeHint(QSize(200, 60))
                    item.setForeground(QColor("#888"))
                    
                    self.lst_clients.addItem(item)
        
        # Update GRID view only if safe
        if self.current_client_view == "grid":
            self._refresh_grid_view(keys, db_clients)
           
    def _get_selected_keys(self):
        """Get selected clients from current view"""
        if self.current_client_view == "list":
            return [it.data(Qt.UserRole) for it in self.lst_clients.selectedItems()]
        else:  # grid view
            return list(self.selected_client_cards)
    
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
    
    # NEW: Shutdown/Restart functions
    def shutdown_all_clients(self):
        """Shutdown all connected clients"""
        reply = QMessageBox.warning(
            self, "⚠️ Shutdown All Computers",
            "This will SHUTDOWN all connected client computers!\n\n"
            "Are you sure you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.server.broadcast_command("SHUTDOWN_PC")
            self.server.log("⚠️ Shutdown command sent to all clients")
            QMessageBox.information(self, "Shutdown Sent", "Shutdown command sent to all connected clients")
    
    def restart_all_clients(self):
        """Restart all connected clients"""
        reply = QMessageBox.warning(
            self, "🔄 Restart All Computers",
            "This will RESTART all connected client computers!\n\n"
            "Are you sure you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.server.broadcast_command("RESTART_PC")
            self.server.log("🔄 Restart command sent to all clients")
            QMessageBox.information(self, "Restart Sent", "Restart command sent to all connected clients")
    
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
            
    def manage_restrictions(self):
        dialog = RestrictionDialog(self, self.server.restrictions)
        
        if dialog.exec_() == QDialog.Accepted:
            new_restrictions = dialog.get_restrictions()
            
            if self.server.save_restrictions(new_restrictions):
                success_count = self.server.broadcast_restrictions()
                
                self._update_restriction_label()
                
                QMessageBox.information(
                    self,
                    "Restrictions Updated",
                    f"Restrictions have been updated and sent to {success_count} client(s).\n\n"
                    f"Keywords blocked: {len(new_restrictions['sites'])}\n"
                    f"Sites blocked: {len(new_restrictions['sites'])}"
                )
            else:
                QMessageBox.critical(
                    self,
                    "Error",
                    "Failed to save restrictions. Please try again."
                )
    
    def _update_restriction_label(self):
        keyword_count = len(self.server.restrictions.get('keywords', []))
        site_count = len(self.server.restrictions.get('sites', []))
        
        if keyword_count == 0 and site_count == 0:
            self.restriction_indicator.setText("No restrictions")
            self.restriction_indicator.setStyleSheet("color: #888; font-size: 10px; padding: 5px;")
        else:
            self.restriction_indicator.setText(
                f"🚫 {keyword_count} keywords, {site_count} sites"
            )
            self.restriction_indicator.setStyleSheet("color: #ff6b6b; font-size: 10px; font-weight: bold; padding: 5px;")
            
    def on_client_connected_auth(self, client_key, client_info):
        """Handle client connection with database storage"""
        if hasattr(self.server, 'db') and self.server.db:
            client_name = client_info.get('name', client_key)
            hostname = client_info.get('hostname', '')
            os_info = client_info.get('os', '')
            
            self.server.db.add_or_update_client(
                self.admin_ip,
                self.username,
                self.user_type,
                client_key,
                client_name,
                hostname,
                os_info
            )
            
            self.log(f"🟢 Client connected: {client_name} ({client_key})")
            self.update_client_list()

    def on_client_disconnected_auth(self, client_key):
        """Handle client disconnection"""
        if hasattr(self.server, 'db') and self.server.db:
            self.server.db.mark_client_disconnected(self.admin_ip, client_key)
            self.log(f"🔴 Client disconnected: {client_key}")
            self.update_client_list()
    
    def _show_client_context_menu(self, position: QPoint):
        item = self.lst_clients.itemAt(position)
        if not item:
            return
        
        # client_key = item.data(Qt.UserRole)
        
        if not item.isSelected():
            self.lst_clients.clearSelection()
            item.setSelected(True)
        
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #252526;
                color: #e0e0e0;
                border: 1px solid #3c3c3c;
            }
            QMenu::item:selected {
                background-color: #094771;
            }
        """)
        
        # # Check if connected
        # with self.server.clients_lock:
        #     is_connected = client_key in self.server.clients
        
        # if is_connected:
        #     status_action = menu.addAction("✅ Connected")
        #     status_action.setEnabled(False)
        # else:
        #     status_action = menu.addAction("🔴 Disconnected")
        #     status_action.setEnabled(False)
        
        # menu.addSeparator()
        
        # # Admin only: remove client
        # if self.user_type == 'admin':
        #     remove_action = menu.addAction("🗑️ Remove from List")
        #     remove_action.triggered.connect(lambda: self.remove_client_from_list(client_key))
            
        #     if is_connected:
        #         remove_action.setEnabled(False)
        #         remove_action.setText("🗑️ Remove (Disconnect first)")
        
        # menu.exec_(self.lst_clients.mapToGlobal(position))
        
        lock_action = menu.addAction("🔒 Lock Screen")
        lock_action.triggered.connect(lambda: self.send_to_selected("LOCK"))
        
        unlock_action = menu.addAction("🔓 Unlock Screen")
        unlock_action.triggered.connect(lambda: self.send_to_selected("UNLOCK"))
        
        menu.addSeparator()
        
        # NEW: Shutdown/Restart in context menu
        shutdown_action = menu.addAction("⚠️ Shutdown PC")
        shutdown_action.triggered.connect(self.shutdown_selected_clients)
        
        restart_action = menu.addAction("🔄 Restart PC")
        restart_action.triggered.connect(self.restart_selected_clients)
        
        menu.addSeparator()
        
        start_monitor_action = menu.addAction("▶️ Start Live View")
        start_monitor_action.triggered.connect(lambda: self.send_to_selected("START_SCREEN_STREAM"))
        
        stop_monitor_action = menu.addAction("⏹️ Stop Live View")
        stop_monitor_action.triggered.connect(lambda: self.send_to_selected("STOP_SCREEN_STREAM"))
        
        menu.addSeparator()
        
        send_file_action = menu.addAction("📤 Send File")
        send_file_action.triggered.connect(self.send_file_to_selected)
        
        menu.addSeparator()
        
        backup_action = menu.addAction("💾 Backup Files")
        backup_action.triggered.connect(self.backup_client_files)
        
        restore_action = menu.addAction("📥 Restore Files")
        restore_action.triggered.connect(self.restore_client_files)
        
        menu.addSeparator()
        
        message_action = menu.addAction("💬 Send Message")
        message_action.triggered.connect(self.send_message_to_selected)
        
        menu.addSeparator()

        remove_action = menu.addAction("🗑️ Remove from List")
        remove_action.triggered.connect(self.remove_selected_clients)
        
        menu.exec_(self.lst_clients.mapToGlobal(position))
        
    def remove_client_from_list(self, client_key):
        """Remove client from list (admin only)"""
        if self.user_type != 'admin':
            QMessageBox.warning(self, "Access Denied", "Only admins can remove clients")
            return
        
        reply = QMessageBox.question(self, "Remove Client",
                                    f"Permanently remove this client?\n\n{client_key}",
                                    QMessageBox.Yes | QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            if self.server.db.remove_client(self.admin_ip, client_key):
                self.log(f"🗑️ Client removed: {client_key}")
                self.update_client_list()
    
    def shutdown_selected_clients(self):
        """Shutdown selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            return
        
        reply = QMessageBox.warning(
            self, "⚠️ Shutdown Selected Computers",
            f"This will SHUTDOWN {len(keys)} selected computer(s)!\n\n"
            "Are you sure you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.send_to_selected("SHUTDOWN_PC")
            QMessageBox.information(self, "Shutdown Sent", f"Shutdown command sent to {len(keys)} client(s)")
    
    def restart_selected_clients(self):
        """Restart selected clients"""
        keys = self._get_selected_keys()
        if not keys:
            return
        
        reply = QMessageBox.warning(
            self, "🔄 Restart Selected Computers",
            f"This will RESTART {len(keys)} selected computer(s)!\n\n"
            "Are you sure you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.send_to_selected("RESTART_PC")
            QMessageBox.information(self, "Restart Sent", f"Restart command sent to {len(keys)} client(s)")
    
   
    
    def restore_client_files(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients to restore")
            return
        
        pc_names = []
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    pc_name = handler.client_info.get("hostname", k.split(":")[0])
                    pc_names.append(pc_name)
        
        dialog = RestoreConfigDialog(self, pc_names)
        if dialog.exec_() != QDialog.Accepted:
            return
        
        config = dialog.get_config()
        restore_path = config['restore_destination']
        backup_folders = config['backup_folders']
        
        if not restore_path:
            QMessageBox.warning(self, "Invalid Path", "Please specify a restore destination")
            return
        
        if not backup_folders:
            QMessageBox.warning(
                self, "No Backups Found",
                "No backup folders found. Please click 'Scan for Backups' first."
            )
            return
        
        matched = sum(1 for pc in pc_names if pc in backup_folders)
        
        reply = QMessageBox.question(
            self, "Confirm Restore",
            f"Restore files to {len(keys)} client(s)?\n\n"
            f"Matched backups: {matched}/{len(keys)}\n"
            f"Destination: {restore_path}\n\n"
            f"⚠️ WARNING: This will overwrite existing files!",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            success_count = self.server.restore_to_clients(keys, restore_path, backup_folders)
            QMessageBox.information(
                self, "Restore Started",
                f"Restore initiated for {success_count} client(s)\n\n"
                f"Files will be automatically distributed based on PC names.\n"
                f"Check the log for progress updates."
            )
    
    def send_file_to_all(self):
        """Send file to ALL connected clients"""
        with self.server.clients_lock:
            all_keys = [k for k, h in self.server.clients.items() if h.client_info.get("type", "student") != "teacher"]
        
        if not all_keys:
            QMessageBox.warning(self, "No Clients", "No clients are currently connected")
            return
        
        reply = QMessageBox.question(
            self, "Confirm Send to All",
            f"Send file to ALL {len(all_keys)} connected client(s)?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
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
            for k in all_keys:
                if k in self.server.clients:
                    threading.Thread(
                        target=self.server.clients[k].send_file_resumable,
                        args=(path, destination),
                        daemon=True
                    ).start()
                    sent += 1
        
        QMessageBox.information(
            self, "File Transfer to All",
            f"Starting transfer to ALL clients:\n"
            f"File: {os.path.basename(path)} ({format_bytes(filesize)})\n"
            f"Recipients: {sent} client(s)\n"
            f"Destination: {destination}"
        )
    
    
    def backup_client_files(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select clients first")
            return
        
        # Show file browser for first client
        selected_path = self.show_file_browser(keys[0])
        if not selected_path:
            return
        
        pc_names = []
        with self.server.clients_lock:
            for k in keys:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    pc_name = handler.client_info.get("hostname", k.split(":")[0])
                    pc_names.append(pc_name)
        
        dialog = BackupConfigDialog(self, pc_names)
        if dialog.exec_() != QDialog.Accepted:
            return
        
        config = dialog.get_config()
        source_path = config['source_path']
        backup_dest = config['destination']
        move_files = config['move_files']
        
        if not source_path:
            QMessageBox.warning(self, "Invalid Path", "Please specify a source path")
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_backup_dir = os.path.join(backup_dest, f"Backup_{timestamp}")
        os.makedirs(final_backup_dir, exist_ok=True)
        
        mode_text = "MOVE (delete from client)" if move_files else "COPY (keep on client)"
        
        reply = QMessageBox.question(
            self, "Confirm Backup",
            f"Request backup from {len(keys)} client(s)?\n\n"
            f"Mode: {mode_text}\n"
            f"Source (on clients): {source_path}\n"
            f"Destination (on this PC): {final_backup_dir}\n\n"
            f"Files will be organized by client PC name.\n"
            f"Backups will run in parallel for faster completion.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.server.custom_backup_dir = final_backup_dir
            
            try:
                # Send requests in parallel for faster initiation
                success_count = self.server.request_backup_from_clients(keys, source_path, move_files)
                
                mode_warning = "\n\n⚠️ Files will be DELETED from clients after backup!" if move_files else ""
                
                QMessageBox.information(
                    self, "Backup Started",
                    f"Backup request sent to {success_count} client(s)\n\n"
                    f"Mode: {mode_text}\n"
                    f"Files will be saved to:\n{final_backup_dir}\n\n"
                    f"Multiple backups will run simultaneously.\n"
                    f"Check the log for progress updates.{mode_warning}"
                )
            finally:
                self.server.custom_backup_dir = None


    def backup_all_clients(self):
        """Optimized backup for ALL connected clients"""
        with self.server.clients_lock:
            all_keys = [k for k, h in self.server.clients.items() 
                    if h.client_info.get("type", "student") != "teacher"]
        
        if not all_keys:
            QMessageBox.warning(self, "No Clients", "No clients are currently connected")
            return
        
        pc_names = []
        with self.server.clients_lock:
            for k in all_keys:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    pc_name = handler.client_info.get("hostname", k.split(":")[0])
                    pc_names.append(pc_name)
        
        reply = QMessageBox.question(
            self, "Confirm Backup All",
            f"Backup files from ALL {len(all_keys)} connected client(s)?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        dialog = BackupConfigDialog(self, pc_names)
        if dialog.exec_() != QDialog.Accepted:
            return
        
        config = dialog.get_config()
        source_path = config['source_path']
        backup_dest = config['destination']
        move_files = config['move_files']
        
        if not source_path:
            QMessageBox.warning(self, "Invalid Path", "Please specify a source path")
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_backup_dir = os.path.join(backup_dest, f"Backup_ALL_{timestamp}")
        os.makedirs(final_backup_dir, exist_ok=True)
        
        mode_text = "MOVE (delete from client)" if move_files else "COPY (keep on client)"
        
        reply = QMessageBox.question(
            self, "Confirm Backup All Clients",
            f"Request backup from ALL {len(all_keys)} client(s)?\n\n"
            f"Mode: {mode_text}\n"
            f"Source (on clients): {source_path}\n"
            f"Destination (on this PC): {final_backup_dir}\n\n"
            f"Files will be organized by client PC name.\n"
            f"All backups will run in parallel for maximum speed.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.server.custom_backup_dir = final_backup_dir
            
            try:
                # Parallel backup requests
                success_count = self.server.request_backup_from_clients(all_keys, source_path, move_files)
                
                mode_warning = "\n\n⚠️ Files will be DELETED from clients after backup!" if move_files else ""
                
                QMessageBox.information(
                    self, "Backup All Started",
                    f"Backup request sent to ALL {success_count} client(s)\n\n"
                    f"Mode: {mode_text}\n"
                    f"Files will be saved to:\n{final_backup_dir}\n\n"
                    f"All clients will backup simultaneously for faster completion.\n"
                    f"Check the log for progress updates.{mode_warning}"
                )
            finally:
                self.server.custom_backup_dir = None


    # Add this to your server class for optimized backup reception
    def receive_backup_data(self, client_socket, client_addr, zip_size, pc_name):
        """Optimized backup data receiver with streaming to disk"""
        try:
            backup_dir = self.custom_backup_dir or BACKUP_DIR
            safe_pc_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in pc_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            zip_filename = f"{safe_pc_name}_{timestamp}.zip"
            zip_path = os.path.join(backup_dir, zip_filename)
            
            # Increase socket buffer for better performance
            try:
                client_socket.setsockopt(socket.SOL_RCVBUF, 16 * 1024 * 1024)  # 16MB buffer
            except:
                pass
            
            # Stream directly to disk (no memory loading)
            chunk_size = 8 * 1024 * 1024  # 8MB chunks
            received = 0
            
            self.log(f"📥 Receiving backup from {pc_name}: {format_bytes(zip_size)}")
            
            with open(zip_path, 'wb') as f:
                while received < zip_size:
                    remaining = zip_size - received
                    to_receive = min(chunk_size, remaining)
                    
                    chunk = client_socket.recv(to_receive)
                    if not chunk:
                        raise ConnectionError("Connection lost during backup transfer")
                    
                    f.write(chunk)
                    received += len(chunk)
                    
                    # Log progress every 100MB or 10%
                    if received % (100 * 1024 * 1024) == 0 or \
                    (received % (zip_size // 10) == 0 and zip_size > 10 * 1024 * 1024):
                        percent = int((received / zip_size) * 100)
                        self.log(f"📥 {pc_name}: {percent}% ({format_bytes(received)} / {format_bytes(zip_size)})")
            
            self.log(f"✅ Backup received from {pc_name}: {zip_filename} ({format_bytes(zip_size)})")
            return True
            
        except Exception as e:
            self.log(f"❌ Failed to receive backup from {pc_name}: {e}")
            # Clean up partial file
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except:
                pass
            return False


    def request_backup_from_clients(self, client_keys, source_path, move_files=False):
        """Send backup requests to multiple clients in parallel"""
        success_count = 0
        
        def send_request(key):
            try:
                with self.clients_lock:
                    if key in self.clients:
                        handler = self.clients[key]
                        pc_name = handler.client_info.get("hostname", key.split(":")[0])
                        
                        command = f"BACKUP:{source_path}:{int(move_files)}\n"
                        handler.client_socket.sendall(command.encode("utf-8"))
                        
                        self.log(f"📤 Backup request sent to {pc_name}")
                        return True
            except Exception as e:
                self.log(f"❌ Failed to send backup request to {key}: {e}")
            return False
        
        # Send all requests in parallel using threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(client_keys), 20)) as executor:
            futures = {executor.submit(send_request, key): key for key in client_keys}
            
            for future in concurrent.futures.as_completed(futures):
                if future.result():
                    success_count += 1
        
        return success_count
    
    def restore_all_clients(self):
        """Restore files to ALL connected clients"""
        with self.server.clients_lock:
            all_keys = [k for k, h in self.server.clients.items() if h.client_info.get("type", "student") != "teacher"]
        
        if not all_keys:
            QMessageBox.warning(self, "No Clients", "No clients are currently connected")
            return
        
        pc_names = []
        with self.server.clients_lock:
            for k in all_keys:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    pc_name = handler.client_info.get("hostname", k.split(":")[0])
                    pc_names.append(pc_name)
        
        reply = QMessageBox.question(
            self, "Confirm Restore All",
            f"Restore files to ALL {len(all_keys)} connected client(s)?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        dialog = RestoreConfigDialog(self, pc_names)
        if dialog.exec_() != QDialog.Accepted:
            return
        
        config = dialog.get_config()
        restore_path = config['restore_destination']
        backup_folders = config['backup_folders']
        
        if not restore_path:
            QMessageBox.warning(self, "Invalid Path", "Please specify a restore destination")
            return
        
        if not backup_folders:
            QMessageBox.warning(
                self, "No Backups Found",
                "No backup folders found. Please click 'Scan for Backups' first."
            )
            return
        
        matched = sum(1 for pc in pc_names if pc in backup_folders)
        
        reply = QMessageBox.question(
            self, "Confirm Restore All Clients",
            f"Restore files to ALL {len(all_keys)} client(s)?\n\n"
            f"Matched backups: {matched}/{len(all_keys)}\n"
            f"Destination: {restore_path}\n\n"
            f"⚠️ WARNING: This will overwrite existing files!",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            success_count = self.server.restore_to_clients(all_keys, restore_path, backup_folders)
            QMessageBox.information(
                self, "Restore All Started",
                f"Restore initiated for ALL {success_count} client(s)\n\n"
                f"Files will be automatically distributed based on PC names.\n"
                f"Check the log for progress updates."
            )
    
    def toggle_presentation(self):
        if not self.server.presenting:
            keys = self._get_selected_keys()
            if not keys:
                QMessageBox.warning(self, "No Selection", "Select one or more clients")
                return
            
            quality = 85
            scale = 1.0
            
            self.server.presentation_quality = quality
            self.server.presentation_scale = scale
            
            self.server.start_presentation(keys)
            self.btn_present.setText("⏹️ Stop Presentation")
            self.btn_present.setStyleSheet("background-color: #d13438;")
        else:
            self.server.stop_presentation()
            self.btn_present.setText("📽️ Present My Screen")
            self.btn_present.setStyleSheet("background-color: #2d2d2d;")
    
    def save_preview_image(self):
        if not self.selected_preview_client:
            QMessageBox.warning(self, "No Client", "No client selected for preview")
            return
        
        with self.server.clients_lock:
            if self.selected_preview_client in self.server.clients:
                handler = self.server.clients[self.selected_preview_client]
                if handler.last_image:
                    filename, _ = QFileDialog.getSaveFileName(
                        self,
                        "Save Screenshot",
                        f"screenshot_{self.selected_preview_client.replace(':', '_')}.jpg",
                        "Images (*.jpg *.png)"
                    )
                    if filename:
                        try:
                            with open(filename, 'wb') as f:
                                f.write(handler.last_image)
                            QMessageBox.information(self, "Success", f"Image saved to:\n{filename}")
                        except Exception as e:
                            QMessageBox.critical(self, "Error", f"Failed to save image:\n{e}")
                else:
                    QMessageBox.warning(self, "No Image", "No image available from this client")
    
    def refresh_preview(self):
        if self.selected_preview_client:
            self._update_frames()
    
    def _display_image_bytes(self, image_data):
        """FIXED: Display image with proper error handling"""
        try:
            if not image_data:
                return
            
            qimg = QImage.fromData(QByteArray(image_data))
            if qimg.isNull():
                self.server.log("⚠️ Invalid image data received")
                return
            
            pixmap = QPixmap.fromImage(qimg)
            if pixmap.isNull():
                return
            
            # Scale to preview size
            scaled = pixmap.scaled(
                self.lbl_preview.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation
            )
            self.lbl_preview.setPixmap(scaled)
        except Exception as e:
            self.server.log(f"❌ Display error: {e}")
    
    def save_log(self):
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Log",
            f"admin_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)"
        )
        
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(self.txt_log.toPlainText())
                QMessageBox.information(self, "Success", f"Log saved to:\n{filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save log:\n{e}")
    
    
    def _create_statistics_view(self):
        """NEW: Create enhanced statistics view with modern UI"""
        view = QWidget()
        view.setStyleSheet("""
            QWidget {
                background-color: #1a1a1a;
            }
            QGroupBox {
                background-color: #242424;
                border: 2px solid #3daee9;
                border-radius: 10px;
                margin-top: 15px;
                padding: 15px;
                font-weight: bold;
                font-size: 12px;
                color: #3daee9;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 5px 10px;
                background-color: #3daee9;
                color: white;
                border-radius: 5px;
            }
            QTableWidget {
                background-color: #2a2a2a;
                alternate-background-color: #252525;
                border: 1px solid #3daee9;
                border-radius: 8px;
                gridline-color: #3a3a3a;
                color: #e0e0e0;
                selection-background-color: #3daee9;
                selection-color: white;
            }
            QTableWidget::item {
                padding: 8px;
                border-bottom: 1px solid #333;
            }
            QTableWidget::item:selected {
                background-color: #3daee9;
                color: white;
            }
            QHeaderView::section {
                background-color: #2d2d2d;
                color: #3daee9;
                padding: 10px;
                border: none;
                border-bottom: 2px solid #3daee9;
                font-weight: bold;
                font-size: 11px;
            }
            QComboBox {
                background-color: #2a2a2a;
                color: #e0e0e0;
                border: 2px solid #3daee9;
                border-radius: 5px;
                padding: 8px;
                min-height: 25px;
                font-size: 11px;
            }
            QComboBox:hover {
                border-color: #5fd4ff;
                background-color: #2d2d2d;
            }
            QComboBox::drop-down {
                border: none;
                width: 30px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #3daee9;
                margin-right: 10px;
            }
            QComboBox QAbstractItemView {
                background-color: #2a2a2a;
                color: #e0e0e0;
                selection-background-color: #3daee9;
                selection-color: white;
                border: 2px solid #3daee9;
            }
            QPushButton {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3daee9, stop:1 #2980b9);
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #5fd4ff, stop:1 #3daee9);
            }
            QPushButton:pressed {
                background-color: #2980b9;
            }
            QLabel#infoLabel {
                background-color: #2a2a2a;
                border-left: 4px solid #3daee9;
                padding: 10px;
                border-radius: 5px;
                color: #e0e0e0;
            }
        """)
        
        layout = QVBoxLayout(view)
        layout.setSpacing(15)
        
        # Enhanced header with gradient background
        header_widget = QWidget()
        header_widget.setStyleSheet("""
            QWidget {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3daee9, stop:0.5 #2ecc71, stop:1 #9b59b6);
                border-radius: 10px;
            }
        """)
        header_layout = QHBoxLayout(header_widget)
        
        header_icon = QLabel("📊")
        header_icon.setFont(QFont("Segoe UI", 24))
        header_layout.addWidget(header_icon)
        
        header_text = QLabel("Application Usage Analytics")
        header_text.setFont(QFont("Segoe UI", 18, QFont.Bold))
        header_text.setStyleSheet("color: white; padding: 10px;")
        header_layout.addWidget(header_text)
        header_layout.addStretch()
        
        # Live indicator
        live_indicator = QLabel("🟢 LIVE")
        live_indicator.setFont(QFont("Segoe UI", 11, QFont.Bold))
        live_indicator.setStyleSheet("color: #2ecc71; padding: 10px;")
        header_layout.addWidget(live_indicator)
        
        layout.addWidget(header_widget)
        
        # Info panel
        info_label = QLabel("💡 Real-time monitoring of application usage across all connected clients")
        info_label.setObjectName("infoLabel")
        info_label.setFont(QFont("Segoe UI", 10))
        layout.addWidget(info_label)
        
        # Overall stats with enhanced styling
        overall_group = QGroupBox("🌐 Overall Statistics - Top Applications")
        overall_group.setStyleSheet("""
            QGroupBox {
                border: 2px solid #2ecc71;
            }
            QGroupBox::title {
                background-color: #2ecc71;
            }
        """)
        overall_layout = QVBoxLayout(overall_group)
        
        # Stats info row
        stats_info_layout = QHBoxLayout()
        
        self.total_apps_label = QLabel("📱 Total Apps: 0")
        self.total_apps_label.setStyleSheet("color: #2ecc71; font-weight: bold; font-size: 11px;")
        stats_info_layout.addWidget(self.total_apps_label)
        
        stats_info_layout.addStretch()
        
        self.monitoring_clients_label = QLabel("👥 Clients: 0")
        self.monitoring_clients_label.setStyleSheet("color: #3daee9; font-weight: bold; font-size: 11px;")
        stats_info_layout.addWidget(self.monitoring_clients_label)
        
        overall_layout.addLayout(stats_info_layout)
        
        self.overall_stats_table = QTableWidget()
        self.overall_stats_table.setColumnCount(3)
        self.overall_stats_table.setHorizontalHeaderLabels(["📱 Application", "📊 Usage Count", "📈 Percentage"])
        self.overall_stats_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.overall_stats_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.overall_stats_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.overall_stats_table.setMinimumHeight(250)
        self.overall_stats_table.setAlternatingRowColors(True)
        self.overall_stats_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.overall_stats_table.setSelectionMode(QTableWidget.SingleSelection)
        self.overall_stats_table.verticalHeader().setVisible(False)
        overall_layout.addWidget(self.overall_stats_table)
        
        layout.addWidget(overall_group)
        
        # Per-client stats with enhanced styling
        client_group = QGroupBox("👤 Per-Client Statistics")
        client_group.setStyleSheet("""
            QGroupBox {
                border: 2px solid #9b59b6;
            }
            QGroupBox::title {
                background-color: #9b59b6;
            }
        """)
        client_layout = QVBoxLayout(client_group)
        
        # Client selector row
        client_selector_layout = QHBoxLayout()
        client_selector_label = QLabel("Select Client:")
        client_selector_label.setStyleSheet("color: #e0e0e0; font-weight: bold; font-size: 11px;")
        client_selector_layout.addWidget(client_selector_label)
        
        self.stats_client_combo = QComboBox()
        self.stats_client_combo.currentTextChanged.connect(self.update_client_stats)
        client_selector_layout.addWidget(self.stats_client_combo, 1)
        
        client_layout.addLayout(client_selector_layout)
        
        # Client stats info
        self.client_apps_label = QLabel("📱 Applications: 0")
        self.client_apps_label.setStyleSheet("color: #9b59b6; font-weight: bold; font-size: 11px; margin-top: 5px;")
        client_layout.addWidget(self.client_apps_label)
        
        self.client_stats_table = QTableWidget()
        self.client_stats_table.setColumnCount(2)
        self.client_stats_table.setHorizontalHeaderLabels(["📱 Application", "📊 Usage Count"])
        self.client_stats_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.client_stats_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.client_stats_table.setMinimumHeight(250)
        self.client_stats_table.setAlternatingRowColors(True)
        self.client_stats_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.client_stats_table.setSelectionMode(QTableWidget.SingleSelection)
        self.client_stats_table.verticalHeader().setVisible(False)
        client_layout.addWidget(self.client_stats_table)
        
        layout.addWidget(client_group)
        
        # Control buttons
        button_layout = QHBoxLayout()
        
        btn_refresh = QPushButton("🔄 Refresh Now")
        btn_refresh.clicked.connect(self.update_statistics)
        btn_refresh.setStyleSheet("""
            QPushButton {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2ecc71, stop:1 #27ae60);
            }
            QPushButton:hover {
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3be682, stop:1 #2ecc71);
            }
        """)
        button_layout.addWidget(btn_refresh)
        
        btn_export = QPushButton("💾 Export Statistics")
        btn_export.clicked.connect(self.export_statistics)
        button_layout.addWidget(btn_export)
        
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        
        return view
    
    def update_statistics(self):
        """Update statistics tables with database integration"""
        # Try loading from database first
        if self.server.db:
            try:
                # Get top transferred files from database
                top_files = self.server.db.get_top_transferred_files(limit=20)
                
                if top_files:
                    self.overall_stats_table.setRowCount(len(top_files))
                    
                    total = sum(f['transfer_count'] for f in top_files)
                    
                    colors = ["#2ecc71", "#3498db", "#9b59b6", "#e74c3c", "#f39c12"]
                    
                    for row, file_stat in enumerate(top_files):
                        # Application name with icon
                        app_item = QTableWidgetItem(f"  {file_stat['filename']}")
                        if row < 5:
                            color = colors[row % len(colors)]
                            app_item.setForeground(QColor(color))
                            font = app_item.font()
                            font.setBold(True)
                            app_item.setFont(font)
                        self.overall_stats_table.setItem(row, 0, app_item)
                        
                        # Transfer count
                        count_item = QTableWidgetItem(str(file_stat['transfer_count']))
                        count_item.setForeground(QColor("#3daee9"))
                        self.overall_stats_table.setItem(row, 1, count_item)
                        
                        # Percentage with progress bar effect
                        pct = (file_stat['transfer_count'] / total * 100) if total > 0 else 0
                        pct_item = QTableWidgetItem(f"{pct:.1f}%")
                        
                        if pct >= 20:
                            pct_item.setForeground(QColor("#2ecc71"))
                        elif pct >= 10:
                            pct_item.setForeground(QColor("#3498db"))
                        elif pct >= 5:
                            pct_item.setForeground(QColor("#f39c12"))
                        else:
                            pct_item.setForeground(QColor("#e74c3c"))
                        
                        self.overall_stats_table.setItem(row, 2, pct_item)
                    
                    self.total_apps_label.setText(f"📱 Total Files: {len(top_files)}")
                    
                    # Update combo with client info
                    current = self.stats_client_combo.currentText()
                    self.stats_client_combo.clear()
                    
                    with self.server.clients_lock:
                        for key, handler in self.server.clients.items():
                            client_type = handler.client_info.get("type", "student")
                            if client_type != "teacher":
                                name = handler.client_info.get("hostname", key)
                                self.stats_client_combo.addItem(name, key)
                    
                    if current:
                        idx = self.stats_client_combo.findText(current)
                        if idx >= 0:
                            self.stats_client_combo.setCurrentIndex(idx)
                    
                    # Update monitoring clients count
                    with self.server.clients_lock:
                        client_count = sum(1 for k, h in self.server.clients.items() 
                                        if h.client_info.get("type", "student") != "teacher")
                    self.monitoring_clients_label.setText(f"👥 Clients: {client_count}")
                    
                    return  # Successfully loaded from database
            except Exception as e:
                self.server.log(f"⚠️ Database stats load failed, using memory: {e}")
        
        # Fallback to in-memory statistics (original code)
        overall_stats = self.server.get_statistics_summary()
        self.overall_stats_table.setRowCount(len(overall_stats))
        
        total = sum(count for _, count in overall_stats)
        
        colors = ["#2ecc71", "#3498db", "#9b59b6", "#e74c3c", "#f39c12"]
        
        for row, (app, count) in enumerate(overall_stats):
            app_item = QTableWidgetItem(f"  {app}")
            if row < 5:
                color = colors[row % len(colors)]
                app_item.setForeground(QColor(color))
                font = app_item.font()
                font.setBold(True)
                app_item.setFont(font)
            self.overall_stats_table.setItem(row, 0, app_item)
            
            count_item = QTableWidgetItem(str(count))
            count_item.setForeground(QColor("#3daee9"))
            self.overall_stats_table.setItem(row, 1, count_item)
            
            pct = (count / total * 100) if total > 0 else 0
            pct_item = QTableWidgetItem(f"{pct:.1f}%")
            
            if pct >= 20:
                pct_item.setForeground(QColor("#2ecc71"))
            elif pct >= 10:
                pct_item.setForeground(QColor("#3498db"))
            elif pct >= 5:
                pct_item.setForeground(QColor("#f39c12"))
            else:
                pct_item.setForeground(QColor("#e74c3c"))
            
            self.overall_stats_table.setItem(row, 2, pct_item)
        
        self.total_apps_label.setText(f"📱 Total Apps: {len(overall_stats)}")
        
        current = self.stats_client_combo.currentText()
        self.stats_client_combo.clear()
        
        with self.server.clients_lock:
            for key, handler in self.server.clients.items():
                client_type = handler.client_info.get("type", "student")
                if client_type != "teacher":
                    name = handler.client_info.get("hostname", key)
                    self.stats_client_combo.addItem(name, key)
        
        if current:
            idx = self.stats_client_combo.findText(current)
            if idx >= 0:
                self.stats_client_combo.setCurrentIndex(idx)
    
    def update_client_stats(self):
        """Update per-client statistics with enhanced visuals"""
        key = self.stats_client_combo.currentData()
        if not key:
            self.client_apps_label.setText("📱 Applications: 0")
            return
        
        stats = self.server.get_client_statistics(key)
        self.client_stats_table.setRowCount(len(stats))
        
        # Update client apps count
        self.client_apps_label.setText(f"📱 Applications: {len(stats)}")
        
        colors = ["#9b59b6", "#3498db", "#2ecc71", "#f39c12", "#e74c3c"]
        
        for row, (app, count) in enumerate(stats):
            # Application name with color
            app_item = QTableWidgetItem(f"  {app}")
            if row < 3:  # Top 3 get special color
                color = colors[row % len(colors)]
                app_item.setForeground(QColor(color))
                font = app_item.font()
                font.setBold(True)
                app_item.setFont(font)
            self.client_stats_table.setItem(row, 0, app_item)
            
            # Usage count
            count_item = QTableWidgetItem(str(count))
            count_item.setForeground(QColor("#9b59b6"))
            self.client_stats_table.setItem(row, 1, count_item)
    
    def export_statistics(self):
        """Export statistics to a file"""
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export Statistics",
            f"statistics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt);;CSV Files (*.csv)"
        )
        
        if not filename:
            return
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("=" * 80 + "\n")
                f.write("APPLICATION USAGE STATISTICS\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 80 + "\n\n")
                
                # Overall stats
                f.write("OVERALL STATISTICS - TOP APPLICATIONS\n")
                f.write("-" * 80 + "\n")
                overall_stats = self.server.get_statistics_summary()
                total = sum(count for _, count in overall_stats)
                
                for i, (app, count) in enumerate(overall_stats, 1):
                    pct = (count / total * 100) if total > 0 else 0
                    f.write(f"{i:2d}. {app:50s} | Count: {count:6d} | {pct:5.1f}%\n")
                
                f.write("\n" + "=" * 80 + "\n\n")
                
                # Per-client stats
                f.write("PER-CLIENT STATISTICS\n")
                f.write("-" * 80 + "\n")
                
                with self.server.clients_lock:
                    for key, handler in self.server.clients.items():
                        client_type = handler.client_info.get("type", "student")
                        if client_type != "teacher":
                            name = handler.client_info.get("hostname", key)
                            stats = self.server.get_client_statistics(key)
                            
                            f.write(f"\nClient: {name} ({key})\n")
                            f.write("-" * 40 + "\n")
                            
                            if stats:
                                for i, (app, count) in enumerate(stats, 1):
                                    f.write(f"  {i:2d}. {app:40s} | Count: {count:6d}\n")
                            else:
                                f.write("  No statistics available\n")
                
                f.write("\n" + "=" * 80 + "\n")
                f.write("END OF REPORT\n")
            
            QMessageBox.information(self, "Export Success", f"Statistics exported to:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export statistics:\n{e}")


    def closeEvent(self, event):
        if hasattr(self.server, 'sync_manager'):
            self.server.sync_manager.stop_sync_cycle()
        event.accept()
        
    def show_database_dashboard(self):
        """Show comprehensive database statistics dashboard"""
        if not self.server.db:
            QMessageBox.warning(
                self, 
                "Database Not Available", 
                "Database tracking is not enabled.\n\n"
                "To enable:\n"
                "1. Start XAMPP MySQL\n"
                "2. Run the SQL schema\n"
                "3. Restart the application"
            )
            return
        
        try:
            # Get various statistics
            success_rate = self.server.db.get_success_rate(days=30)
            history = self.server.db.get_transfer_history(days=7)
            active_transfers = self.server.db.get_active_transfers()
            client_performance = self.server.db.get_client_performance()
            
            # Build comprehensive dashboard
            dashboard = f"""
    <html>
    <head>
    <style>
    body {{
        font-family: 'Segoe UI', Arial, sans-serif;
        background-color: #1e1e1e;
        color: #e0e0e0;
        padding: 20px;
    }}
    h2 {{
        color: #3daee9;
        border-bottom: 2px solid #3daee9;
        padding-bottom: 10px;
    }}
    h3 {{
        color: #2ecc71;
        margin-top: 20px;
    }}
    .stat-box {{
        background-color: #2a2a2a;
        border-left: 4px solid #3daee9;
        padding: 15px;
        margin: 10px 0;
        border-radius: 5px;
    }}
    .success {{ color: #2ecc71; font-weight: bold; }}
    .failed {{ color: #e74c3c; font-weight: bold; }}
    .warning {{ color: #f39c12; font-weight: bold; }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin: 10px 0;
    }}
    th {{
        background-color: #3daee9;
        color: white;
        padding: 10px;
        text-align: left;
    }}
    td {{
        padding: 8px;
        border-bottom: 1px solid #3c3c3c;
    }}
    tr:hover {{
        background-color: #2a2a2a;
    }}
    </style>
    </head>
    <body>

    <h2>📊 Database Transfer Statistics Dashboard</h2>

    <div class="stat-box">
    <h3>Overall Performance (Last 30 Days)</h3>
    <table>
    <tr>
        <td><b>Total Transfers:</b></td>
        <td>{success_rate.get('total', 0)}</td>
    </tr>
    <tr>
        <td><b>Successful:</b></td>
        <td class="success">{success_rate.get('successful', 0)}</td>
    </tr>
    <tr>
        <td><b>Failed:</b></td>
        <td class="failed">{success_rate.get('failed', 0)}</td>
    </tr>
    <tr>
        <td><b>In Progress:</b></td>
        <td class="warning">{success_rate.get('in_progress', 0)}</td>
    </tr>
    <tr>
        <td><b>Success Rate:</b></td>
        <td class="success">{success_rate.get('success_rate', 0):.1f}%</td>
    </tr>
    </table>
    </div>
    """
            
            # Active Transfers
            if active_transfers:
                dashboard += """
    <div class="stat-box">
    <h3>🔄 Active Transfers</h3>
    <table>
    <tr>
        <th>Filename</th>
        <th>Source</th>
        <th>Destination</th>
        <th>Progress</th>
        <th>Speed</th>
    </tr>
    """
                for transfer in active_transfers[:10]:
                    dashboard += f"""
    <tr>
        <td>{transfer['filename']}</td>
        <td>{transfer['source_id']}</td>
        <td>{transfer['destination_id']}</td>
        <td>{transfer.get('progress_percent', 0):.1f}%</td>
        <td>{transfer.get('transfer_speed_mbps', 0) or 0:.1f} Mbps</td>
    </tr>
    """
                dashboard += "</table></div>"
            
            # Recent History
            if history:
                dashboard += """
    <div class="stat-box">
    <h3>📈 Recent History (Last 7 Days)</h3>
    <table>
    <tr>
        <th>Date</th>
        <th>Total</th>
        <th>Successful</th>
        <th>Failed</th>
        <th>Data Transferred</th>
        <th>Avg Speed</th>
    </tr>
    """
                for day in history:
                    total_bytes = day.get('total_bytes', 0) or 0
                    avg_speed = day.get('avg_speed', 0) or 0
                    dashboard += f"""
    <tr>
        <td>{day['date']}</td>
        <td>{day['total_transfers']}</td>
        <td class="success">{day['successful']}</td>
        <td class="failed">{day['failed']}</td>
        <td>{format_bytes(total_bytes)}</td>
        <td>{avg_speed:.1f} Mbps</td>
    </tr>
    """
                dashboard += "</table></div>"
            
            # Client Performance
            if client_performance:
                dashboard += """
    <div class="stat-box">
    <h3>👥 Client Performance</h3>
    <table>
    <tr>
        <th>Client</th>
        <th>Transfers</th>
        <th>Success Rate</th>
        <th>Total Data</th>
        <th>Avg Speed</th>
    </tr>
    """
                for client in client_performance[:10]:
                    total_bytes = client.get('total_bytes', 0) or 0
                    avg_speed = client.get('avg_speed', 0) or 0
                    success_rate_val = client.get('success_rate', 0) or 0
                    dashboard += f"""
    <tr>
        <td>{client.get('client_id', 'Unknown')}</td>
        <td>{client.get('total_transfers', 0)}</td>
        <td>{success_rate_val:.1f}%</td>
        <td>{format_bytes(total_bytes)}</td>
        <td>{avg_speed:.1f} Mbps</td>
    </tr>
    """
                dashboard += "</table></div>"
            
            dashboard += """
    </body>
    </html>
    """
            
            # Create dialog with HTML viewer
            from PyQt5.QtWidgets import QTextBrowser
            dialog = QDialog(self)
            dialog.setWindowTitle("Database Statistics Dashboard")
            dialog.resize(900, 700)
            dialog.setStyleSheet(self.styleSheet())
            
            layout = QVBoxLayout(dialog)
            
            browser = QTextBrowser()
            browser.setHtml(dashboard)
            browser.setOpenExternalLinks(False)
            layout.addWidget(browser)
            
            btn_layout = QHBoxLayout()
            
            btn_export = QPushButton("📄 Export to JSON")
            btn_export.clicked.connect(lambda: self.export_database_to_json())
            btn_layout.addWidget(btn_export)
            
            btn_layout.addStretch()
            
            btn_close = QPushButton("Close")
            btn_close.clicked.connect(dialog.close)
            btn_layout.addWidget(btn_close)
            
            layout.addLayout(btn_layout)
            
            dialog.exec_()
            
        except Exception as e:
            QMessageBox.critical(
                self, 
                "Error", 
                f"Failed to load database dashboard:\n{str(e)}\n\n"
                f"Make sure XAMPP MySQL is running."
            )
            self.server.log(f"❌ Database dashboard error: {e}")
    def export_database_to_json(self):
        """Export database statistics to JSON file"""
        if not self.server.db:
            QMessageBox.warning(self, "Database Not Available", 
                            "Database tracking is not enabled")
            return
        
        try:
            filename, _ = QFileDialog.getSaveFileName(
                self,
                "Export Database Statistics",
                f"lab_manager_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                "JSON Files (*.json)"
            )
            
            if not filename:
                return
            
            success = self.server.db.export_to_json(filename, days=90)
            
            if success:
                QMessageBox.information(
                    self,
                    "Export Successful",
                    f"Database statistics exported to:\n{filename}\n\n"
                    f"Contains last 90 days of transfer data."
                )
                self.server.log(f"📊 Database exported to: {filename}")
            else:
                QMessageBox.critical(
                    self,
                    "Export Failed",
                    "Failed to export database statistics."
                )
        
        except Exception as e:
            QMessageBox.critical(
                self,
                "Export Error",
                f"Failed to export database:\n{e}"
            )
            
    def show_teacher_management(self):
        """Show teacher management dialog"""
        from PyQt5.QtWidgets import QDialog, QTableWidget, QHeaderView, QTableWidgetItem
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Teacher Management")
        dialog.resize(800, 500)
        dialog.setStyleSheet(self.styleSheet())
        
        layout = QVBoxLayout(dialog)
        
        # Header
        header = QLabel("👩‍🏫 Teacher Account Management")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet("color: #3daee9; padding: 10px;")
        layout.addWidget(header)
        
        # Teacher table
        table = QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels([
            "Username", "Full Name", "Last Login", "IP Address", "Created", "Status"
        ])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(table)
        
        # Load teachers
        teachers = self.db.get_all_teachers()
        table.setRowCount(len(teachers))
        
        for row, teacher in enumerate(teachers):
            table.setItem(row, 0, QTableWidgetItem(teacher['username']))
            table.setItem(row, 1, QTableWidgetItem(teacher['full_name']))
            
            last_login = teacher['last_login'].strftime("%Y-%m-%d %H:%M") if teacher['last_login'] else "Never"
            table.setItem(row, 2, QTableWidgetItem(last_login))
            
            table.setItem(row, 3, QTableWidgetItem(teacher['ip_address'] or "N/A"))
            table.setItem(row, 4, QTableWidgetItem(teacher['created_at'].strftime("%Y-%m-%d")))
            
            status = "Active" if teacher['is_active'] else "Inactive"
            status_item = QTableWidgetItem(status)
            status_item.setForeground(QColor("#2ecc71" if teacher['is_active'] else "#e74c3c"))
            table.setItem(row, 5, status_item)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        btn_add = QPushButton("• Add Teacher")
        btn_add.clicked.connect(lambda: self.add_teacher_dialog(dialog, table))
        btn_layout.addWidget(btn_add)
        
        btn_layout.addStretch()
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dialog.close)
        btn_layout.addWidget(btn_close)
        
        layout.addLayout(btn_layout)
        
        dialog.exec_()

    def add_teacher_dialog(self, parent, table):
        """Dialog to add new teacher"""
        username, ok1 = QInputDialog.getText(parent, "Add Teacher", "Enter username:")
        if not ok1 or not username:
            return
        
        full_name, ok2 = QInputDialog.getText(parent, "Add Teacher", "Enter full name:")
        if not ok2 or not full_name:
            return
        
        password, ok3 = QInputDialog.getText(parent, "Add Teacher", "Enter password:", QLineEdit.Password)
        if not ok3 or not password:
            return
        
        confirm_password, ok4 = QInputDialog.getText(parent, "Add Teacher", "Confirm password:", QLineEdit.Password)
        if not ok4 or confirm_password != password:
            QMessageBox.warning(parent, "Error", "Passwords don't match")
            return
        
        # Create teacher
        success = self.db.create_teacher(username, password, full_name, self.admin_user['admin_id'])
        
        if success:
            QMessageBox.information(parent, "Success", f"Teacher account created:\n\nUsername: {username}\nName: {full_name}")
            # Refresh table
            self.show_teacher_management()
        else:
            QMessageBox.critical(parent, "Error", "Failed to create teacher account")
            
            
    def remove_selected_clients(self):
        """Remove selected clients from database"""
        keys = self._get_selected_keys()
        if not keys:
            return
        
        reply = QMessageBox.question(
            self, "Remove Clients",
            f"Permanently remove {len(keys)} client(s) from the list?\n\n"
            "This will delete their connection history.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            removed = 0
            for key in keys:
                client_ip = key.split(":")[0]
                if self.server.db and self.server.admin_ip:
                    if self.server.db.remove_client(self.server.admin_ip, client_ip):
                        removed += 1
            
            QMessageBox.information(self, "Removed", f"Removed {removed} client(s) from list")
        self.refresh_clients()
        
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Lab Manager - Admin")
    
    window = AdminWindow()
    window.show()
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()