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
    QTextEdit, QTabWidget, QGroupBox, QComboBox, QInputDialog, QLineEdit,
    QDialog, QCheckBox, QRadioButton, QButtonGroup, QMenu, QSplitter,
    QFrame, QScrollArea
)
from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal, QPoint, QSize
from PyQt5.QtGui import QPixmap, QImage, QFont, QIcon, QCursor
from PyQt5.QtCore import QByteArray

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
    
    

class RestrictionDialog(QDialog):
    def __init__(self, parent=None, current_restrictions=None):
        super().__init__(parent)
        self.setWindowTitle("Site & Keyword Restrictions")
        self.resize(700, 600)
        
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
            QLineEdit, QTextEdit, QListWidget {
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
        
        self.restrictions = current_restrictions or {"keywords": [], "sites": []}
        
        layout = QVBoxLayout(self)
        
        header = QLabel("🚫 Content Restriction Management")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet("color: #d13438; padding: 10px;")
        layout.addWidget(header)
        
        info = QLabel("Block websites and search keywords on all client computers")
        info.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(info)
        
        keyword_group = QGroupBox("🔍 Blocked Keywords")
        keyword_layout = QVBoxLayout(keyword_group)
        
        keyword_info = QLabel("Keywords to block in searches (e.g., 'porn', 'violence', 'drugs')")
        keyword_info.setStyleSheet("color: #888; font-size: 10px;")
        keyword_layout.addWidget(keyword_info)
        
        self.keyword_list = QListWidget()
        for keyword in self.restrictions.get("keywords", []):
            self.keyword_list.addItem(keyword)
        keyword_layout.addWidget(self.keyword_list)
        
        keyword_btn_layout = QHBoxLayout()
        self.txt_keyword = QLineEdit()
        self.txt_keyword.setPlaceholderText("Enter keyword to block...")
        keyword_btn_layout.addWidget(self.txt_keyword)
        
        btn_add_keyword = QPushButton("➕ Add")
        btn_add_keyword.clicked.connect(self._add_keyword)
        keyword_btn_layout.addWidget(btn_add_keyword)
        
        btn_remove_keyword = QPushButton("➖ Remove")
        btn_remove_keyword.clicked.connect(self._remove_keyword)
        keyword_btn_layout.addWidget(btn_remove_keyword)
        
        keyword_layout.addLayout(keyword_btn_layout)
        layout.addWidget(keyword_group)
        
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
    
    def _add_keyword(self):
        keyword = self.txt_keyword.text().strip().lower()
        if keyword and keyword not in [self.keyword_list.item(i).text() for i in range(self.keyword_list.count())]:
            self.keyword_list.addItem(keyword)
            self.txt_keyword.clear()
    
    def _remove_keyword(self):
        current = self.keyword_list.currentItem()
        if current:
            self.keyword_list.takeItem(self.keyword_list.row(current))
    
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
    
    def _preset_social_media(self):
        social_sites = [
            "facebook.com", "twitter.com", "instagram.com", 
            "tiktok.com", "snapchat.com", "reddit.com"
        ]
        for site in social_sites:
            if site not in [self.site_list.item(i).text() for i in range(self.site_list.count())]:
                self.site_list.addItem(site)
    
    def _preset_adult_content(self):
        adult_keywords = ["porn", "xxx", "adult", "sex", "nude", "nsfw"]
        for keyword in adult_keywords:
            if keyword not in [self.keyword_list.item(i).text() for i in range(self.keyword_list.count())]:
                self.keyword_list.addItem(keyword)
    
    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear All Restrictions",
            "Remove all keyword and site restrictions?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.keyword_list.clear()
            self.site_list.clear()
    
    def get_restrictions(self):
        keywords = [self.keyword_list.item(i).text() for i in range(self.keyword_list.count())]
        sites = [self.site_list.item(i).text() for i in range(self.site_list.count())]
        return {"keywords": keywords, "sites": sites}


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
        self.base_sync_dir = base_sync_dir or os.path.join(os.path.expanduser("~"), "lab_sync")
        os.makedirs(self.base_sync_dir, exist_ok=True)
        
        self.file_hashes = {}
        self.client_configs = {}
        self.sync_thread = None
        self.sync_running = False
        self.server.log(f"File sync manager initialized at {self.base_sync_dir}")
    
    def set_global_source_path(self, source_path):
        self.global_source_path = source_path
        self.server.log(f"Global source path set to: {source_path}")
    
    def start_sync_cycle(self, sync_interval=30):
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
        self.sync_running = False
        self.server.log("AUTO SYNC stopped")
    
    def _sync_client_files(self, client_key, source_path):
        cmd = f"COLLECT_FILES:{source_path}"
        
        with self.server.clients_lock:
            if client_key in self.server.clients:
                handler = self.server.clients[client_key]
                handler.send_command(cmd)
    
    def receive_file_list(self, client_key, files_data):
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
                
                old_hash = self.file_hashes[client_key].get(file_path)
                
                if old_hash is None:
                    new_files.append(file_path)
                    self.server.log(f"NEW: {client_key} -> {file_path}")
                elif old_hash != file_hash:
                    modified_files.append(file_path)
                    self.server.log(f"MODIFIED: {client_key} -> {file_path}")
                
                self.file_hashes[client_key][file_path] = file_hash
            
            files_to_request = new_files + modified_files
            for file_path in files_to_request:
                self._request_file_from_client(client_key, file_path)
            
            if files_to_request:
                self.server.log(f"Sync {client_key}: {len(new_files)} new, {len(modified_files)} modified")
            
        except Exception as e:
            self.server.log(f"Error processing file list from {client_key}: {e}")
    
    def _request_file_from_client(self, client_key, file_path):
        cmd = f"SEND_FILE_TO_ADMIN:{file_path}"
        
        with self.server.clients_lock:
            if client_key in self.server.clients:
                handler = self.server.clients[client_key]
                handler.send_command(cmd)
    
    def receive_file_from_client(self, client_key, file_info, file_data):
        try:
            file_path = file_info.get("path", "unknown")
            file_name = file_info.get("name", "file")
            
            client_dir = os.path.join(self.base_sync_dir, client_key.replace(":", "_"))
            os.makedirs(client_dir, exist_ok=True)
            
            relative_path = file_path.replace("\\", "/")
            save_path = os.path.join(client_dir, relative_path.lstrip("/"))
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            with open(save_path, "wb") as f:
                f.write(file_data)
            
            self.server.log(f"SAVED: {client_key} -> {save_path}")
            
        except Exception as e:
            self.server.log(f"Error saving file from {client_key}: {e}")
    
    def get_sync_status(self):
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
        self.last_image_ts = None
        self.connected_time = time.time()
        self.frames_received = 0
        self.bytes_received = 0
        self.last_heartbeat = time.time()
        self.client_info = {"hostname": f"Client-{addr[0]}", "status": "connected"}
        
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
        """OPTIMIZED: Faster resumable file transfer"""
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
                
                self.sock.settimeout(2.0)
                buffer = b""
                ready_received = False
                start_wait = time.time()
                
                self.server.log(f"⏳ Waiting for client READY...")
                
                while not ready_received:
                    if (time.time() - start_wait) >= 30:
                        self.server.log(f"❌ Timeout waiting for READY after 30 seconds")
                        self.server.log(f"💡 Last buffer content: {buffer[:100]}")
                        raise Exception("Timeout waiting for READY")
                    
                    try:
                        chunk = self.sock.recv(1024)
                        if not chunk:
                            self.server.log(f"❌ Connection closed while waiting for READY")
                            raise Exception("Connection closed waiting for READY")
                        buffer += chunk
                    except socket.timeout:
                        continue
                    except OSError as e:
                        self.server.log(f"⚠️ Socket error waiting for READY: {e}")
                        return False
                    
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        msg = line.decode('utf-8', errors='ignore').strip()
                        
                        if not msg:
                            continue
                        
                        self.server.log(f"📝 {self.key}: {msg}")
                        
                        msg_upper = msg.upper()
                        if "READY" in msg_upper:
                            ready_received = True
                            self.server.log(f"✅ READY received (matched: '{msg}') - breaking wait loop")
                            break
                        elif "HEARTBEAT" in msg_upper:
                            continue
                        elif "ERROR" in msg_upper:
                            error_detail = msg.split(":", 1)[1] if ":" in msg else "Unknown"
                            self.server.log(f"❌ Client error: {error_detail}")
                            raise Exception(f"Client error: {error_detail}")
                    
                    if ready_received:
                        break
                
                if not ready_received:
                    self.server.log(f"❌ READY not received")
                    raise Exception("READY not received")
                
                self.server.log("✅ Client READY confirmed - Starting chunk transfer")
                self.sock.settimeout(0.1)  # FIXED: Restore timeout after READY wait
                
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
                            f.seek(chunk_index * chunk_size)
                            chunk_data = f.read(chunk_size)
                            if not chunk_data:
                                break
                            
                            checksum = transfer._calculate_chunk_checksum(chunk_data)
                            
                            chunk_header = struct.pack(">II", chunk_index, len(chunk_data))
                            chunk_header += checksum.encode('utf-8').ljust(64, b'\x00')
                            
                            try:
                                self.sock.sendall(chunk_header + chunk_data)
                            except OSError:
                                self.server.log(f"⚠️ Socket closed during chunk {chunk_index}")
                                transfer.save_progress_batch()
                                return False
                            
                            if chunk_delay > 0:
                                time.sleep(chunk_delay)
                            
                            sent_bytes += len(chunk_data)
                            chunks_in_batch.append((chunk_index, checksum))
                            
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
                                        
                                        if "CHUNK_OK" in msg or "OK" in msg:
                                            ack_received = True
                                            self.server.log(f"✅ ACK received for batch")
                                            break
                                        elif "HEARTBEAT" in msg:
                                            continue
                                        elif "CHUNK_ERROR" in msg or "ERROR" in msg:
                                            raise Exception(f"Client error at chunk {chunk_index}")
                                
                                if not ack_received:
                                    self.server.log(f"❌ Timeout waiting for ACK at chunk {chunk_index}")
                                    raise Exception(f"Timeout waiting for ACK at chunk {chunk_index}")
                                
                                for c_idx, c_sum in chunks_in_batch:
                                    transfer.mark_chunk_complete(c_idx, c_sum)
                                
                                transfer.save_progress_batch()
                                chunks_in_batch = []
                                
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
                
                self.sock.settimeout(5.0)
                complete_buffer = b""
                complete_received = False
                complete_start = time.time()
                
                while not complete_received and (time.time() - complete_start) < 5:
                    try:
                        chunk = self.sock.recv(1024)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    
                    if not chunk:
                        break
                    
                    complete_buffer += chunk
                    while b'\n' in complete_buffer:
                        line, complete_buffer = complete_buffer.split(b'\n', 1)
                        msg = line.decode('utf-8', errors='ignore').strip().upper()
                        
                        if msg == "TRANSFER_COMPLETE":
                            complete_received = True
                            break
                        elif msg == "HEARTBEAT":
                            continue
                
                self.sock.settimeout(None)
                
                if complete_received:
                    elapsed = time.time() - start_time
                    rate = filesize / elapsed if elapsed > 0 else 0
                    self.server.log(f"✅ {basename} complete | {format_bytes(rate)}/s | {elapsed:.1f}s")
                    transfer.cleanup()
                    return True
                else:
                    self.server.log(f"⚠️ No completion confirmation")
                    return False
                
        except Exception as e:
            self.server.log(f"❌ Transfer error: {e}")
            return False
        finally:
            self.transferring.clear()
            # FIXED: Restore socket state for _reader_loop
            try:
                self.sock.setblocking(False)
                self.sock.settimeout(0.1)
            except:
                pass

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
                                
                                # Check if this is a teacher client
                                client_type = self.client_info.get("type", "student")
                                if client_type == "teacher":
                                    with self.server.teacher_lock:
                                        self.server.teacher_clients[self.key] = True
                                    self.server.log(f"👨‍🏫 Teacher client connected: {self.key}")
                                    # Send client list in a separate thread to avoid blocking
                                    threading.Thread(target=self.server.broadcast_client_list_to_teachers, daemon=True).start()
                                    continue
                                
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
                                    # Notify teachers in separate thread
                                    threading.Thread(target=self.server.broadcast_client_list_to_teachers, daemon=True).start()
                                
                            except Exception as e:
                                self.server.log(f"⚠️ Failed to parse INFO from {self.key}: {e}")

                        elif header.startswith("TEACHER_FILE"):
                            # Teacher sending file with full path
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
                                filepath = metadata.get('filepath', 'unknown')  # FULL PATH FROM TEACHER
                                
                                self.server.log(f"📥 Receiving file from teacher: {filename}")
                                self.server.log(f"📁 Source path: {filepath}")
                                
                                # Receive file data
                                file_data = b""
                                remaining = filesize
                                
                                while remaining > 0:
                                    chunk = self.sock.recv(min(RECV_BUFFER, remaining))
                                    if not chunk:
                                        raise ConnectionError("Connection closed reading file data")
                                    file_data += chunk
                                    remaining -= len(chunk)
                                    buffer = b""  # Clear buffer during file transfer
                                
                                # Wait for end marker
                                end_marker = b""
                                while not end_marker.endswith(b"<FILE_END>"):
                                    chunk = self.sock.recv(1024)
                                    if not chunk:
                                        break
                                    end_marker += chunk
                                    if len(end_marker) > 20:
                                        end_marker = end_marker[-20:]
                                
                                # Save file to inbox with metadata
                                save_path = os.path.join(INBOX_DIR, f"teacher_{filename}")
                                with open(save_path, 'wb') as f:
                                    f.write(file_data)
                                
                                # Save metadata file with path info
                                metadata_path = save_path + ".meta.json"
                                with open(metadata_path, 'w') as f:
                                    json.dump(metadata, f, indent=2)
                                
                                self.server.log(f"✅ Teacher file saved: {save_path}")
                                self.server.log(f"📋 Original path: {filepath}")
                                self.server.log(f"ℹ️ Metadata saved to: {metadata_path}")
                                
                            except Exception as e:
                                self.server.log(f"❌ Error receiving teacher file: {e}")
                        
                        elif header.startswith("TEACHER_START_PRESENTATION:"):
                            # Teacher wants to present to specific clients
                            try:
                                targets_json = header.split(":", 1)[1]
                                target_clients = json.loads(targets_json)
                                self.server.log(f"📽️ Teacher {self.key} starting presentation to {len(target_clients)} client(s)")
                                
                                # Store teacher's presentation targets
                                self.teacher_presentation_targets = target_clients
                                
                                # Tell clients to start receiving presentation
                                with self.server.clients_lock:
                                    for target in target_clients:
                                        # Find client by matching hostname or key
                                        for client_key, handler in self.server.clients.items():
                                            client_hostname = handler.client_info.get("hostname", "")
                                            # Match by hostname or full key string
                                            if target in client_hostname or target == client_key:
                                                handler.send_command("START_PRESENTATION")
                                                self.server.log(f"   ▸ Presentation started for {client_key}")
                                                break
                                
                            except Exception as e:
                                self.server.log(f"❌ Error starting teacher presentation: {e}")
                        
                        elif header.startswith("TEACHER_STOP_PRESENTATION"):
                            # Teacher stopped presenting
                            self.server.log(f"⏹️ Teacher {self.key} stopped presenting")
                            
                            if hasattr(self, 'teacher_presentation_targets'):
                                with self.server.clients_lock:
                                    for target in self.teacher_presentation_targets:
                                        for client_key, handler in self.server.clients.items():
                                            client_hostname = handler.client_info.get("hostname", "")
                                            if target in client_hostname or target == client_key:
                                                handler.send_command("STOP_PRESENTATION")
                                                self.server.log(f"   ▸ Presentation stopped for {client_key}")
                                                break
                                
                                delattr(self, 'teacher_presentation_targets')
                        
                        elif header.startswith("TEACHER_PRESENT_FRAME"):
                            # Teacher sending presentation frame to forward to clients
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
                                
                                # Forward frame to target clients
                                if hasattr(self, 'teacher_presentation_targets'):
                                    header_to_send = b"PRESENT_FRAME\n" + struct.pack(">Q", frame_size)
                                    
                                    with self.server.clients_lock:
                                        for target in self.teacher_presentation_targets:
                                            for client_key, handler in self.server.clients.items():
                                                client_hostname = handler.client_info.get("hostname", "")
                                                if target in client_hostname or target == client_key:
                                                    try:
                                                        with handler.lock:
                                                            handler.sock.sendall(header_to_send + frame_data)
                                                    except:
                                                        pass
                                                    break
                        
                        elif header.startswith("START_TEACHER_PRESENTATION"):
                            self.server.log(f"📽️ Teacher {self.key} started presenting")
                            
                        elif header.startswith("STOP_TEACHER_PRESENTATION"):
                            self.server.log(f"⏹️ Teacher {self.key} stopped presenting")
                            
                        elif header.startswith("PRESENT_FRAME"):
                            # Teacher sending presentation frame
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
                            client_key = header.split(":", 1)[1]
                            self.server.log(f"👨‍🏫 Teacher {self.key} monitoring {client_key}")
                            
                        elif header.startswith("STOP_MONITOR_CLIENT"):
                            self.server.log(f"👨‍🏫 Teacher {self.key} stopped monitoring")
                            
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
        
        self.restrictions = self.load_restrictions()
        
        # Teacher client support
        self.teacher_clients = {}
        self.teacher_lock = threading.Lock()

    def load_restrictions(self):
        try:
            if os.path.exists(RESTRICTIONS_FILE):
                with open(RESTRICTIONS_FILE, 'r') as f:
                    return json.load(f)
        except:
            pass
        return {"keywords": [], "sites": []}
    
    def save_restrictions(self, restrictions):
        try:
            with open(RESTRICTIONS_FILE, 'w') as f:
                json.dump(restrictions, f, indent=2)
            self.restrictions = restrictions
            self.log(f"💾 Restrictions saved: {len(restrictions['keywords'])} keywords, {len(restrictions['sites'])} sites")
            return True
        except Exception as e:
            self.log(f"❌ Failed to save restrictions: {e}")
            return False
    
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
            
            self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self.accept_thread.start()
            
            self.log(f"🚀 Server started on {self.host}:{self.port}")
            # ENHANCED: Show actual IP addresses
            hostname = socket.gethostname()
            try:
                local_ip = socket.gethostbyname(hostname)
            except:
                local_ip = "127.0.0.1"
            self.log(f"📡 Listening on IP: {local_ip}:{self.port}")
            self.log(f"💡 Clients should connect to: {local_ip}")
            self.log(f"📋 Make sure port {self.port} is not blocked by firewall")
            # ENHANCED: Show actual IP addresses
            hostname = socket.gethostname()
            try:
                local_ip = socket.gethostbyname(hostname)
            except:
                local_ip = "127.0.0.1"
            self.log(f"📡 Listening on IP: {local_ip}:{self.port}")
            self.log(f"💡 Clients should connect to: {local_ip}")
            self.log(f"📋 Make sure port {self.port} is not blocked by firewall")
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


class AdminWindow(QMainWindow):
    def __init__(self):
        super().__init__()
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
        self.selected_preview_client = None
        self.view_mode = "monitor"  # "monitor" or "clients"
        
        self._build_ui()
        self._start_timers()
    
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # NEW: Left panel (HandyCafe style)
        left_panel = self._create_left_panel()
        left_panel.setFixedWidth(280)  # FIXED: Set fixed width
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
        
        btn_send_file = QPushButton("📤 Send File")
        btn_send_file.clicked.connect(self.send_file_to_selected)
        file_layout.addWidget(btn_send_file)
        
        btn_backup = QPushButton("💾 Backup Files")
        btn_backup.clicked.connect(self.backup_client_files)
        file_layout.addWidget(btn_backup)
        
        btn_restore = QPushButton("📥 Restore Files")
        btn_restore.clicked.connect(self.restore_client_files)
        file_layout.addWidget(btn_restore)
        
        layout.addWidget(file_group)
        
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
        
        btn_restrictions = QPushButton("🚫 Manage Restrictions")
        btn_restrictions.clicked.connect(self.manage_restrictions)
        actions_layout.addWidget(btn_restrictions)
        
        self.restriction_indicator = QLabel("No restrictions")
        self.restriction_indicator.setStyleSheet("color: #888; font-size: 10px; padding: 5px;")
        self.restriction_indicator.setAlignment(Qt.AlignCenter)
        actions_layout.addWidget(self.restriction_indicator)
        
        layout.addWidget(actions_group)
        
        layout.addStretch()
        
        # View Tabs
        tabs_group = QGroupBox("View")
        tabs_layout = QHBoxLayout(tabs_group)
        
        self.btn_view_monitor = QPushButton("📺 Monitor")
        self.btn_view_monitor.clicked.connect(lambda: self.switch_view("monitor"))
        self.btn_view_monitor.setStyleSheet("background-color: #0078d4;")
        tabs_layout.addWidget(self.btn_view_monitor)
        
        self.btn_view_clients = QPushButton("👥 Clients")
        self.btn_view_clients.clicked.connect(lambda: self.switch_view("clients"))
        tabs_layout.addWidget(self.btn_view_clients)
        
        self.btn_view_logs = QPushButton("📋 Logs")
        self.btn_view_logs.clicked.connect(lambda: self.switch_view("logs"))
        tabs_layout.addWidget(self.btn_view_logs)
        
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
        """Create clients list view"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        title = QLabel("👥 Connected Clients")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        layout.addWidget(title)
        
        self.lst_clients = QListWidget()
        self.lst_clients.setSelectionMode(QListWidget.MultiSelection)
        self.lst_clients.itemSelectionChanged.connect(self._on_client_selection_changed)
        self.lst_clients.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lst_clients.customContextMenuRequested.connect(self._show_client_context_menu)
        
        # FIXED: Better styling for icon + text display
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
        
        layout.addWidget(self.lst_clients)
        
        btn_layout = QHBoxLayout()
        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.clicked.connect(self.refresh_clients)
        btn_layout.addWidget(btn_refresh)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        return widget
    
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
        """NEW: Switch between views"""
        self.view_mode = view_name
        
        # Update button styles
        self.btn_view_monitor.setStyleSheet("background-color: #2d2d2d;" if view_name != "monitor" else "background-color: #0078d4;")
        self.btn_view_clients.setStyleSheet("background-color: #2d2d2d;" if view_name != "clients" else "background-color: #0078d4;")
        self.btn_view_logs.setStyleSheet("background-color: #2d2d2d;" if view_name != "logs" else "background-color: #0078d4;")
        
        # Switch view
        if view_name == "monitor":
            self.stacked_views.setCurrentIndex(0)
        elif view_name == "clients":
            self.stacked_views.setCurrentIndex(1)
            self.refresh_clients()
        elif view_name == "logs":
            self.stacked_views.setCurrentIndex(2)
    
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
    
    def refresh_clients(self):
        keys = self.server.list_clients()
        selected = set([it.data(Qt.UserRole) for it in self.lst_clients.selectedItems() if it.data(Qt.UserRole)])
        
        current_keys = set([self.lst_clients.item(i).data(Qt.UserRole) 
                        for i in range(self.lst_clients.count())])
        
        if current_keys == set(keys):
            return
        
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
        
        if duplicate_hostnames:
            dup_list = "\n".join([f"  • {h} ({len(hostname_map[h])} clients)" for h in duplicate_hostnames])
            self.server.log(f"⚠️ WARNING: Duplicate PC names detected:\n{dup_list}")
        
        self.lst_clients.clear()
        
        # FIXED: Create PC icon once
        pc_icon = self._create_pc_icon()
        
        for k in keys:
            from PyQt5.QtWidgets import QListWidgetItem
            
            hostname = k.split(":")[0]
            with self.server.clients_lock:
                if k in self.server.clients:
                    handler = self.server.clients[k]
                    hostname = handler.client_info.get("hostname", hostname)
            
            is_duplicate = hostname in duplicate_hostnames
            
            # FIXED: Show as PC icon with name
            if is_duplicate:
                display_text = f"⚠️ {hostname}\nIP: {k.split(':')[0]}"
            else:
                display_text = f"{hostname}\nIP: {k.split(':')[0]}"
            
            # Create item with icon
            item = QListWidgetItem(pc_icon, display_text)
            item.setData(Qt.UserRole, k)
            
            # FIXED: Better item styling
            item.setFont(QFont("Segoe UI", 11))
            item.setSizeHint(QSize(200, 60))  # Set minimum size for better visibility
            
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
                    f"Keywords blocked: {len(new_restrictions['keywords'])}\n"
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
        
        menu.exec_(self.lst_clients.mapToGlobal(position))
    
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
    
    def backup_client_files(self):
        keys = self._get_selected_keys()
        if not keys:
            QMessageBox.warning(self, "No Selection", "Select one or more clients to backup")
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
            f"Files will be organized by client PC name.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.server.custom_backup_dir = final_backup_dir
            
            try:
                success_count = self.server.request_backup_from_clients(keys, source_path, move_files)
                
                mode_warning = "\n\n⚠️ Files will be DELETED from clients after backup!" if move_files else ""
                
                QMessageBox.information(
                    self, "Backup Started",
                    f"Backup request sent to {success_count} client(s)\n\n"
                    f"Mode: {mode_text}\n"
                    f"Files will be saved to:\n{final_backup_dir}\n\n"
                    f"Check the log for progress updates.{mode_warning}"
                )
            finally:
                self.server.custom_backup_dir = None
    
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
        try:
            qimg = QImage.fromData(QByteArray(image_data))
            if not qimg.isNull():
                pixmap = QPixmap.fromImage(qimg)
                scaled = pixmap.scaled(
                    self.lbl_preview.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.lbl_preview.setPixmap(scaled)
        except Exception as e:
            self.server.log(f"Display error: {e}")
    
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