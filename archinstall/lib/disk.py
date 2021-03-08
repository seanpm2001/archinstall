import glob, re, os, json, time, hashlib
import pathlib
from collections import OrderedDict
from .exceptions import DiskError
from .general import *
from .output import log, LOG_LEVELS
from .storage import storage

ROOT_DIR_PATTERN = re.compile('^.*?/devices')
GPT = 0b00000001
MBR = 0b00000010

#import ctypes
#import ctypes.util
#libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
#libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p)

class BlockDevice():
	def __init__(self, path, info=None):
		if not info:
			# If we don't give any information, we need to auto-fill it.
			# Otherwise any subsequent usage will break.
			info = all_disks()[path].info

		self.path = path
		self.info = info
		self.part_cache = OrderedDict()
		# TODO: Currently disk encryption is a BIT missleading.
		#       It's actually partition-encryption, but for future-proofing this
		#       I'm placing the encryption password on a BlockDevice level.
		self.encryption_passwoed = None

	def __repr__(self, *args, **kwargs):
		return f"BlockDevice({self.device})"

	def __iter__(self):
		for partition in self.partitions:
			yield self.partitions[partition]

	def __getitem__(self, key, *args, **kwargs):
		if key not in self.info:
			raise KeyError(f'{self} does not contain information: "{key}"')
		return self.info[key]

	def json(self):
		"""
		json() has precedence over __dump__, so this is a way
		to give less/partial information for user readability.
		"""
		return {
			'path' : self.path,
			'size' : self.info['size'] if 'size' in self.info else '<unknown>',
			'model' : self.info['model'] if 'model' in self.info else '<unknown>'
		}

	def __dump__(self):
		return {
			'path': self.path,
			'info': self.info,
			'partition_cache': self.part_cache
		}

	@property
	def device(self):
		"""
		Returns the actual device-endpoint of the BlockDevice.
		If it's a loop-back-device it returns the back-file,
		If it's a ATA-drive it returns the /dev/X device
		And if it's a crypto-device it returns the parent device
		"""
		if "type" not in self.info:
			raise DiskError(f'Could not locate backplane info for "{self.path}"')

		if self.info['type'] == 'loop':
			for drive in json.loads(b''.join(sys_command(f'losetup --json', hide_from_log=True)).decode('UTF_8'))['loopdevices']:
				if not drive['name'] == self.path: continue

				return drive['back-file']
		elif self.info['type'] == 'disk':
			return self.path
		elif self.info['type'] == 'crypt':
			if 'pkname' not in self.info:
				raise DiskError(f'A crypt device ({self.path}) without a parent kernel device name.')
			return f"/dev/{self.info['pkname']}"

	#	if not stat.S_ISBLK(os.stat(full_path).st_mode):
	#		raise DiskError(f'Selected disk "{full_path}" is not a block device.')

	@property
	def partitions(self):
		o = b''.join(sys_command(f'partprobe {self.path}'))

		#o = b''.join(sys_command('/usr/bin/lsblk -o name -J -b {dev}'.format(dev=dev)))
		o = b''.join(sys_command(f'/usr/bin/lsblk -J {self.path}'))

		if b'not a block device' in o:
			raise DiskError(f'Can not read partitions off something that isn\'t a block device: {self.path}')

		if not o[:1] == b'{':
			raise DiskError(f'Error getting JSON output from:', f'/usr/bin/lsblk -J {self.path}')

		r = json.loads(o.decode('UTF-8'))
		if len(r['blockdevices']) and 'children' in r['blockdevices'][0]:
			root_path = f"/dev/{r['blockdevices'][0]['name']}"
			for part in r['blockdevices'][0]['children']:
				part_id = part['name'][len(os.path.basename(self.path)):]
				if part_id not in self.part_cache:
					## TODO: Force over-write even if in cache?
					self.part_cache[part_id] = Partition(root_path + part_id, part_id=part_id, size=part['size'])

		return {k: self.part_cache[k] for k in sorted(self.part_cache)}

	@property
	def partition(self):
		all_partitions = self.partitions
		return [all_partitions[k] for k in all_partitions]

	@property
	def partition_table_type(self):
		return GPT

	def has_partitions(self):
		return len(self.partitions)

	def has_mount_point(self, mountpoint):
		for partition in self.partitions:
			if self.partitions[partition].mountpoint == mountpoint:
				return True
		return False

class Partition():
	def __init__(self, path, part_id=None, size=-1, filesystem=None, mountpoint=None, encrypted=False):
		if not part_id:
			part_id = os.path.basename(path)
		self.path = path
		self.part_id = part_id
		self.mountpoint = mountpoint
		self.target_mountpoint = mountpoint
		self.filesystem = filesystem
		self.size = size # TODO: Refresh?
		self.encrypted = encrypted
		self.allow_formatting = False # A fail-safe for unconfigured partitions, such as windows NTFS partitions.

		if mountpoint:
			self.mount(mountpoint)

		mount_information = get_mount_info(self.path)
		fstype = get_filesystem_type(self.path) # blkid -o value -s TYPE self.path
		
		if self.mountpoint != mount_information.get('target', None) and mountpoint:
			raise DiskError(f"{self} was given a mountpoint but the actual mountpoint differs: {mount_information.get('target', None)}")

		if (target := mount_information.get('target', None)):
			self.mountpoint = target
		if (fstype := mount_information.get('fstype', fstype)):
			self.filesystem = fstype

	def __lt__(self, left_comparitor):
		if type(left_comparitor) == Partition:
			left_comparitor = left_comparitor.path
		else:
			left_comparitor = str(left_comparitor)
		return self.path < left_comparitor # Not quite sure the order here is correct. But /dev/nvme0n1p1 comes before /dev/nvme0n1p5 so seems correct.

	def __repr__(self, *args, **kwargs):
		mount_repr = ''
		if self.mountpoint:
			mount_repr = f", mounted={self.mountpoint}"
		elif self.target_mountpoint:
			mount_repr = f", rel_mountpoint={self.target_mountpoint}"

		if self.encrypted:
			return f'Partition(path={self.path}, real_device={self.real_device}, fs={self.filesystem}{mount_repr})'
		else:
			return f'Partition(path={self.path}, fs={self.filesystem}{mount_repr})'

	def has_content(self):
		temporary_mountpoint = '/tmp/'+hashlib.md5(bytes(f"{time.time()}", 'UTF-8')+os.urandom(12)).hexdigest()
		temporary_path = pathlib.Path(temporary_mountpoint)

		temporary_path.mkdir(parents=True, exist_ok=True)
		if (handle := sys_command(f'/usr/bin/mount {self.path} {temporary_mountpoint}')).exit_code != 0:
			raise DiskError(f'Could not mount and check for content on {self.path} because: {b"".join(handle)}')
		
		files = len(glob.glob(f"{temporary_mountpoint}/*"))
		sys_command(f'/usr/bin/umount {temporary_mountpoint}')

		temporary_path.rmdir()

		return True if files > 0 else False

	def safe_to_format(self):
		if self.allow_formatting is False:
			return False
		elif self.target_mountpoint == '/boot' and self.has_content():
			return False

		return True

	def format(self, filesystem=None, path=None, allow_formatting=None, log_formating=True):
		"""
		Format can be given an overriding path, for instance /dev/null to test
		the formating functionality and in essence the support for the given filesystem.
		"""
		if filesystem is None:
			filesystem = self.filesystem
		if path is None:
			path = self.path
		if allow_formatting is None:
			allow_formatting = self.allow_formatting

		if not allow_formatting:
			raise PermissionError(f"{self} is not formatable either because instance is locked ({self.allow_formatting}) or a blocking flag was given ({allow_formatting})")

		if log_formating:
			log(f'Formatting {path} -> {filesystem}', level=LOG_LEVELS.Info)

		if filesystem == 'btrfs':
			o = b''.join(sys_command(f'/usr/bin/mkfs.btrfs -f {path}'))
			if b'UUID' not in o:
				raise DiskError(f'Could not format {path} with {filesystem} because: {o}')
			self.filesystem = 'btrfs'

		elif filesystem == 'vfat':
			o = b''.join(sys_command(f'/usr/bin/mkfs.vfat -F32 {path}'))
			if (b'mkfs.fat' not in o and b'mkfs.vfat' not in o) or b'command not found' in o:
				raise DiskError(f'Could not format {path} with {filesystem} because: {o}')
			self.filesystem = 'vfat'

		elif filesystem == 'ext4':
			if (handle := sys_command(f'/usr/bin/mkfs.ext4 -F {path}')).exit_code != 0:
				raise DiskError(f'Could not format {path} with {filesystem} because: {b"".join(handle)}')
			self.filesystem = 'ext4'

		elif filesystem == 'xfs':
			if (handle := sys_command(f'/usr/bin/mkfs.xfs -f {path}')).exit_code != 0:
				raise DiskError(f'Could not format {path} with {filesystem} because: {b"".join(handle)}')
			self.filesystem = 'xfs'

		elif filesystem == 'f2fs':
			if (handle := sys_command(f'/usr/bin/mkfs.f2fs -f {path}')).exit_code != 0:
				raise DiskError(f'Could not format {path} with {filesystem} because: {b"".join(handle)}')
			self.filesystem = 'f2fs'

		elif filesystem == 'crypto_LUKS':
			from .luks import luks2
			encrypted_partition = luks2(self, None, None)
			encrypted_partition.format(path)
			self.filesystem = 'crypto_LUKS'

		else:
			raise UnknownFilesystemFormat(f"Fileformat '{filesystem}' is not yet implemented.")
		return True

	def find_parent_of(self, data, name, parent=None):
		if data['name'] == name:
			return parent
		elif 'children' in data:
			for child in data['children']:
				if (parent := self.find_parent_of(child, name, parent=data['name'])):
					return parent

	@property
	def real_device(self):
		if not self.encrypted:
			return self.path
		else:
			for blockdevice in json.loads(b''.join(sys_command('lsblk -J')).decode('UTF-8'))['blockdevices']:
				if (parent := self.find_parent_of(blockdevice, os.path.basename(self.path))):
					return f"/dev/{parent}"
			raise DiskError(f'Could not find appropriate parent for encrypted partition {self}')

	def mount(self, target, fs=None, options=''):
		if not self.mountpoint:
			log(f'Mounting {self} to {target}', level=LOG_LEVELS.Info)
			if not fs:
				if not self.filesystem: raise DiskError(f'Need to format (or define) the filesystem on {self} before mounting.')
				fs = self.filesystem
			## libc has some issues with loop devices, defaulting back to sys calls
		#	ret = libc.mount(self.path.encode(), target.encode(), fs.encode(), 0, options.encode())
		#	if ret < 0:
		#		errno = ctypes.get_errno()
		#		raise OSError(errno, f"Error mounting {self.path} ({fs}) on {target} with options '{options}': {os.strerror(errno)}")
			if sys_command(f'/usr/bin/mount {self.path} {target}').exit_code == 0:
				self.mountpoint = target
				return True

	def filesystem_supported(self):
		"""
		The support for a filesystem (this partition) is tested by calling
		partition.format() with a path set to '/dev/null' which returns two exceptions:
		 1. SysCallError saying that /dev/null is not formattable - but the filesystem is supported
		 2. UnknownFilesystemFormat that indicates that we don't support the given filesystem type
		"""
		try:
			self.format(self.filesystem, '/dev/null', log_formating=False, allow_formatting=True)
		except SysCallError:
			pass # We supported it, but /dev/null is not formatable as expected so the mkfs call exited with an error code
		except UnknownFilesystemFormat as err:
			raise err
		return True

class Filesystem():
	# TODO:
	#   When instance of a HDD is selected, check all usages and gracefully unmount them
	#   as well as close any crypto handles.
	def __init__(self, blockdevice, mode=GPT):
		self.blockdevice = blockdevice
		self.mode = mode

	def __enter__(self, *args, **kwargs):
		if self.blockdevice.keep_partitions is False:
			log(f'Wiping {self.blockdevice} by using partition format {self.mode}', level=LOG_LEVELS.Debug)
			if self.mode == GPT:
				if sys_command(f'/usr/bin/parted -s {self.blockdevice.device} mklabel gpt',).exit_code == 0:
					return self
				else:
					raise DiskError(f'Problem setting the partition format to GPT:', f'/usr/bin/parted -s {self.blockdevice.device} mklabel gpt')
			else:
				raise DiskError(f'Unknown mode selected to format in: {self.mode}')
		
		# TODO: partition_table_type is hardcoded to GPT at the moment. This has to be changed.
		elif self.mode == self.blockdevice.partition_table_type:
			log(f'Kept partition format {self.mode} for {self.blockdevice}', level=LOG_LEVELS.Debug)
		else:
			raise DiskError(f'The selected partition table format {self.mode} does not match that of {self.blockdevice}.')

	def __repr__(self):
		return f"Filesystem(blockdevice={self.blockdevice}, mode={self.mode})"

	def __exit__(self, *args, **kwargs):
		# TODO: https://stackoverflow.com/questions/28157929/how-to-safely-handle-an-exception-inside-a-context-manager
		if len(args) >= 2 and args[1]:
			raise args[1]
		b''.join(sys_command(f'sync'))
		return True

	def raw_parted(self, string:str):
		x = sys_command(f'/usr/bin/parted -s {string}')
		o = b''.join(x)
		return x

	def parted(self, string:str):
		"""
		Performs a parted execution of the given string

		:param string: A raw string passed to /usr/bin/parted -s <string>
		:type string: str
		"""
		return self.raw_parted(string).exit_code

	def use_entire_disk(self, prep_mode=None):
		self.add_partition('primary', start='1MiB', end='513MiB', format='vfat')
		self.set_name(0, 'EFI')
		self.set(0, 'boot on')
		self.set(0, 'esp on') # TODO: Redundant, as in GPT mode it's an alias for "boot on"? https://www.gnu.org/software/parted/manual/html_node/set.html
		if prep_mode == 'luks2':
			self.add_partition('primary', start='513MiB', end='100%')
		else:
			self.add_partition('primary', start='513MiB', end='100%', format=prep_mode)

	def add_partition(self, type, start, end, format=None):
		log(f'Adding partition to {self.blockdevice}', level=LOG_LEVELS.Info)
		
		previous_partitions = self.blockdevice.partitions
		if format:
			partitioning = self.parted(f'{self.blockdevice.device} mkpart {type} {format} {start} {end}') == 0
		else:
			partitioning = self.parted(f'{self.blockdevice.device} mkpart {type} {start} {end}') == 0

		if partitioning:
			start_wait = time.time()
			while previous_partitions == self.blockdevice.partitions:
				time.sleep(0.025) # Let the new partition come up in the kernel
				if time.time() - start_wait > 10:
					raise DiskError(f"New partition never showed up after adding new partition on {self} (timeout 10 seconds).")

			return True

	def set_name(self, partition:int, name:str):
		return self.parted(f'{self.blockdevice.device} name {partition+1} "{name}"') == 0

	def set(self, partition:int, string:str):
		return self.parted(f'{self.blockdevice.device} set {partition+1} {string}') == 0

def device_state(name, *args, **kwargs):
	# Based out of: https://askubuntu.com/questions/528690/how-to-get-list-of-all-non-removable-disk-device-names-ssd-hdd-and-sata-ide-onl/528709#528709
	if os.path.isfile('/sys/block/{}/device/block/{}/removable'.format(name, name)):
		with open('/sys/block/{}/device/block/{}/removable'.format(name, name)) as f:
			if f.read(1) == '1':
				return

	path = ROOT_DIR_PATTERN.sub('', os.readlink('/sys/block/{}'.format(name)))
	hotplug_buses = ("usb", "ieee1394", "mmc", "pcmcia", "firewire")
	for bus in hotplug_buses:
		if os.path.exists('/sys/bus/{}'.format(bus)):
			for device_bus in os.listdir('/sys/bus/{}/devices'.format(bus)):
				device_link = ROOT_DIR_PATTERN.sub('', os.readlink('/sys/bus/{}/devices/{}'.format(bus, device_bus)))
				if re.search(device_link, path):
					return
	return True

# lsblk --json -l -n -o path
def all_disks(*args, **kwargs):
	kwargs.setdefault("partitions", False)
	drives = OrderedDict()
	#for drive in json.loads(sys_command(f'losetup --json', *args, **lkwargs, hide_from_log=True)).decode('UTF_8')['loopdevices']:
	for drive in json.loads(b''.join(sys_command(f'lsblk --json -l -n -o path,size,type,mountpoint,label,pkname,model', *args, **kwargs, hide_from_log=True)).decode('UTF_8'))['blockdevices']:
		if not kwargs['partitions'] and drive['type'] == 'part': continue

		drives[drive['path']] = BlockDevice(drive['path'], drive)
	return drives

def convert_to_gigabytes(string):
	unit = string.strip()[-1]
	size = float(string.strip()[:-1])

	if unit == 'M':
		size = size/1024
	elif unit == 'T':
		size = size*1024

	return size

def harddrive(size=None, model=None, fuzzy=False):
	collection = all_disks()
	for drive in collection:
		if size and convert_to_gigabytes(collection[drive]['size']) != size:
			continue
		if model and (collection[drive]['model'] is None or collection[drive]['model'].lower() != model.lower()):
			continue

		return collection[drive]

def get_mount_info(path):
	try:
		output = b''.join(sys_command(f'/usr/bin/findmnt --json {path}'))
	except SysCallError:
		return {}

	output = output.decode('UTF-8')
	output = json.loads(output)
	if 'filesystems' in output:
		if len(output['filesystems']) > 1:
			raise DiskError(f"Path '{path}' contains multiple mountpoints: {output['filesystems']}")

		return output['filesystems'][0]

def get_filesystem_type(path):
	output = b''.join(sys_command(f"blkid -o value -s TYPE {path}"))
	return output.strip().decode('UTF-8')