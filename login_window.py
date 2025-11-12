from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QLineEdit, 
    QPushButton, QMessageBox, QComboBox, QSpacerItem, QSizePolicy, QFrame
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
import socket


class LoginDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.user_data = None
        self.user_type = None
        
        # Window setup
        self.setWindowTitle("ACCLABS - Login")
        self.resize(390, 425)
        self.setMinimumSize(380, 400)
        self.setStyleSheet("""
            QDialog {
                background-color: #121212;
                border-radius: 10px;
            }
            QLabel {
                color: #e0e0e0;
                font-family: 'Segoe UI';
            }
            QLineEdit {
                background-color: #1f1f1f;
                color: #ffffff;
                border: 2px solid #3daee9;
                border-radius: 6px;
                padding: 8px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border-color: #5fd4ff;
                background-color: #252525;
            }
            QPushButton {
                background-color: #3daee9;
                color: white;
                border: none;
                padding: 10px;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #5fd4ff;
            }
            QPushButton:pressed {
                background-color: #2980b9;
            }
            QComboBox {
                background-color: #1f1f1f;
                color: #ffffff;
                border: 2px solid #3daee9;
                border-radius: 6px;
                padding: 6px;
                font-size: 13px;
            }
            QComboBox:hover {
                border-color: #5fd4ff;
            }
        """)
        
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(40, 25, 40, 25)
        
        # Header
        header = QLabel("💻 ACCLABS")
        header.setFont(QFont("Segoe UI Semibold", 20))
        header.setStyleSheet("color: #3daee9;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)

        subtitle = QLabel("Sign in to continue")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: #aaaaaa; font-size: 11.5px;")
        layout.addWidget(subtitle)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet("color: #3daee9; margin-top: 8px; margin-bottom: 8px;")
        layout.addWidget(divider)

        # Spacer for balance
        layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        
        # Login as
        layout.addWidget(QLabel("Login as:"))
        self.combo_type = QComboBox()
        self.combo_type.addItems(["Admin", "Teacher"])
        layout.addWidget(self.combo_type)
        
        # Username field
        layout.addWidget(QLabel("Username:"))
        self.txt_username = QLineEdit()
        self.txt_username.setPlaceholderText("Enter username")
        layout.addWidget(self.txt_username)
        
        # Password field
        layout.addWidget(QLabel("Password:"))
        self.txt_password = QLineEdit()
        self.txt_password.setPlaceholderText("Enter password")
        self.txt_password.setEchoMode(QLineEdit.Password)
        self.txt_password.returnPressed.connect(self.login)
        layout.addWidget(self.txt_password)

        # Spacer before button
        layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # Login button
        self.btn_login = QPushButton("🔐 Login")
        self.btn_login.clicked.connect(self.login)
        layout.addWidget(self.btn_login, alignment=Qt.AlignCenter)

        layout.addStretch(1)

        # Footer
        footer = QLabel("© 2025 ACCLABS System")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("color: #555; font-size: 10px; margin-top: 8px;")
        layout.addWidget(footer)

    def login(self):
        username = self.txt_username.text().strip()
        password = self.txt_password.text()
        user_type = self.combo_type.currentText().lower()
        
        if not username or not password:
            QMessageBox.warning(self, "Input Required", "Please enter both username and password.")
            return
        
        self.btn_login.setEnabled(False)
        self.btn_login.setText("Authenticating... ⏳")
        
        try:
            hostname = socket.gethostname()
            ip_address = socket.gethostbyname(hostname)
            
            if user_type == "admin":
                result = self.db.authenticate_admin(username, password, ip_address)
            else:
                result = self.db.authenticate_teacher(username, password, ip_address)
            
            if result:
                self.user_data = result
                self.user_type = user_type
                self.accept()
            else:
                QMessageBox.critical(self, "Login Failed", "Invalid username or password.")
                self.btn_login.setEnabled(True)
                self.btn_login.setText("🔐 Login")
        
        except Exception as e:
            QMessageBox.critical(self, "Login Error", f"Authentication error: {e}")
            self.btn_login.setEnabled(True)
            self.btn_login.setText("🔐 Login")
