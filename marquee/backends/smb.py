import socket
import os
import re
from smb import smb_structs
from smb.SMBConnection import SMBConnection


class RemoteCopy:
    def __init__(self, conn_str):
        self.src = None
        self.dest = None
        self.username = ''
        self.password = ''
        self.server_ip = None
        self.share_name = None
        self.remote_path = None
        self.conn = None
        self._parse_connection_string(conn_str)
        self._open_connection()
        # print(self.username, self.password, self.server_ip, self.remote_path)

    def _parse_connection_string(self, conn_str):
        pattern = r'smb:\/\/(?:([\w-]+)(?::([\w-]+))?@)?([\d.]+)\/([\w\/-]+)'
        # parsing the input string
        match = re.match(pattern, conn_str)
        # printing the parsed output
        if match:
            self.username = match.group(1) or ''
            self.password = match.group(2) or ''
            self.server_ip = match.group(3)
            self.share_name = match.group(4).split('/')[0]
            self.remote_path = '/'.join(match.group(4).split('/')[1:])
            if not self.remote_path.endswith('/'):
                self.remote_path += '/'
        else:
            raise ValueError('Invalid connection string')

    def _open_connection(self):
        self.conn = SMBConnection(self.username, self.password, 'client', self.server_ip, use_ntlm_v2=True)
        try:
            self.conn.connect(self.server_ip)
        except socket.error as e:
            print(f'Failed to connect to server: {e}')
            return

    def __del__(self):
        self.conn.close()

    def _copy_file(self, local_file_path, remote_file):
        # Check if the remote file exists and has the same size as the local file
        local_filename = os.path.basename(local_file_path)
        remote_file_path = os.path.dirname(remote_file)
        exists = False
        size_match = False
        for f in self.conn.listPath(self.share_name, remote_file_path):
            if f.filename == local_filename:
                exists = True
                size_match = f.file_size == os.path.getsize(local_file_path)
                print(f"File '{local_file_path}' already exists and same size. Skipping upload...")
                break

        # Upload the file only if it does not exist or has a different size
        if not exists or not size_match:
            with open(local_file_path, 'rb') as f:
                self.conn.storeFile(self.share_name, remote_file, f, 30, True)

    def _copy_folder(self, local_folder_path, remote_folder_path):
        self.create_remote_directory(remote_folder_path)
        # Upload all files in the local folder to the remote folder

        for file in os.listdir(local_folder_path):
            local_path = os.path.join(local_folder_path, file)
            remote_path = os.path.join(remote_folder_path, file)
            if os.path.isfile(local_path):
                self._copy_file(local_path, remote_path)
            elif os.path.isdir(local_path):
                self._copy_folder(local_path, remote_path)

    def create_remote_directory(self, remote_path):
        subdirs = remote_path.split('/')
        basepath = ''
        for subdir in subdirs:
            if subdir == '':
                continue
            basepath = os.path.join(basepath, subdir)
            try:
                self.conn.createDirectory(self.share_name, basepath)
            except smb_structs.OperationFailure as e:
                pass

    def copy(self, src, dest):
        dest = str(self.remote_path + dest).replace("//", "/")
        if os.path.isfile(src):
            remote_path = os.path.join(dest, os.path.basename(src))
            self.create_remote_directory(dest)
            self._copy_file(src, remote_path)
        elif os.path.isdir(src):
            self._copy_folder(src, dest)
