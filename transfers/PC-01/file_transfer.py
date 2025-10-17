# file_transfer.py
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
from datetime import datetime
import os
import shutil
import math

class FileTransferManager(QObject):
    """
    Chunked, simulated/network-agnostic File Transfer Manager.

    - send_file(filepath, target_pcs) will split the file into chunks and simulate sending
      each chunk to each target PC (in parallel). Progress updates are emitted.
    - When a transfer to a given PC completes, the file is saved into transfers/<pc>/<filename>.
    - Supports inbox per PC (list of received filenames).
    """

    # Signals:
    # progress: transfer_id (str), pc_name (str), percent (int)
    progress = pyqtSignal(str, str, int)
    # complete: transfer_id, pc_name, saved_path
    complete = pyqtSignal(str, str, str)
    # failed: transfer_id, pc_name, reason
    failed = pyqtSignal(str, str, str)
    # new_transfer record: transfer_id, meta
    new_transfer = pyqtSignal(str, dict)

    def __init__(self, transfers_dir="transfers", chunk_size=1024*1024, parent=None):
        """
        chunk_size default 1MB (can be lowered or raised).
        transfers_dir: where received files will be stored.
        """
        super().__init__(parent)
        self.transfers_dir = transfers_dir
        os.makedirs(self.transfers_dir, exist_ok=True)

        self.chunk_size = chunk_size
        self._transfers = {}   # transfer_id -> {filepath, filename, size, total_chunks, targets: {pc: state}}
        self.inbox = {}        # pc_name -> [saved_paths,...]
        self.history = []      # list of transfer records (metadata)

    def _make_transfer_id(self, filename):
        ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{ts}_{filename}"

    def send_file(self, filepath: str, target_pcs: list):
        """
        Start sending filepath to all target_pcs.
        Returns transfer_id.
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError("File not found")

        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)
        total_chunks = math.ceil(filesize / self.chunk_size) if filesize > 0 else 1
        transfer_id = self._make_transfer_id(filename)

        # initialize entry
        entry = {
            "id": transfer_id,
            "filepath": os.path.abspath(filepath),
            "filename": filename,
            "size": filesize,
            "total_chunks": total_chunks,
            "targets": {}
        }

        for pc in target_pcs:
            entry["targets"][pc] = {
                "sent_chunks": 0,
                "status": "queued",  # queued / sending / completed / failed
            }

        self._transfers[transfer_id] = entry
        self.history.append({
            "id": transfer_id,
            "filename": filename,
            "targets": list(entry["targets"].keys()),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "size": filesize
        })

        # emit new transfer info
        self.new_transfer.emit(transfer_id, {
            "id": transfer_id,
            "filename": filename,
            "size": filesize,
            "targets": list(entry["targets"].keys()),
            "total_chunks": total_chunks
        })

        # start chunked delivery: read file and schedule chunk sends
        # We'll read chunk-by-chunk and for each chunk, start a QTimer.singleShot to simulate network latency.
        with open(filepath, "rb") as f:
            chunk_index = 0
            while True:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                # schedule sending this chunk to each pc
                for pc in entry["targets"].keys():
                    # closure capture
                    QTimer.singleShot(0, lambda c=chunk, pc=pc, tid=transfer_id, idx=chunk_index: self._deliver_chunk(tid, pc, idx, c))
                chunk_index += 1

        return transfer_id

    def _deliver_chunk(self, transfer_id: str, pc_name: str, chunk_idx: int, chunk_data: bytes):
        """
        Internal: simulate sending a chunk to a PC.
        We'll simulate per-chunk transmission latency proportional to chunk size (very small).
        On completion of last chunk for that PC, we assemble and write the file.
        """
        # minimal checks
        if transfer_id not in self._transfers:
            self.failed.emit(transfer_id, pc_name, "Unknown transfer")
            return

        transfer = self._transfers[transfer_id]
        target = transfer["targets"].get(pc_name)
        if target is None:
            self.failed.emit(transfer_id, pc_name, "Unknown target")
            return

        # mark sending
        if target["status"] == "queued":
            target["status"] = "sending"

        # simulate small latency proportional to chunk length:
        simulated_latency_ms = max(10, int(len(chunk_data) / 1024))  # e.g., 1ms per KB
        # use a singleShot to simulate chunk arrival after latency
        QTimer.singleShot(simulated_latency_ms, lambda tid=transfer_id, pc=pc_name, idx=chunk_idx, c=chunk_data: self._on_chunk_arrived(tid, pc, idx, c))

    def _on_chunk_arrived(self, transfer_id: str, pc_name: str, chunk_idx: int, chunk_data: bytes):
        """
        Called after simulated network latency; stores chunk to a temp folder per transfer+pc.
        When all chunks have arrived for a pc, write final file and mark complete.
        """
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            self.failed.emit(transfer_id, pc_name, "Transfer disappeared")
            return

        pc_state = transfer["targets"].get(pc_name)
        if pc_state is None:
            self.failed.emit(transfer_id, pc_name, "Target missing")
            return

        # create temp folder for this transfer/pc
        temp_dir = os.path.join(self.transfers_dir, "__tmp__", transfer_id, pc_name)
        os.makedirs(temp_dir, exist_ok=True)
        chunk_path = os.path.join(temp_dir, f"chunk_{chunk_idx:06d}.part")
        try:
            with open(chunk_path, "wb") as cf:
                cf.write(chunk_data)
        except Exception as e:
            pc_state["status"] = "failed"
            self.failed.emit(transfer_id, pc_name, str(e))
            return

        # increment sent chunks
        pc_state["sent_chunks"] = pc_state.get("sent_chunks", 0) + 1

        # update progress percent
        total_chunks = transfer["total_chunks"]
        percent = int(pc_state["sent_chunks"] / total_chunks * 100)
        self.progress.emit(transfer_id, pc_name, percent)

        # when all chunks done for this pc, assemble file
        if pc_state["sent_chunks"] >= total_chunks:
            # assemble final path
            pc_dir = os.path.join(self.transfers_dir, pc_name)
            os.makedirs(pc_dir, exist_ok=True)
            final_path = os.path.join(pc_dir, transfer["filename"])

            # assemble by sorted chunk files
            chunk_files = sorted([f for f in os.listdir(temp_dir) if f.startswith("chunk_")])
            try:
                with open(final_path, "wb") as outf:
                    for cfname in chunk_files:
                        with open(os.path.join(temp_dir, cfname), "rb") as rcf:
                            shutil.copyfileobj(rcf, outf)
                # cleanup temp dir for this pc
                for cfname in chunk_files:
                    os.remove(os.path.join(temp_dir, cfname))
                os.rmdir(temp_dir)
            except Exception as e:
                pc_state["status"] = "failed"
                self.failed.emit(transfer_id, pc_name, str(e))
                return

            pc_state["status"] = "completed"
            # add to inbox
            if pc_name not in self.inbox:
                self.inbox[pc_name] = []
            self.inbox[pc_name].append(final_path)
            # emit complete
            self.complete.emit(transfer_id, pc_name, final_path)

    def get_inbox(self, pc_name: str):
        """Return list of saved file paths for this PC (received files)."""
        return list(self.inbox.get(pc_name, []))

    def get_history(self):
        """Return high-level history of transfers."""
        return list(self.history)

    def cancel_transfer_for_pc(self, transfer_id: str, pc_name: str):
        """Mark a target as failed and stop processing further chunks for it (simulation)."""
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return False
        target = transfer["targets"].get(pc_name)
        if not target:
            return False
        target["status"] = "failed"
        self.failed.emit(transfer_id, pc_name, "Cancelled by user")
        return True
