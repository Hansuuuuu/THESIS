"""
File Transfer Database Manager - MySQL/XAMPP Version
For Lab Management System

This module provides MySQL database integration for tracking file transfers,
chunks, backups, and statistics using XAMPP's MySQL server.

Requirements:
    pip install mysql-connector-python

Usage:
    from file_transfer_db_mysql import FileTransferDB
    
    # Initialize with XAMPP MySQL connection
    db = FileTransferDB(
        host='localhost',
        port=3306,
        user='root',
        password='',  # XAMPP default
        database='lab_manager'
    )
    
    # Create a transfer
    db.create_transfer(
        transfer_id="abc123",
        filename="document.pdf",
        file_path="/path/to/document.pdf",
        file_size=1024000,
        source_type="admin",
        source_id="ADMIN-PC",
        dest_type="client",
        dest_id="PC-01",
        total_chunks=10
    )
    
    # Update progress
    db.update_transfer_progress("abc123", chunks_completed=5, bytes_transferred=512000)
    
    # Mark complete
    db.complete_transfer("abc123", success=True)
"""

import mysql.connector
from mysql.connector import Error, pooling
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import hashlib
import json


class FileTransferDB:
    """MySQL Database manager for file transfer operations"""
    
    def __init__(self, host: str = 'localhost', port: int = 3306,
                 user: str = 'root', password: str = '', database: str = 'lab_manager',
                 pool_size: int = 5):
        """
        Initialize MySQL connection with connection pooling
        
        Args:
            host: MySQL server host (default: localhost for XAMPP)
            port: MySQL server port (default: 3306)
            user: MySQL username (default: root for XAMPP)
            password: MySQL password (default: empty for XAMPP)
            database: Database name
            pool_size: Connection pool size
        """
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        
        # Create connection pool for better performance
        try:
            self.connection_pool = pooling.MySQLConnectionPool(
                pool_name="lab_manager_pool",
                pool_size=pool_size,
                pool_reset_session=True,
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                autocommit=False
            )
            print(f"✅ Connected to MySQL database '{database}' at {host}:{port}")
        except Error as e:
            print(f"❌ Error connecting to MySQL: {e}")
            raise
    
    def get_connection(self) -> mysql.connector.MySQLConnection:
        """Get connection from pool"""
        try:
            return self.connection_pool.get_connection()
        except Error as e:
            print(f"Error getting connection from pool: {e}")
            raise
    
    def test_connection(self) -> bool:
        """Test database connection"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
            return True
        except Error as e:
            print(f"Connection test failed: {e}")
            return False
    
    # ==================== FILE TRANSFER OPERATIONS ====================
    
    def create_transfer(self, transfer_id: str, filename: str, file_path: str, 
                       file_size: int, source_type: str, source_id: str,
                       dest_type: str, dest_id: str, total_chunks: int,
                       transfer_type: str = "file_send", file_hash: str = None) -> bool:
        """
        Create a new file transfer record
        
        Args:
            transfer_id: Unique transfer identifier
            filename: Name of the file
            file_path: Full path to the file
            file_size: Size in bytes
            source_type: Type of source ('admin', 'teacher', 'client')
            source_id: Source identifier (hostname/key)
            dest_type: Type of destination
            dest_id: Destination identifier
            total_chunks: Total number of chunks
            transfer_type: Type of transfer operation
            file_hash: Optional MD5 hash of file
        
        Returns:
            True if successful, False otherwise
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                INSERT INTO file_transfers (
                    transfer_id, filename, file_path, file_size, file_hash,
                    source_type, source_id, destination_type, destination_id,
                    status, chunks_total, initiated_at, transfer_type
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, NOW(), %s)
            """
            
            values = (transfer_id, filename, file_path, file_size, file_hash,
                     source_type, source_id, dest_type, dest_id,
                     total_chunks, transfer_type)
            
            cursor.execute(query, values)
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error creating transfer: {e}")
            return False
    
    def update_transfer_progress(self, transfer_id: str, chunks_completed: int,
                                bytes_transferred: int, status: str = 'in_progress') -> bool:
        """
        Update transfer progress
        
        Args:
            transfer_id: Transfer identifier
            chunks_completed: Number of chunks completed
            bytes_transferred: Total bytes transferred so far
            status: Current status
        
        Returns:
            True if successful
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Get total chunks to calculate percentage
            cursor.execute(
                "SELECT chunks_total FROM file_transfers WHERE transfer_id = %s", 
                (transfer_id,)
            )
            row = cursor.fetchone()
            
            if not row:
                cursor.close()
                conn.close()
                return False
            
            total_chunks = row[0]
            progress = (chunks_completed / total_chunks * 100.0) if total_chunks > 0 else 0.0
            
            query = """
                UPDATE file_transfers 
                SET chunks_completed = %s,
                    total_bytes_transferred = %s,
                    progress_percent = %s,
                    status = %s,
                    started_at = COALESCE(started_at, NOW())
                WHERE transfer_id = %s
            """
            
            cursor.execute(query, (chunks_completed, bytes_transferred, progress,
                                  status, transfer_id))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error updating transfer progress: {e}")
            return False
    
    def complete_transfer(self, transfer_id: str, success: bool = True, 
                         error_msg: str = None, transfer_speed: float = None) -> bool:
        """
        Mark transfer as completed or failed
        
        Args:
            transfer_id: Transfer identifier
            success: Whether transfer was successful
            error_msg: Error message if failed
            transfer_speed: Transfer speed in Mbps
        
        Returns:
            True if successful
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            status = 'completed' if success else 'failed'
            progress = 100.0 if success else None
            
            query = """
                UPDATE file_transfers 
                SET status = %s,
                    completed_at = NOW(),
                    progress_percent = %s,
                    error_message = %s,
                    transfer_speed_mbps = %s
                WHERE transfer_id = %s
            """
            
            cursor.execute(query, (status, progress, error_msg, transfer_speed, transfer_id))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error completing transfer: {e}")
            return False
    
    def get_transfer_status(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        """Get current transfer status"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.execute(
                "SELECT * FROM file_transfers WHERE transfer_id = %s", 
                (transfer_id,)
            )
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            return row
            
        except Error as e:
            print(f"Error getting transfer status: {e}")
            return None
    
    def get_incomplete_transfers(self, client_key: str = None) -> List[Dict[str, Any]]:
        """
        Get list of incomplete transfers for resumption
        
        Args:
            client_key: Optional client key to filter by
        
        Returns:
            List of incomplete transfer records
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            if client_key:
                query = """
                    SELECT * FROM file_transfers 
                    WHERE status IN ('pending', 'in_progress')
                      AND (source_id = %s OR destination_id = %s)
                    ORDER BY initiated_at DESC
                """
                cursor.execute(query, (client_key, client_key))
            else:
                query = """
                    SELECT * FROM file_transfers 
                    WHERE status IN ('pending', 'in_progress')
                    ORDER BY initiated_at DESC
                """
                cursor.execute(query)
            
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting incomplete transfers: {e}")
            return []
    
    def retry_transfer(self, transfer_id: str) -> bool:
        """Increment retry count and reset status to pending"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                UPDATE file_transfers 
                SET status = 'pending',
                    retry_count = retry_count + 1,
                    error_message = NULL
                WHERE transfer_id = %s
            """
            
            cursor.execute(query, (transfer_id,))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error retrying transfer: {e}")
            return False
    
    # ==================== CHUNK OPERATIONS ====================
    
    def save_chunk_status(self, transfer_id: str, chunk_index: int, 
                         chunk_size: int, chunk_hash: str = None,
                         status: str = 'received') -> bool:
        """
        Record chunk receipt
        
        Args:
            transfer_id: Transfer identifier
            chunk_index: Index of the chunk
            chunk_size: Size of chunk in bytes
            chunk_hash: MD5 hash of chunk data
            status: Status ('sent', 'received', 'verified')
        
        Returns:
            True if successful
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Use INSERT ... ON DUPLICATE KEY UPDATE for MySQL
            query = """
                INSERT INTO transfer_chunks (
                    transfer_id, chunk_index, chunk_size, chunk_hash,
                    status, received_at
                ) VALUES (%s, %s, %s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    received_at = NOW()
            """
            
            cursor.execute(query, (transfer_id, chunk_index, chunk_size, 
                                  chunk_hash, status))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error saving chunk status: {e}")
            return False
    
    def get_received_chunks(self, transfer_id: str) -> List[int]:
        """
        Get list of already received chunks
        
        Args:
            transfer_id: Transfer identifier
        
        Returns:
            List of chunk indices
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                SELECT chunk_index FROM transfer_chunks 
                WHERE transfer_id = %s AND status = 'received'
                ORDER BY chunk_index
            """
            
            cursor.execute(query, (transfer_id,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return [row[0] for row in rows]
            
        except Error as e:
            print(f"Error getting received chunks: {e}")
            return []
    
    def get_missing_chunks(self, transfer_id: str) -> List[int]:
        """Get list of chunks that haven't been received yet"""
        try:
            # Get total chunks
            status = self.get_transfer_status(transfer_id)
            if not status:
                return []
            
            total_chunks = status['chunks_total']
            received = self.get_received_chunks(transfer_id)
            
            # Find missing chunks
            all_chunks = set(range(total_chunks))
            received_set = set(received)
            missing = sorted(list(all_chunks - received_set))
            
            return missing
            
        except Exception as e:
            print(f"Error getting missing chunks: {e}")
            return []
    
    # ==================== CLIENT SESSION TRACKING ====================
    
    def create_session(self, session_id: str, client_key: str, hostname: str,
                      client_type: str, ip_address: str, **kwargs) -> bool:
        """Create a new client session record"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                INSERT INTO client_sessions (
                    session_id, client_key, hostname, client_type, ip_address,
                    os_info, python_version, client_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            values = (session_id, client_key, hostname, client_type, ip_address,
                     kwargs.get('os_info'), kwargs.get('python_version'),
                     kwargs.get('client_version'))
            
            cursor.execute(query, values)
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error creating session: {e}")
            return False
    
    def update_session_activity(self, session_id: str) -> bool:
        """Update last activity timestamp for a session"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                UPDATE client_sessions 
                SET last_activity = NOW()
                WHERE session_id = %s
            """
            
            cursor.execute(query, (session_id,))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error updating session activity: {e}")
            return False
    
    def end_session(self, session_id: str) -> bool:
        """Mark session as disconnected"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                UPDATE client_sessions 
                SET disconnected_at = NOW()
                WHERE session_id = %s
            """
            
            cursor.execute(query, (session_id,))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error ending session: {e}")
            return False
    
    # ==================== BACKUP OPERATIONS ====================
    
    def create_backup(self, backup_id: str, operation_type: str, client_key: str,
                     client_hostname: str, backup_path: str, **kwargs) -> bool:
        """Create a backup operation record"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            query = """
                INSERT INTO backup_operations (
                    backup_id, operation_type, client_key, client_hostname,
                    backup_name, backup_path, backup_size, file_count,
                    template_name, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_progress')
            """
            
            values = (backup_id, operation_type, client_key, client_hostname,
                     kwargs.get('backup_name'), backup_path, kwargs.get('backup_size'),
                     kwargs.get('file_count'), kwargs.get('template_name'))
            
            cursor.execute(query, values)
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error creating backup: {e}")
            return False
    
    def complete_backup(self, backup_id: str, success: bool = True, 
                       error_msg: str = None) -> bool:
        """Mark backup operation as completed"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            status = 'completed' if success else 'failed'
            
            query = """
                UPDATE backup_operations 
                SET status = %s,
                    completed_at = NOW(),
                    error_message = %s
                WHERE backup_id = %s
            """
            
            cursor.execute(query, (status, error_msg, backup_id))
            conn.commit()
            cursor.close()
            conn.close()
            return True
            
        except Error as e:
            print(f"Error completing backup: {e}")
            return False
    
    def get_client_backups(self, client_key: str) -> List[Dict[str, Any]]:
        """Get all backups for a specific client"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT * FROM backup_operations 
                WHERE client_key = %s
                ORDER BY started_at DESC
            """
            
            cursor.execute(query, (client_key,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting client backups: {e}")
            return []
    
    # ==================== STATISTICS & REPORTING ====================
    
    def get_transfer_history(self, days: int = 7, client_key: str = None) -> List[Dict[str, Any]]:
        """
        Get transfer history for reporting
        
        Args:
            days: Number of days to look back
            client_key: Optional client filter
        
        Returns:
            List of daily statistics
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            if client_key:
                query = """
                    SELECT 
                        DATE(initiated_at) as date,
                        COUNT(*) as total_transfers,
                        SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                        SUM(total_bytes_transferred) as total_bytes,
                        AVG(transfer_speed_mbps) as avg_speed,
                        AVG(duration_seconds) as avg_duration
                    FROM file_transfers
                    WHERE initiated_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                      AND (source_id = %s OR destination_id = %s)
                    GROUP BY DATE(initiated_at)
                    ORDER BY date DESC
                """
                cursor.execute(query, (days, client_key, client_key))
            else:
                query = """
                    SELECT 
                        DATE(initiated_at) as date,
                        COUNT(*) as total_transfers,
                        SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                        SUM(total_bytes_transferred) as total_bytes,
                        AVG(transfer_speed_mbps) as avg_speed,
                        AVG(duration_seconds) as avg_duration
                    FROM file_transfers
                    WHERE initiated_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                    GROUP BY DATE(initiated_at)
                    ORDER BY date DESC
                """
                cursor.execute(query, (days,))
            
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting transfer history: {e}")
            return []
    
    def get_top_transferred_files(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get most frequently transferred files"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT 
                    filename,
                    COUNT(*) as transfer_count,
                    SUM(file_size) as total_size,
                    AVG(transfer_speed_mbps) as avg_speed,
                    MAX(initiated_at) as last_transfer
                FROM file_transfers
                WHERE status = 'completed'
                GROUP BY filename
                ORDER BY transfer_count DESC
                LIMIT %s
            """
            
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting top files: {e}")
            return []
    
    def get_client_transfer_stats(self, client_key: str) -> Optional[Dict[str, Any]]:
        """Get transfer statistics for specific client"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT 
                    COUNT(*) as total_transfers,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                    SUM(total_bytes_transferred) as total_bytes,
                    AVG(transfer_speed_mbps) as avg_speed,
                    MAX(initiated_at) as last_transfer
                FROM file_transfers
                WHERE source_id = %s OR destination_id = %s
            """
            
            cursor.execute(query, (client_key, client_key))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            return row
            
        except Error as e:
            print(f"Error getting client stats: {e}")
            return None
    
    def get_success_rate(self, days: int = 30) -> Dict[str, Any]:
        """Calculate overall success rate"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status IN ('pending', 'in_progress') THEN 1 ELSE 0 END) as in_progress
                FROM file_transfers
                WHERE initiated_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """
            
            cursor.execute(query, (days,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if row and row['total'] > 0:
                return {
                    'total': row['total'],
                    'successful': row['successful'],
                    'failed': row['failed'],
                    'in_progress': row['in_progress'],
                    'success_rate': (row['successful'] / row['total'] * 100.0)
                }
            else:
                return {
                    'total': 0,
                    'successful': 0,
                    'failed': 0,
                    'in_progress': 0,
                    'success_rate': 0.0
                }
                
        except Error as e:
            print(f"Error calculating success rate: {e}")
            return {}
    
    def get_active_transfers(self) -> List[Dict[str, Any]]:
        """Get all currently active transfers"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT * FROM file_transfers 
                WHERE status = 'in_progress'
                ORDER BY started_at DESC
            """
            
            cursor.execute(query)
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting active transfers: {e}")
            return []
    
    # ==================== VIEWS AND PROCEDURES ====================
    
    def get_daily_statistics(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get data from daily statistics view"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = "SELECT * FROM v_daily_statistics LIMIT %s"
            cursor.execute(query, (days,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting daily statistics: {e}")
            return []
    
    def get_client_performance(self) -> List[Dict[str, Any]]:
        """Get data from client performance view"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = "SELECT * FROM v_client_performance"
            cursor.execute(query)
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            return rows
            
        except Error as e:
            print(f"Error getting client performance: {e}")
            return []
    
    def call_get_transfer_summary(self, days: int = 30) -> Optional[Dict[str, Any]]:
        """Call stored procedure to get transfer summary"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.callproc('sp_get_transfer_summary', [days])
            
            # Fetch results
            for result in cursor.stored_results():
                row = result.fetchone()
                cursor.close()
                conn.close()
                return row
            
            cursor.close()
            conn.close()
            return None
            
        except Error as e:
            print(f"Error calling transfer summary procedure: {e}")
            return None
    
    def call_cleanup_old_records(self, days: int = 90) -> int:
        """Call stored procedure to cleanup old records"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.callproc('sp_cleanup_old_records', [days])
            
            # Fetch result (deleted count)
            for result in cursor.stored_results():
                row = result.fetchone()
                deleted = row['deleted_records'] if row else 0
                cursor.close()
                conn.close()
                return deleted
            
            cursor.close()
            conn.close()
            return 0
            
        except Error as e:
            print(f"Error calling cleanup procedure: {e}")
            return 0
    
    # ==================== UTILITY METHODS ====================
    
    def export_to_json(self, output_file: str, days: int = 30) -> bool:
        """Export recent transfers to JSON file"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = """
                SELECT * FROM file_transfers
                WHERE initiated_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY initiated_at DESC
            """
            
            cursor.execute(query, (days,))
            rows = cursor.fetchall()
            cursor.close()
            conn.close()
            
            # Convert datetime to string for JSON serialization
            for row in rows:
                for key, value in row.items():
                    if isinstance(value, datetime):
                        row[key] = value.isoformat()
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(rows, f, indent=2, default=str)
            
            return True
            
        except Exception as e:
            print(f"Error exporting to JSON: {e}")
            return False
    
    def close_all_connections(self):
        """Close all connections in the pool"""
        try:
            # MySQL connector handles pool closure automatically
            print("✅ Connection pool closed")
        except Exception as e:
            print(f"Error closing connections: {e}")
    
    # ==================== AUTHENTICATION METHODS ====================
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using SHA-256"""
        return hashlib.sha256(password.encode()).hexdigest()
    
    def authenticate_user(self, username: str, password: str, ip_address: str = None) -> Dict[str, Any]:
        """
        Authenticate user (admin or teacher)
        
        Returns:
            dict with keys: success, user_id, user_type, full_name, message
        """
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            password_hash = self.hash_password(password)
            if not ip_address:
                import socket
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    ip_address = s.getsockname()[0]
                    s.close()
                except:
                    ip_address = "127.0.0.1"
            
            # Use stored procedure
            args = [username, password_hash, ip_address, 0, '', '', 0]
            result = cursor.callproc('sp_authenticate_user', args)
            
            user_id = result[3]
            user_type = result[4]
            full_name = result[5]
            success = result[6]
            
            cursor.close()
            conn.close()
            
            if success:
                # Create session
                session_id = self._create_session(user_id, user_type, username, ip_address)
                return {
                    'success': True,
                    'user_id': user_id,
                    'user_type': user_type,
                    'full_name': full_name,
                    'session_id': session_id,
                    'message': f"Welcome, {full_name}!"
                }
            else:
                return {
                    'success': False,
                    'user_id': None,
                    'user_type': None,
                    'full_name': None,
                    'session_id': None,
                    'message': "Invalid username or password"
                }
        except Exception as e:
            print(f"❌ Authentication error: {e}")
            if conn:
                conn.close()
            return {
                'success': False,
                'user_id': None,
                'user_type': None,
                'full_name': None,
                'session_id': None,
                'message': f"Authentication error: {str(e)}"
            }
    
    def authenticate_admin(self, username: str, password: str, ip_address: str = None) -> dict:
        """
        Authenticate admin user - Used by LoginDialog
        Returns dict with user info if successful, None if failed
        """
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            # Get admin record
            query = """
                SELECT admin_id, username, password_hash, full_name, email, is_active
                FROM admin
                WHERE username = %s AND is_active = 1
            """
            cursor.execute(query, (username,))
            admin = cursor.fetchone()
            
            if not admin:
                cursor.close()
                conn.close()
                return None
            
            # Verify password (SHA-256)
            password_hash = self.hash_password(password)
            if password_hash != admin['password_hash']:
                cursor.close()
                conn.close()
                return None
            
            # Update last login
            cursor.execute(
                "UPDATE admin SET last_login = NOW(), ip_address = %s WHERE admin_id = %s",
                (ip_address or '127.0.0.1', admin['admin_id'])
            )
            conn.commit()
            
            # Create session
            import uuid
            session_id = str(uuid.uuid4())
            cursor.execute("""
                INSERT INTO login_sessions 
                (session_id, user_type, user_id, username, ip_address, login_time, is_active)
                VALUES (%s, 'admin', %s, %s, %s, NOW(), 1)
            """, (session_id, admin['admin_id'], username, ip_address or '127.0.0.1'))
            conn.commit()
            
            cursor.close()
            conn.close()
            
            # Return user data in format expected by LoginDialog
            return {
                'admin_id': admin['admin_id'],
                'user_id': admin['admin_id'],
                'username': admin['username'],
                'full_name': admin['full_name'],
                'email': admin['email'],
                'session_id': session_id
            }
            
        except Exception as e:
            print(f"❌ Admin authentication error: {e}")
            if conn:
                conn.close()
            return None
    
    def authenticate_teacher(self, username: str, password: str, ip_address: str = None) -> dict:
        """
        Authenticate teacher user - Used by LoginDialog
        Returns dict with user info if successful, None if failed
        """
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            
            # Get teacher record
            query = """
                SELECT teacher_id, username, password_hash, full_name, email, is_active
                FROM teacher
                WHERE username = %s AND is_active = 1
            """
            cursor.execute(query, (username,))
            teacher = cursor.fetchone()
            
            if not teacher:
                cursor.close()
                conn.close()
                return None
            
            # Verify password (SHA-256)
            password_hash = self.hash_password(password)
            if password_hash != teacher['password_hash']:
                cursor.close()
                conn.close()
                return None
            
            # Update last login
            cursor.execute(
                "UPDATE teacher SET last_login = NOW(), ip_address = %s WHERE teacher_id = %s",
                (ip_address or '127.0.0.1', teacher['teacher_id'])
            )
            conn.commit()
            
            # Create session
            import uuid
            session_id = str(uuid.uuid4())
            cursor.execute("""
                INSERT INTO login_sessions 
                (session_id, user_type, user_id, username, ip_address, login_time, is_active)
                VALUES (%s, 'teacher', %s, %s, %s, NOW(), 1)
            """, (session_id, teacher['teacher_id'], username, ip_address or '127.0.0.1'))
            conn.commit()
            
            cursor.close()
            conn.close()
            
            # Return user data in format expected by LoginDialog
            return {
                'teacher_id': teacher['teacher_id'],
                'user_id': teacher['teacher_id'],
                'username': teacher['username'],
                'full_name': teacher['full_name'],
                'email': teacher['email'],
                'session_id': session_id
            }
            
        except Exception as e:
            print(f"❌ Teacher authentication error: {e}")
            if conn:
                conn.close()
            return None
    
    def _create_session(self, user_id: int, user_type: str, username: str, ip_address: str) -> str:
        """Create login session"""
        import uuid
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            session_id = str(uuid.uuid4())
            query = """
                INSERT INTO login_sessions 
                (session_id, user_type, user_id, username, ip_address, login_time, is_active)
                VALUES (%s, %s, %s, %s, %s, NOW(), 1)
            """
            cursor.execute(query, (session_id, user_type, user_id, username, ip_address))
            conn.commit()
            cursor.close()
            conn.close()
            return session_id
        except Exception as e:
            print(f"❌ Session creation error: {e}")
            if conn:
                conn.close()
            return None
    
    def create_teacher(self, admin_id: int, username: str, password: str, 
                      full_name: str, email: str = None) -> Dict[str, Any]:
        """Create new teacher account (admin only)"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Check if username exists
            cursor.execute("SELECT COUNT(*) FROM teacher WHERE username = %s", (username,))
            if cursor.fetchone()[0] > 0:
                cursor.close()
                conn.close()
                return {'success': False, 'message': 'Username already exists'}
            
            cursor.execute("SELECT COUNT(*) FROM admin WHERE username = %s", (username,))
            if cursor.fetchone()[0] > 0:
                cursor.close()
                conn.close()
                return {'success': False, 'message': 'Username already exists in admin table'}
            
            # Create teacher
            password_hash = self.hash_password(password)
            query = """
                INSERT INTO teacher 
                (username, password_hash, full_name, email, created_by_admin_id, is_active)
                VALUES (%s, %s, %s, %s, %s, 1)
            """
            cursor.execute(query, (username, password_hash, full_name, email, admin_id))
            conn.commit()
            
            teacher_id = cursor.lastrowid
            cursor.close()
            conn.close()
            
            return {
                'success': True,
                'teacher_id': teacher_id,
                'message': f"Teacher account '{username}' created successfully"
            }
        except Exception as e:
            if conn:
                conn.close()
            return {'success': False, 'message': f"Error: {str(e)}"}
    
    def get_all_teachers(self) -> List[Dict[str, Any]]:
        """Get list of all teachers"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            query = """
                SELECT teacher_id, username, full_name, email, last_login, 
                       created_at, is_active
                FROM teacher
                ORDER BY created_at DESC
            """
            cursor.execute(query)
            teachers = cursor.fetchall()
            cursor.close()
            conn.close()
            return teachers
        except Exception as e:
            print(f"❌ Error fetching teachers: {e}")
            if conn:
                conn.close()
            return []
    
    def update_teacher_status(self, teacher_id: int, is_active: bool) -> bool:
        """Enable or disable teacher account"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            query = "UPDATE teacher SET is_active = %s WHERE teacher_id = %s"
            cursor.execute(query, (is_active, teacher_id))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            if conn:
                conn.close()
            return False
    
    def delete_teacher(self, teacher_id: int) -> bool:
        """Delete teacher account"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            query = "DELETE FROM teacher WHERE teacher_id = %s"
            cursor.execute(query, (teacher_id,))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            if conn:
                conn.close()
            return False
    
    # ==================== CLIENT MANAGEMENT METHODS ====================
    
    def add_or_update_client(self, admin_ip: str, admin_username: str, admin_type: str,
                            client_ip: str, client_name: str, client_hostname: str = None,
                            os_info: str = None, python_version: str = None, 
                            client_version: str = None) -> bool:
        """Add or update client connection"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Check if exists
            cursor.execute("SELECT id FROM admin_clients WHERE admin_ip = %s AND client_ip = %s",
                          (admin_ip, client_ip))
            existing = cursor.fetchone()
            
            if existing:
                # Update
                query = """
                    UPDATE admin_clients 
                    SET client_name = %s, client_hostname = %s, connection_status = 'connected',
                        last_seen = NOW(), last_heartbeat = NOW(), os_info = %s,
                        python_version = %s, client_version = %s, disconnected_at = NULL
                    WHERE admin_ip = %s AND client_ip = %s
                """
                cursor.execute(query, (client_name, client_hostname, os_info, python_version,
                                      client_version, admin_ip, client_ip))
            else:
                # Insert
                query = """
                    INSERT INTO admin_clients 
                    (admin_ip, admin_username, admin_type, client_ip, client_name, 
                     client_hostname, connection_status, os_info, python_version, 
                     client_version, last_heartbeat)
                    VALUES (%s, %s, %s, %s, %s, %s, 'connected', %s, %s, %s, NOW())
                """
                cursor.execute(query, (admin_ip, admin_username, admin_type, client_ip,
                                      client_name, client_hostname, os_info, python_version,
                                      client_version))
            
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            print(f"❌ Error adding/updating client: {e}")
            if conn:
                conn.close()
            return False
    
    def update_client_heartbeat(self, admin_ip: str, client_ip: str, client_name: str,
                               hostname: str = None, os_info: str = None, 
                               cpu_usage: float = None, memory_usage: float = None) -> bool:
        """Update client heartbeat"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            args = [admin_ip, client_ip, client_name, hostname, os_info, cpu_usage, memory_usage]
            cursor.callproc('sp_update_client_heartbeat', args)
            
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            if conn:
                conn.close()
            return False
    
    def mark_client_disconnected(self, admin_ip: str, client_ip: str) -> bool:
        """Mark client as disconnected"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            query = """
                UPDATE admin_clients 
                SET connection_status = 'disconnected', disconnected_at = NOW()
                WHERE admin_ip = %s AND client_ip = %s
            """
            cursor.execute(query, (admin_ip, client_ip))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            if conn:
                conn.close()
            return False
    
    def remove_client(self, admin_ip: str, client_ip: str) -> bool:
        """Remove client from list (admin only)"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            query = "DELETE FROM admin_clients WHERE admin_ip = %s AND client_ip = %s"
            cursor.execute(query, (admin_ip, client_ip))
            conn.commit()
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            if conn:
                conn.close()
            return False
    
    def get_admin_clients(self, admin_ip: str) -> List[Dict[str, Any]]:
        """Get all clients for admin"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            query = """
                SELECT * FROM v_active_admin_clients
                WHERE admin_ip = %s
                ORDER BY connection_status DESC, client_name
            """
            cursor.execute(query, (admin_ip,))
            clients = cursor.fetchall()
            cursor.close()
            conn.close()
            return clients
        except Exception as e:
            if conn:
                conn.close()
            return []


# ==================== EXAMPLE USAGE ====================

if __name__ == "__main__":
    print("="*60)
    print("MySQL File Transfer Database - Example Usage")
    print("="*60)
    
    # Initialize database with XAMPP defaults
    try:
        db = FileTransferDB(
            host='localhost',
            port=3306,
            user='root',
            password='',  # XAMPP default (no password)
            database='lab_manager'
        )
        
        print("\n✅ Database connected successfully!")
        
        # Test connection
        if db.test_connection():
            print("✅ Connection test passed")
        
        print("\n" + "="*60)
        print("RUNNING EXAMPLE OPERATIONS")
        print("="*60)
        
        # Example 1: Create a transfer
        print("\n1. Creating test transfer...")
        transfer_id = "test_mysql_001"
        success = db.create_transfer(
            transfer_id=transfer_id,
            filename="test_document.pdf",
            file_path="/home/admin/test_document.pdf",
            file_size=1024000,
            source_type="admin",
            source_id="ADMIN-PC",
            dest_type="client",
            dest_id="PC-01",
            total_chunks=10,
            transfer_type="file_send"
        )
        print(f"   Transfer created: {success}")
        
        # Example 2: Update progress
        print("\n2. Updating transfer progress...")
        db.update_transfer_progress(transfer_id, chunks_completed=5, bytes_transferred=512000)
        status = db.get_transfer_status(transfer_id)
        if status:
            print(f"   Progress: {status['progress_percent']:.1f}% ({status['chunks_completed']}/{status['chunks_total']} chunks)")
        
        # Example 3: Save chunk status
        print("\n3. Recording chunk receipts...")
        for i in range(5):
            db.save_chunk_status(transfer_id, chunk_index=i, chunk_size=102400)
        received = db.get_received_chunks(transfer_id)
        print(f"   Received chunks: {received}")
        
        # Example 4: Complete transfer
        print("\n4. Completing transfer...")
        db.complete_transfer(transfer_id, success=True, transfer_speed=45.5)
        final_status = db.get_transfer_status(transfer_id)
        if final_status:
            print(f"   Status: {final_status['status']}")
            print(f"   Speed: {final_status['transfer_speed_mbps']} Mbps")
        
        # Example 5: Get statistics
        print("\n5. Getting statistics...")
        
        # Use view
        daily_stats = db.get_daily_statistics(days=7)
        print(f"   Daily statistics (last 7 days): {len(daily_stats)} records")
        
        # Use stored procedure
        summary = db.call_get_transfer_summary(days=30)
        if summary:
            print(f"   Success rate (30d): {summary['success_rate']:.1f}%")
            print(f"   Total transfers: {summary['total_transfers']}")
        
        # Example 6: Clean up test data
        print("\n6. Cleaning up test data...")
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM file_transfers WHERE transfer_id = %s", (transfer_id,))
        conn.commit()
        cursor.close()
        conn.close()
        print("   Test data removed")
        
        print("\n" + "="*60)
        print("✅ All examples completed successfully!")
        print("="*60)
        
    except Error as e:
        print(f"\n❌ Database error: {e}")
        print("\nTroubleshooting:")
        print("1. Make sure XAMPP MySQL is running")
        print("2. Verify database 'lab_manager' exists")
        print("3. Run the SQL schema from XAMPP_MySQL_Setup.md")
        print("4. Check MySQL credentials (user/password)")