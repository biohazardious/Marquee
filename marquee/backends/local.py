import os
import shutil
import time


class LocalCopy:
    def __init__(self, copy_path):
        if not copy_path.endswith(os.path.sep):
            copy_path += os.path.sep
        self.copy_path = copy_path
        self.source = None
        self.destination = None

    def copy(self, source, destination):
        self.source = source
        self.destination = os.path.join(self.copy_path, destination)

        if os.path.isfile(self.source):
            destination_path = os.path.join(self.destination, os.path.basename(source))
            self._copy_file(self.source, destination_path)
        elif os.path.isdir(self.source):
            self._copy_directory()

    def _copy_file(self, src, dest):
        if not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)

        if os.path.exists(dest) and os.path.getsize(src) == os.path.getsize(dest):
            print(f"File '{src}' already exists and same size. Skipping copy...")
            return

        start_time = time.time()
        shutil.copy2(src, dest)
        end_time = time.time()
        transfer_speed = os.path.getsize(src) / (end_time - start_time) / (1024 * 1024)
        print(f"Copied '{src}' to '{dest}' ({transfer_speed:.2f} MB/s)")

    def _copy_directory(self):
        if not os.path.exists(self.destination):
            os.makedirs(self.destination)

        for root, dirs, files in os.walk(self.source):
            for file in files:
                source_path = os.path.join(root, file)
                dest_path = os.path.join(self.destination, os.path.relpath(source_path, self.source))
                self._copy_file(source_path, dest_path)
