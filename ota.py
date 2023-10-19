"""
This is an Over-The-Air (OTA) utility for updating a microcontroller, such as Raspberry Pi Pico W, over a Wi-Fi network.

How it works:
  - Files committed to GitHub repositories are monitored for changes.
  - When the `updated()` method is called, GitHub is queried for any new commits.
  - If updates are found, the latest file versions are pulled down onto the microcontroller.
  - Files added to a GitHub repo can be automatically downloaded to the microcontroller.

Update Intervals
   The parameter "update_interval_minutes" specifies how often to actually update files.
   If you call the "updated()" method during the interval, no updates will occur.  If
   you call "updated()" after the timer has expired, you will get updates and the Pico
   WILL BE RESET (i.e. rebooted). You can avoid having your Pico reset by simply not
   specifying an update interval. But, you will be responsible for tracking timers and
   how often you want to do updates.

Features:
  - Track files in multiple repositories.
  - Uses the GitHub REST API to track file updates.

There are 5 classes defined here:
  1. OTAUpdater: Manages the updating of files to the latest version.
  2. OTAFileMetadata: Stores metadata for individual files.
  3. OTADatabase: Handles read/write of file info to a local "database."
  4. OTANewFileWillNotValidate: Exception for new files that will not validate prior to use.
  5. OTANoMemory: Exception for running out of memory due to a known 'urequests' bug.

Tested on:
  - Raspberry Pi Pico W - firmware v1.20.0 (2023-04-26 vintage)
  - Raspberry Pi Pico W - firmware v1.21.0 (2023-10-06 vintage)


Caveats/Limitations:
  - Only works with single files, not directories.

Example Usage:
if __name__ == "__main__":
    from machine import reset
    import time
    import secrets

    OTA_UPDATE_GITHUB_REPOS = {
        "gamename/raspberry-pi-pico-w-mailbox-sensor": ["boot.py", "main.py", "mailbox.py"],
        "gamename/micropython-over-the-air-utility": ["ota.py"],
        "gamename/micropython-utilities": ["utils.py", "cleanup_logs.py"]
    }

    ota_updater = OTAUpdater(
        secrets.GITHUB_USER,
        secrets.GITHUB_TOKEN,
        OTA_UPDATE_GITHUB_REPOS,
        update_interval_minutes=60,  # Set the update interval to 60 minutes
        debug=True,
        save_backups=True
    )

    ota_updater.updated()  # Check for updates, and apply if available
    print("MAIN: Updates checked. Continuing with the main code...")

Thanks:
  This project was inspired by, and loosely based on, Kevin McAleer's project https://github.com/kevinmcaleer/ota
"""

import gc
import hashlib
import json
import os
import time

import machine
import ubinascii
import uos
import urequests as requests
import utime


def calculate_github_sha(filename):
    """
    This will generate the same sha1 value as GitHub's own calculation

    :param filename: The file get our sha value for
    :type filename: str
    :return: hex string
    :rtype: str
    """
    try:
        # Does the file exist?
        uos.stat(filename)
    except OSError:
        # Nope, guess not
        return ''
    else:
        s = hashlib.sha1()
        # Open the file in binary mode
        with open(filename, "rb") as file:
            chunk_size = 1024
            data = bytes()
            while True:
                chunk = file.read(chunk_size)
                if not chunk:
                    break
                data += chunk

        s.update("blob %u\0" % len(data))
        s.update(data)
        s_binary = s.digest()

        # Convert the binary digest to a hexadecimal string
        return ''.join('{:02x}'.format(byte) for byte in s_binary)


def valid_code(file_path) -> bool:
    """
    Verify a file contains reasonably error-free python code. This isn't perfect,
    but it will tell you if a file is at least syntactically correct.

    :param file_path: A file path
    :type file_path: str
    :return: True or False
    :rtype: bool
    """
    try:
        with open(file_path, 'r') as file:
            python_code = file.read()
            compile(python_code, file_path, 'exec')
            return True  # Code is valid
    except (SyntaxError, FileNotFoundError):
        return False  # Code is invalid or file not found


class OTANoMemory(Exception):
    """
    Due to a known mem leak issue in 'urequests', flag when we run into it.
    """

    def __init__(self, message="Insufficient memory to continue"):
        self.message = message
        super().__init__(self.message)


class OTANewFileWillNotValidate(Exception):
    """
    When we pull a new copy of a file, prior to its use, we validate that
    the code is at least syntactically correct. This exception is generated
    when we detect a problem.
    """

    def __init__(self, message="The new file will not validate"):
        self.message = message
        super().__init__(self.message)


class OTAUpdater:
    """
    This is to update a microcontroller (e.g. Raspberry Pi Pico W) over-the-air (OTA). It does
    this by monitoring a GitHub repo for changes.

    Attributes:
        organization - The GitHub repository organization name
        repository - The GitHub repository name
        repo_dct - A dictionary of repositories and their files to be updated
    """

    def __init__(self, github_userid, github_token, repo_dct, update_interval_minutes=None,
                 update_on_initialization=False, debug=False, save_backups=False):
        """
        Initialize the OTAUpdater.

        :param github_userid: The GitHub user id.
        :param github_token: The GitHub user token.
        :param repo_dct: A dictionary of repositories and their files to be updated.
        :param update_interval_minutes: Update interval in minutes (optional, None for no timer).
        :param update_on_initialization: Incorporate updates at init time.
        :param debug: Enable debug mode.
        :param save_backups: Save backup copies of files.
        """
        gc.enable()  # In case it is not in your 'boot.py' file
        self.files_obj = []
        self.debug = debug
        self.save_backups = save_backups

        for repo in repo_dct.keys():
            for file in repo_dct[repo]:
                self.files_obj.append(OTAFileMetadata(github_userid, github_token, repo, file,
                                                      debug=self.debug, save_backups=self.save_backups))

        self.db = OTADatabase(self.files_obj, debug=self.debug)
        self.update_interval_minutes = update_interval_minutes  # Update interval in minutes
        if self.update_interval_minutes is not None:
            self.update_interval_seconds = update_interval_minutes * 60  # Convert to seconds
        else:
            self.update_interval_seconds = None  # No timer

        self.last_update_time = None  # Initialize last update time

        if update_on_initialization:
            self.updated(force_update=True)

    def debug_print(self, msg):
        """
        Print only when debug enabled
        """
        if self.debug:
            print(msg)

    def fetch_updates(self):
        """
        Walk through all the OTAFileMetadata objects in a list and update each to
        the latest GitHub version

        :return: Nothing
        """
        try:
            self.debug_print("OTAU: Pulling latest GitHub versions ...")
            for ndx, _ in enumerate(self.files_obj):
                file = self.files_obj[ndx].get_filename()
                self.debug_print(f"OTAU: ... {file}")
                self.files_obj[ndx].update_latest()
            self.debug_print("OTAU: GitHub pulls completed")
        except OTANewFileWillNotValidate:
            print("OTAU: Validation error. Cannot update")

    def _check_for_updates(self):
        """
        Check for updates from GitHub and apply them if available.

        :return: True if updates were applied, False otherwise.
        """
        self.debug_print("OTAU: Checking for updates")
        files_updated_flag = False
        self.fetch_updates()
        self.debug_print("OTAU: Comparing GitHub version with local versions ...")
        for entry in self.files_obj:
            self.debug_print(f"OTAU: ... {entry.get_filename()}")
            if entry.new_version_available():
                self.debug_print(f'OTAU: --> {entry.get_filename()} updated')
                self.debug_print(f'OTAU: current: {entry.get_current()}')
                self.debug_print(f'OTAU: latest:  {entry.get_latest()}')
                entry.set_current_to_latest()
                self.db.update(entry.to_json())
                if not files_updated_flag:
                    files_updated_flag = True

        return files_updated_flag

    def updated(self, force_update=False) -> bool:
        """
        Check for updates and apply them if the update interval has expired (if set).

        :param force_update: If True, force an update regardless of the update interval.
        :type force_update: bool
        :return: True if updates were applied, False otherwise.
        """
        current_time = utime.time()

        # If force_update is True, always check for updates
        if force_update:
            return self._check_and_apply_updates(current_time)

        # Check if the update interval has expired (if a timer is set)
        if self.update_interval_seconds is not None:
            if self.last_update_time is None or current_time - self.last_update_time >= self.update_interval_seconds:
                return self._check_and_apply_updates(current_time)
            else:
                self.debug_print("OTAU: Update interval not yet expired")
                return False
        else:
            # No timer, always check for updates
            return self._check_and_apply_updates(current_time)

    def _check_and_apply_updates(self, current_time) -> bool:
        """
        Check for updates and apply them if updates are available.

        :param current_time: The current time.
        :type current_time: int
        :return: True if updates were applied, False otherwise.
        """
        if self._check_for_updates():
            self.last_update_time = current_time  # Update the last update time
            self.debug_print("OTAU: Updates applied. Resetting system.")
            utime.sleep(1)  # Sleep for a moment before resetting
            machine.reset()
        else:
            self.debug_print("OTAU: No updates found")
            return False


class OTAFileMetadata:
    """
    This class contains the version metadata for individual files on GitHub.

    Attributes:
        organization - The GitHub repository organization name
        repository - The GitHub repository name
        filename - A single file name to be monitored
    """

    LATEST_FILE_PREFIX = '__latest__'
    ERROR_FILE_PREFIX = '__error__'
    BACKUP_FILE_PREFIX = '__backup__'
    OTA_MINIMUM_MEMORY = 32000

    def __init__(self, user, token, repository, filename, debug=False, save_backups=False):
        """
        Initializer

        :param repository: The GitHub repository
        :type repository: str
        :param filename: A file to monitor and update
        :type filename: str
        :param debug: Enable debug
        :type debug: bool
        """
        gc.enable()  # mem leak bugs
        self.filename = filename
        self.url = f'https://api.github.com/repos/{repository}/contents/{self.filename}'
        self.debug = debug
        self.save_backups = save_backups
        self.latest = None
        self.latest_file = None
        self.current = calculate_github_sha(self.filename)
        self.request_header = {
            "Authorization": f"token {token}",
            'User-Agent': user
        }
        self.update_latest()

    def debug_print(self, msg):
        """
        Print only when debug is enabled
        """
        if self.debug:
            print(msg)

    def to_json(self):
        """
        Convert the object to json string

        :return: a json string
        :rtype: json
        """
        return {
            self.filename: {
                "latest": self.latest,
                "current": self.current
            }
        }

    def mem_check(self):
        """
        There is a known mem leak in 'urequests'. Below is a workaround attempt
        """
        gc.collect()
        free_mem = gc.mem_free()
        self.debug_print(f"OTAF: Free mem: {free_mem}")
        if free_mem < self.OTA_MINIMUM_MEMORY:
            raise OTANoMemory()

    def update_latest(self):
        """
        Query GitHub for the latest version of our file

        :return: Nothing
        """
        self.mem_check()
        try:
            response = requests.get(self.url, headers=self.request_header).json()
        except ValueError:
            print("OTAF: Json error in response")
            print("OTAF: URL = ", self.url)
        except MemoryError:
            raise OTANoMemory()
        else:
            self.mem_check()
            if 'sha' in response:
                self.latest = response['sha']
                if self.new_version_available():
                    file_content = ubinascii.a2b_base64(response['content'])
                    self.latest_file = self.LATEST_FILE_PREFIX + self.get_filename()
                    with open(self.latest_file, 'w') as f:
                        f.write(str(file_content, 'utf-8'))
                    if not valid_code(self.latest_file):
                        error_file = self.ERROR_FILE_PREFIX + self.get_filename()
                        # keep a copy for forensics
                        os.rename(self.latest_file, error_file)
                        self.latest_file = None
                        raise OTANewFileWillNotValidate(f'New {self.get_filename()} will not validate')
            else:
                print("OTAF: URL = ", self.url)
                print("OTAF: response = ", response)
                time.sleep(1)

    def get_filename(self):
        """
        Get the file name we are monitoring

        :return: The file name
        :rtype: str
        """
        return self.filename

    def get_current(self):
        """
        Get the sha value of the file we have on the microcontroller

        :return: The current value
        :rtype: str
        """
        return self.current

    def get_latest(self):
        """
        Get the latest sha value for this file taken from GitHub

        :return: The latest version sha value
        :rtype: string
        """
        return self.latest

    def set_current_to_latest(self):
        """
        Set the current value to the latest value

        :return: Nothing
        """
        if self.save_backups and bool(self.get_filename() in os.listdir()):
            backup_file = self.BACKUP_FILE_PREFIX + self.get_filename()
            os.rename(self.get_filename(), backup_file)
        os.rename(self.latest_file, self.get_filename())
        self.current = self.latest

    def new_version_available(self):
        """
        Determine if there is a newer version available by comparing the current/latest
        sha values.

        :return: True or False
        :rtype: bool
        """
        return bool(self.current != self.latest)


class OTADatabase:
    """
    A simple database of files being monitored

    Attributes:
        :param: memory_resident_version_list - A list of OTAFileMetadata objects
    """
    DB_FILE = 'versions.json'

    def __init__(self, memory_resident_version_list, debug=False):
        """
        Initializer.
        1. Read the database if it exists
        2. Create the database if it does not exist
        3. Sync the contents of the database and the OTAFileMetadata objects passed as attributes

        :param memory_resident_version_list: A list of OTAFileMetadata objects
        :type memory_resident_version_list: list
        :param debug: Enable debug
        :type debug: bool
        """
        self.filename = self.DB_FILE
        self.debug = debug
        self.version_entries = memory_resident_version_list
        if self.db_file_exists():
            for version_entry in self.version_entries:
                filename = version_entry.get_filename()
                if not self.entry_exists(filename):
                    self.create(version_entry.to_json())
        else:
            for entry in self.version_entries:
                self.create(entry.to_json())

    def debug_print(self, msg):
        """
        Print only when debug enabled
        """
        if self.debug:
            print(msg)

    def db_file_exists(self):
        """
        Does our database file exist?

        :return: True or False
        :rtype: bool
        """
        return bool(self.filename in os.listdir())

    def read(self):
        """
        Read the entire database

        :return: A json string or None if the db doesn't exist
        :rtype: json or None
        """
        try:
            with open(self.filename, 'r') as file:
                data = json.load(file)
            return data
        except OSError:
            return None

    def write(self, data):
        """
        Write the whole database

        :param data: A list of json strings
        :type data: list
        :return: Nothing
        """
        with open(self.filename, 'w') as file:
            json.dump(data, file)

    def create(self, item):
        """
        Create a new db entry

        :param item: json string
        :type item: json
        :return: Nothing
        """
        filename = list(item)[0]
        if not self.entry_exists(filename):
            data = self.read()
            if not data:
                data = {}
            data.update(item)
            self.write(data)
        else:
            raise RuntimeError(f'OTAD: Already an entry for {filename} in database')

    def entry_exists(self, filename):
        """
        Find out if a db entry exists for a particular file

        :param filename: A file name
        :type filename: str
        :return: True or False
        :rtype: bool
        """
        entry_exists = False
        data = self.read()
        if data:
            for key in data.keys():
                if bool(key == filename):
                    entry_exists = True
                    break
        return entry_exists

    def get_entry(self, filename):
        """
        Get a particular db entry based on the file name

        :param filename: a file name
        :type filename: str
        :return: json string or None if not found
        :rtype: json or None
        """
        retval = None
        data = self.read()
        if data:
            for key in data.keys():
                if bool(key == filename):
                    retval = data[key]
        return retval

    def update(self, new_item):
        """
        Update a db entry

        :param new_item: A json string
        :type new_item: json
        :return: Nothing
        """
        filename = list(new_item)[0]
        data = self.read()
        self.delete(filename)
        data.update(new_item)
        self.write(data)

    def delete(self, filename):
        """
        Delete a db entry.

        :param filename: A filename to look up the entry
        :type filename: str
        :return: Nothing
        """
        data = self.read()
        if self.entry_exists(filename):
            del data[filename]
            self.write(data)
