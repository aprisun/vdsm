#
# Copyright 2008-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#


# stdlib imports
from collections import namedtuple
from contextlib import contextmanager
from xml.dom.minidom import parseString as _domParseStr
import itertools
import logging
import os
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

# 3rd party libs imports
import libvirt

# vdsm imports
from vdsm import concurrent
from vdsm import constants
from vdsm import cpuarch
from vdsm import hooks
from vdsm import host
from vdsm import hostdev
from vdsm import libvirtconnection
from vdsm import numa
from vdsm import osinfo
from vdsm import qemuimg
from vdsm import response
from vdsm import supervdsm
from vdsm import utils
from vdsm.compat import pickle
from vdsm.config import config
from vdsm.define import ERROR, NORMAL, doneCode, errCode
from vdsm.logUtils import SimpleLogAdapter
from vdsm.network import api as net_api
from vdsm.storage import fileUtils
from vdsm.virt import guestagent
from vdsm.virt import sampling
from vdsm.virt import vmchannels
from vdsm.virt import vmexitreason
from vdsm.virt import virdomain
from vdsm.virt import vmstats
from vdsm.virt import vmstatus
from vdsm.virt.vmpowerdown import VmShutdown, VmReboot
from vdsm.virt.utils import isVdsmImage, cleanup_guest_socket
from storage import outOfProcess as oop
from storage import sd

# local imports
# In future those should be imported via ..
import caps

# local package imports
from .domain_descriptor import DomainDescriptor
from . import migration
from . import recovery
from . import vmdevices
from .vmdevices import hwclass
from .vmdevices.storage import DISK_TYPE
from .vmtune import update_io_tune_dom, collect_inner_elements
from .vmtune import io_tune_values_to_dom, io_tune_dom_to_values
from . import vmxml
from .vmxml import METADATA_VM_TUNE_URI, METADATA_VM_TUNE_ELEMENT
from .vmxml import METADATA_VM_TUNE_PREFIX


DEFAULT_BRIDGE = config.get("vars", "default_bridge")

# A libvirt constant for undefined cpu quota
_NO_CPU_QUOTA = 0

# A libvirt constant for undefined cpu period
_NO_CPU_PERIOD = 0


def _filterSnappableDiskDevices(diskDeviceXmlElements):
    return [x for x in diskDeviceXmlElements
            if x.getAttribute('device') in ('disk', 'lun', '')]


class VolumeError(RuntimeError):
    def __str__(self):
        return "Bad volume specification " + RuntimeError.__str__(self)


class DoubleDownError(RuntimeError):
    pass


class ImprobableResizeRequestError(RuntimeError):
    pass


class BlockJobExistsError(Exception):
    pass


VALID_STATES = (vmstatus.DOWN, vmstatus.MIGRATION_DESTINATION,
                vmstatus.MIGRATION_SOURCE, vmstatus.PAUSED,
                vmstatus.POWERING_DOWN, vmstatus.REBOOT_IN_PROGRESS,
                vmstatus.RESTORING_STATE, vmstatus.SAVING_STATE,
                vmstatus.UP, vmstatus.WAIT_FOR_LAUNCH)


class ConsoleDisconnectAction:
    NONE = 'NONE'
    LOCK_SCREEN = 'LOCK_SCREEN'
    SHUTDOWN = 'SHUTDOWN'
    LOGOUT = 'LOGOUT'
    REBOOT = 'REBOOT'


# These strings are representing libvirt virDomainEventType values
# http://libvirt.org/html/libvirt-libvirt-domain.html#virDomainEventType
_EVENT_STRINGS = (
    "Defined",
    "Undefined",
    "Started",
    "Suspended",
    "Resumed",
    "Stopped",
    "Shutdown",
    "PM-Suspended",
    "Crashed",
)


def eventToString(event):
    try:
        return _EVENT_STRINGS[event]
    except IndexError:
        return "Unknown (%i)" % event


class SetLinkAndNetworkError(Exception):
    pass


class UpdatePortMirroringError(Exception):
    pass


VolumeChainEntry = namedtuple('VolumeChainEntry',
                              ['uuid', 'path', 'allocation'])

VolumeSize = namedtuple("VolumeSize",
                        ["apparentsize", "truesize"])


class MigrationError(Exception):
    pass


class StorageUnavailableError(Exception):
    pass


class HotunplugTimeout(Exception):
    pass


class MissingLibvirtDomainError(Exception):
    def __init__(self, reason=vmexitreason.LIBVIRT_DOMAIN_MISSING):
        super(MissingLibvirtDomainError, self).__init__(
            vmexitreason.exitReasons.get(reason, 'Missing VM'))
        self.reason = reason


class DestroyedOnStartupError(Exception):
    """
    The VM was destroyed while it was starting up.
    This most likely happens because the startup is very slow.
    """


class Vm(object):
    """
    Used for abstracting communication between various parts of the
    system and Qemu.

    Runs Qemu in a subprocess and communicates with it, and monitors
    its behaviour.
    """

    log = logging.getLogger("virt.vm")
    # limit threads number until the libvirt lock will be fixed
    _ongoingCreations = threading.BoundedSemaphore(4)
    DeviceMapping = ((hwclass.DISK, vmdevices.storage.Drive),
                     (hwclass.NIC, vmdevices.network.Interface),
                     (hwclass.SOUND, vmdevices.core.Sound),
                     (hwclass.VIDEO, vmdevices.core.Video),
                     (hwclass.GRAPHICS, vmdevices.graphics.Graphics),
                     (hwclass.CONTROLLER, vmdevices.core.Controller),
                     (hwclass.GENERAL, vmdevices.core.Generic),
                     (hwclass.BALLOON, vmdevices.core.Balloon),
                     (hwclass.WATCHDOG, vmdevices.core.Watchdog),
                     (hwclass.CONSOLE, vmdevices.core.Console),
                     (hwclass.REDIR, vmdevices.core.Redir),
                     (hwclass.RNG, vmdevices.core.Rng),
                     (hwclass.SMARTCARD, vmdevices.core.Smartcard),
                     (hwclass.TPM, vmdevices.core.Tpm),
                     (hwclass.HOSTDEV, vmdevices.hostdevice.HostDevice),
                     (hwclass.MEMORY, vmdevices.core.Memory))

    def _emptyDevMap(self):
        return dict((dev, []) for dev, _ in self.DeviceMapping)

    def _makeChannelPath(self, deviceName):
        return constants.P_LIBVIRT_VMCHANNELS + self.id + '.' + deviceName

    def __init__(self, cif, params, recover=False):
        """
        Initialize a new VM instance.

        :param cif: The client interface that creates this VM.
        :type cif: :class:`clientIF.clientIF`
        :param params: The VM parameters.
        :type params: dict
        :param recover: Signal if the Vm is recovering;
        :type recover: bool
        """
        self._dom = virdomain.Disconnected(params["vmId"])
        self.recovering = recover
        self.conf = {'pid': '0', '_blockJobs': {}, 'clientIp': ''}
        self.conf.update(params)
        if 'smp' not in self .conf:
            self.conf['smp'] = '1'
        # restore placeholders for BC sake
        vmdevices.graphics.initLegacyConf(self.conf)
        self.cif = cif
        self.log = SimpleLogAdapter(self.log, {"vmId": self.conf['vmId']})
        self._destroy_requested = threading.Event()
        self._recovery_file = recovery.File(self.conf['vmId'])
        self._monitorResponse = 0
        self.memCommitted = 0
        self._consoleDisconnectAction = ConsoleDisconnectAction.LOCK_SCREEN
        self._confLock = threading.Lock()
        self._jobsLock = threading.Lock()
        self._statusLock = threading.Lock()
        self._creationThread = concurrent.thread(self._startUnderlyingVm)
        if 'migrationDest' in self.conf:
            self._lastStatus = vmstatus.MIGRATION_DESTINATION
        elif 'restoreState' in self.conf:
            self._lastStatus = vmstatus.RESTORING_STATE
        else:
            self._lastStatus = vmstatus.WAIT_FOR_LAUNCH
        self._migrationSourceThread = migration.SourceThread(self)
        self._kvmEnable = self.conf.get('kvmEnable', 'true')
        self._incomingMigrationFinished = threading.Event()
        self.id = self.conf['vmId']
        self._volPrepareLock = threading.Lock()
        self._initTimePauseCode = None
        self._initTimeRTC = int(self.conf.get('timeOffset', 0))
        self._guestEvent = vmstatus.POWERING_UP
        self._guestEventTime = 0
        self._guestCpuRunning = False
        self._guestCpuLock = threading.Lock()
        self._startTime = time.time() - \
            float(self.conf.pop('elapsedTimeOffset', 0))

        self._usedIndices = {}  # {'ide': [], 'virtio' = []}
        self.stopDisksStatsCollection()
        self._vmStartEvent = threading.Event()
        self._vmAsyncStartError = None
        self._vmCreationEvent = threading.Event()
        self._pathsPreparedEvent = threading.Event()
        self._devices = self._emptyDevMap()

        self._connection = libvirtconnection.get(cif)
        if 'vmName' not in self.conf:
            self.conf['vmName'] = 'n%s' % self.id
        self._guestSocketFile = self._makeChannelPath(vmchannels.DEVICE_NAME)
        self._qemuguestSocketFile = self._makeChannelPath(
            vmchannels.QEMU_GA_DEVICE_NAME)
        self.guestAgent = guestagent.GuestAgent(
            self._guestSocketFile, self.cif.channelListener, self.log,
            self._onGuestStatusChange,
            self.conf.pop('guestAgentAPIVersion', None))
        self._domain = DomainDescriptor.from_id(self.id)
        self._released = threading.Event()
        self._releaseLock = threading.Lock()
        self._watchdogEvent = {}
        self.arch = cpuarch.effective()
        self._powerDownEvent = threading.Event()
        self._liveMergeCleanupThreads = {}
        self._shutdownLock = threading.Lock()
        self._shutdownReason = None
        self._vcpuLimit = None
        self._vcpuTuneInfo = {}
        self._numaInfo = {}
        self._vmJobs = None
        self._clientPort = ''

    @property
    def start_time(self):
        return self._startTime

    @property
    def domain(self):
        return self._domain

    def _get_lastStatus(self):
        # note that we don't use _statusLock here. One of the reasons is the
        # non-obvious recursive locking in the following flow:
        # _set_lastStatus() -> saveState() -> status() -> _get_lastStatus().
        status = self._lastStatus
        if not self._guestCpuRunning and status in vmstatus.PAUSED_STATES:
            return vmstatus.PAUSED
        return status

    def _set_lastStatus(self, value):
        with self._statusLock:
            if self._lastStatus == vmstatus.DOWN:
                self.log.warning(
                    'trying to set state to %s when already Down',
                    value)
                if value == vmstatus.DOWN:
                    raise DoubleDownError
                else:
                    return
            if value not in VALID_STATES:
                self.log.error('setting state to %s', value)
            if self._lastStatus != value:
                self.saveState()
                self._lastStatus = value

    def send_status_event(self, **kwargs):
        stats = {'status': self._getVmStatus()}
        stats.update(kwargs)
        self._notify('VM_status', stats)

    def _notify(self, operation, params):
        sub_id = '|virt|%s|%s' % (operation, self.id)
        self.cif.notify(sub_id, **{self.id: params})

    def _onGuestStatusChange(self):
        self.send_status_event(**self._getGuestStats())

    def _get_status_time(self):
        """
        Value provided by this method is used to order messages
        containing changed status on the engine side.
        """
        return str(int(utils.monotonic_time() * 1000))

    lastStatus = property(_get_lastStatus, _set_lastStatus)

    def __getNextIndex(self, used):
        for n in xrange(max(used or [0]) + 2):
            if n not in used:
                idx = n
                break
        return str(idx)

    def _normalizeVdsmImg(self, drv):
        drv['reqsize'] = drv.get('reqsize', '0')  # Backward compatible
        if 'device' not in drv:
            drv['device'] = 'disk'

        if drv['device'] == 'disk':
            volsize = self._getVolumeSize(drv['domainID'], drv['poolID'],
                                          drv['imageID'], drv['volumeID'])
            drv['truesize'] = str(volsize.truesize)
            drv['apparentsize'] = str(volsize.apparentsize)
        else:
            drv['truesize'] = 0
            drv['apparentsize'] = 0

    def __legacyDrives(self):
        """
        Backward compatibility for qa scripts that specify direct paths.
        """
        legacies = []
        DEVICE_SPEC = ((0, 'hda'), (1, 'hdb'), (2, 'hdc'), (3, 'hdd'))
        for index, linuxName in DEVICE_SPEC:
            path = self.conf.get(linuxName)
            if path:
                legacies.append({'type': hwclass.DISK,
                                 'device': 'disk', 'path': path,
                                 'iface': 'ide', 'index': index,
                                 'truesize': 0})
        return legacies

    def __removableDrives(self):
        removables = [{
            'type': hwclass.DISK,
            'device': 'cdrom',
            'iface': vmdevices.storage.DEFAULT_INTERFACE_FOR_ARCH[self.arch],
            'path': self.conf.get('cdrom', ''),
            'index': 2,
            'truesize': 0}]
        floppyPath = self.conf.get('floppy')
        if floppyPath:
            removables.append({
                'type': hwclass.DISK,
                'device': 'floppy',
                'path': floppyPath,
                'iface': 'fdc',
                'index': 0,
                'truesize': 0})
        return removables

    def _devMapFromDevSpecMap(self, dev_spec_map):
        dev_map = self._emptyDevMap()

        for dev_type, dev_class in self.DeviceMapping:
            for dev in dev_spec_map[dev_type]:
                dev_map[dev_type].append(dev_class(self.conf, self.log, **dev))

        return dev_map

    def _devSpecMapFromConf(self):
        """
        Return the "devices" section of this Vm's conf.
        If missing, create it according to old API.
        """
        devices = self._emptyDevMap()

        # For BC we need to save previous behaviour for old type parameters.
        # The new/old type parameter will be distinguished
        # by existence/absence of the 'devices' key

        try:
            # while this code is running, Vm is queryable for status(),
            # thus we must fix devices in an atomic way, hence the deep copy
            with self._confLock:
                devConf = utils.picklecopy(self.conf['devices'])
        except KeyError:
            # (very) old Engines do not send device configuration
            devices[hwclass.DISK] = self.getConfDrives()
            devices[hwclass.NIC] = self.getConfNetworkInterfaces()
            devices[hwclass.SOUND] = self.getConfSound()
            devices[hwclass.VIDEO] = self.getConfVideo()
            devices[hwclass.GRAPHICS] = self.getConfGraphics()
            devices[hwclass.CONTROLLER] = self.getConfController()
        else:
            for dev in devConf:
                try:
                    devices[dev['type']].append(dev)
                except KeyError:
                    if 'type' not in dev or dev['type'] != 'channel':
                        self.log.warn("Unknown type found, device: '%s' "
                                      "found", dev)
                    devices[hwclass.GENERAL].append(dev)

            if not devices[hwclass.GRAPHICS]:
                devices[hwclass.GRAPHICS] = self.getConfGraphics()

        self._checkDeviceLimits(devices)

        # Normalize vdsm images
        for drv in devices[hwclass.DISK]:
            if isVdsmImage(drv):
                try:
                    self._normalizeVdsmImg(drv)
                except StorageUnavailableError:
                    # storage unavailable is not fatal on recovery;
                    # the storage subsystem monitors the devices
                    # and will notify when they come up later.
                    if not self.recovering:
                        raise

        self.normalizeDrivesIndices(devices[hwclass.DISK])

        # Preserve old behavior. Since libvirt add a memory balloon device
        # to all guests, we need to specifically request not to add it.
        self._normalizeBalloonDevice(devices[hwclass.BALLOON])

        return devices

    def _normalizeBalloonDevice(self, balloonDevices):
        EMPTY_BALLOON = {'type': hwclass.BALLOON,
                         'device': 'memballoon',
                         'specParams': {
                             'model': 'none'}}

        # Avoid overriding the saved balloon target value on recovery.
        if not self.recovering:
            for dev in balloonDevices:
                dev['target'] = int(self.conf.get('memSize')) * 1024

        if not balloonDevices:
            balloonDevices.append(EMPTY_BALLOON)

    def _checkDeviceLimits(self, devices):
        # libvirt only support one watchdog and one console device
        for device in (hwclass.WATCHDOG, hwclass.CONSOLE):
            if len(devices[device]) > 1:
                raise ValueError("only a single %s device is "
                                 "supported" % device)
        graphDevTypes = set()
        for dev in devices[hwclass.GRAPHICS]:
            if dev.get('device') not in graphDevTypes:
                graphDevTypes.add(dev.get('device'))
            else:
                raise ValueError("only a single graphic device "
                                 "per type is supported")

    def getConfController(self):
        """
        Normalize controller device.
        """
        controllers = []
        # For now we create by default only 'virtio-serial' controller
        controllers.append({'type': hwclass.CONTROLLER,
                            'device': 'virtio-serial'})
        return controllers

    def getConfVideo(self):
        """
        Normalize video device provided by conf.
        """

        DEFAULT_VIDEOS = {cpuarch.X86_64: 'cirrus',
                          cpuarch.PPC64: 'vga',
                          cpuarch.PPC64LE: 'vga'}

        vcards = []
        if self.conf.get('display') == 'vnc':
            devType = DEFAULT_VIDEOS[self.arch]
        elif self.hasSpice:
            devType = 'qxl'
        else:
            devType = None

        if devType:
            # this method is called only in the ancient Engines compatibility
            # path. But ancient Engines do not support headless VMs, as they
            # will not stop sending display data all of sudden.
            monitors = int(self.conf.get('spiceMonitors', '1'))
            vram = '65536' if (monitors <= 2) else '32768'
            for idx in range(monitors):
                vcards.append({'type': hwclass.VIDEO,
                               'specParams': {'vram': vram},
                               'device': devType})

        return vcards

    def getConfGraphics(self):
        """
        Normalize graphics device provided by conf.
        """

        # this method needs to cope both with ancient Engines and with
        # recent Engines unaware of graphic devices.
        if 'display' not in self.conf:
            return []

        return [{
            'type': hwclass.GRAPHICS,
            'device': (
                'spice'
                if self.conf['display'] in ('qxl', 'qxlnc')
                else 'vnc'),
            'specParams': vmdevices.graphics.makeSpecParams(self.conf)
        }]

    def getConfSound(self):
        """
        Normalize sound device provided by conf.
        """
        scards = []
        if self.conf.get('soundDevice'):
            scards.append({'type': hwclass.SOUND,
                           'device': self.conf.get('soundDevice')})

        return scards

    def getConfNetworkInterfaces(self):
        """
        Normalize networks interfaces provided by conf.
        """
        nics = []
        macs = self.conf.get('macAddr', '').split(',')
        models = self.conf.get('nicModel', '').split(',')
        bridges = self.conf.get('bridge', DEFAULT_BRIDGE).split(',')
        if macs == ['']:
            macs = []
        if models == ['']:
            models = []
        if bridges == ['']:
            bridges = []
        if len(models) < len(macs) or len(models) < len(bridges):
            raise ValueError('Bad nic specification')
        if models and not (macs or bridges):
            raise ValueError('Bad nic specification')
        if not macs or not models or not bridges:
            return ''
        macs = macs + [macs[-1]] * (len(models) - len(macs))
        bridges = bridges + [bridges[-1]] * (len(models) - len(bridges))

        for mac, model, bridge in zip(macs, models, bridges):
            if model == 'pv':
                model = 'virtio'
            nics.append({'type': hwclass.NIC,
                         'macAddr': mac,
                         'nicModel': model, 'network': bridge,
                         'device': 'bridge'})
        return nics

    def getConfDrives(self):
        """
        Normalize drives provided by conf.
        """
        # FIXME
        # Will be better to change the self.conf but this implies an API change
        # Remove this when the API parameters will be consistent.
        confDrives = self.conf.get('drives', [])
        if not confDrives:
            confDrives.extend(self.__legacyDrives())
        confDrives.extend(self.__removableDrives())

        for drv in confDrives:
            drv['type'] = hwclass.DISK
            drv['format'] = drv.get('format') or 'raw'
            drv['propagateErrors'] = drv.get('propagateErrors') or 'off'
            drv['readonly'] = False
            drv['shared'] = False
            # FIXME: For BC we have now two identical keys: iface = if
            # Till the day that conf will not returned as a status anymore.
            drv['iface'] = drv.get('iface') or \
                drv.get(
                    'if',
                    vmdevices.storage.DEFAULT_INTERFACE_FOR_ARCH[self.arch])

        return confDrives

    def updateDriveIndex(self, drv):
        if not drv['iface'] in self._usedIndices:
            self._usedIndices[drv['iface']] = []
        drv['index'] = self.__getNextIndex(self._usedIndices[drv['iface']])
        self._usedIndices[drv['iface']].append(int(drv['index']))

    def normalizeDrivesIndices(self, confDrives):
        drives = [(order, drv) for order, drv in enumerate(confDrives)]
        indexed = []
        for order, drv in drives:
            if drv['iface'] not in self._usedIndices:
                self._usedIndices[drv['iface']] = []
            idx = drv.get('index')
            if idx is not None:
                self._usedIndices[drv['iface']].append(int(idx))
                indexed.append(order)

        for order, drv in drives:
            if order not in indexed:
                self.updateDriveIndex(drv)

        return [drv for order, drv in drives]

    def run(self):
        self._creationThread.start()
        self._vmStartEvent.wait()
        if self._vmAsyncStartError:
            return self._vmAsyncStartError

        return response.success(vmList=self.status())

    def memCommit(self):
        """
        Reserve the required memory for this VM.
        """
        memory = int(self.conf['memSize'])
        memory += config.getint('vars', 'guest_ram_overhead')
        self.memCommitted = 2 ** 20 * memory

    def _startUnderlyingVm(self):
        self.log.debug("Start")
        acquired = False
        if 'migrationDest' in self.conf:
            self.log.debug('Acquiring incoming migration semaphore.')
            acquired = migration.incomingMigrations.acquire(blocking=False)
            if not acquired:
                self._vmAsyncStartError = response.error('migrateLimit')
                self._vmStartEvent.set()
                return

        self.saveState()
        self._vmStartEvent.set()
        try:
            self.memCommit()
            with self._ongoingCreations:
                self._vmCreationEvent.set()
                try:
                    self._run()
                except MissingLibvirtDomainError:
                    # we cannot continue without a libvirt domain object,
                    # not even on recovery, to avoid state desync or worse
                    # split-brain scenarios.
                    raise
                except Exception as e:
                    # as above
                    if isinstance(e, libvirt.libvirtError) and \
                            e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                        raise MissingLibvirtDomainError()
                    elif not self.recovering:
                        raise
                    else:
                        self.log.info("Skipping errors on recovery",
                                      exc_info=True)

            if ('migrationDest' in self.conf or 'restoreState' in self.conf) \
                    and self.lastStatus != vmstatus.DOWN:
                self._completeIncomingMigration()

            self.lastStatus = vmstatus.UP
            if self._initTimePauseCode:
                with self._confLock:
                    self.conf['pauseCode'] = self._initTimePauseCode
                if self._initTimePauseCode == 'ENOSPC':
                    self.cont()
            else:
                try:
                    with self._confLock:
                        del self.conf['pauseCode']
                except KeyError:
                    pass

            self.recovering = False
            self.saveState()

            self.send_status_event(**self._getRunningVmStats())

        except MissingLibvirtDomainError as e:
            # we cannot ever deal with this error, not even on recovery.
            self.setDownStatus(
                self.conf.get(
                    'exitCode', ERROR),
                self.conf.get(
                    'exitReason', e.reason),
                self.conf.get(
                    'exitMessage', ''))
            self.recovering = False
        except DestroyedOnStartupError:
            # this could not happen on recovery
            self.setDownStatus(NORMAL, vmexitreason.DESTROYED_ON_STARTUP)
        except MigrationError:
            self.log.exception("Failed to start a migration destination vm")
            self.setDownStatus(ERROR, vmexitreason.MIGRATION_FAILED)
        except Exception as e:
            if self.recovering:
                self.log.info("Skipping errors on recovery", exc_info=True)
            else:
                self.log.exception("The vm start process failed")
                self.setDownStatus(ERROR, vmexitreason.GENERIC_ERROR, str(e))
        finally:
            if acquired:
                self.log.debug('Releasing incoming migration semaphore')
                migration.incomingMigrations.release()

    def incomingMigrationPending(self):
        return 'migrationDest' in self.conf or 'restoreState' in self.conf

    def stopDisksStatsCollection(self):
        self._volumesPrepared = False

    def startDisksStatsCollection(self):
        self._volumesPrepared = True

    def isDisksStatsCollectionEnabled(self):
        return self._volumesPrepared

    def preparePaths(self):
        drives = self._devSpecMapFromConf()[hwclass.DISK]
        self._preparePathsForDrives(drives)

    def _preparePathsForDrives(self, drives):
        for drive in drives:
            with self._volPrepareLock:
                if self._destroy_requested.is_set():
                    # A destroy request has been issued, exit early
                    break
                drive['path'] = self.cif.prepareVolumePath(drive, self.id)

        else:
            # Now we got all the resources we needed
            self.startDisksStatsCollection()

    def _prepareTransientDisks(self, drives):
        for drive in drives:
            self._createTransientDisk(drive)

    def _onQemuDeath(self):
        self.log.info('underlying process disconnected')
        self._dom = virdomain.Disconnected(self.id)
        # Try release VM resources first, if failed stuck in 'Powering Down'
        # state
        result = self.releaseVm()
        if not result['status']['code']:
            with self._shutdownLock:
                reason = self._shutdownReason
            if reason is None:
                self.setDownStatus(ERROR, vmexitreason.LOST_QEMU_CONNECTION)
            else:
                self.setDownStatus(NORMAL, reason)
        self._powerDownEvent.set()

    def _loadCorrectedTimeout(self, base, doubler=20, load=None):
        """
        Return load-corrected base timeout

        :param base: base timeout, when system is idle
        :param doubler: when (with how many running VMs) should base timeout be
                        doubled
        :param load: current load, number of VMs by default
        """
        if load is None:
            load = len(self.cif.vmContainer)
        return base * (doubler + load) / doubler

    def saveState(self):
        self._recovery_file.save(self)

        try:
            self._updateDomainDescriptor()
        except Exception:
            # we do not care if _dom suddenly died now
            pass

    def onReboot(self):
        try:
            self.log.info('reboot event')
            self._startTime = time.time()
            self._guestEventTime = self._startTime
            self._guestEvent = vmstatus.REBOOT_IN_PROGRESS
            self._powerDownEvent.set()
            self.saveState()
            # this always triggers onStatusChange event, which
            # also sends back status event to Engine.
            self.guestAgent.onReboot()
            if self.conf.get('volatileFloppy'):
                self._ejectFloppy()
                self.log.debug('ejected volatileFloppy')
        except Exception:
            self.log.exception("Reboot event failed")

    def onConnect(self, clientIp='', clientPort=''):
        if clientIp:
            with self._confLock:
                self.conf['clientIp'] = clientIp
            self._clientPort = clientPort

    def _timedDesktopLock(self):
        # This is not a definite fix, we're aware that there is still the
        # possibility of a race condition, however this covers more cases
        # than before and a quick gain
        if (not self.conf.get('clientIp', '') and
           not self._destroy_requested.is_set()):
            delay = config.get('vars', 'user_shutdown_timeout')
            timeout = config.getint('vars', 'sys_shutdown_timeout')
            CDA = ConsoleDisconnectAction
            if self._consoleDisconnectAction == CDA.LOCK_SCREEN:
                self.guestAgent.desktopLock()
            elif self._consoleDisconnectAction == CDA.LOGOUT:
                self.guestAgent.desktopLogoff()
            elif self._consoleDisconnectAction == CDA.REBOOT:
                self.shutdown(delay=delay, reboot=True, timeout=timeout,
                              message='Scheduled reboot on disconnect',
                              force=True)
            elif self._consoleDisconnectAction == CDA.SHUTDOWN:
                self.shutdown(delay=delay, reboot=False, timeout=timeout,
                              message='Scheduled shutdown on disconnect',
                              force=True)

    def onDisconnect(self, detail=None, clientIp='', clientPort=''):
        if self.conf['clientIp'] != clientIp:
            self.log.debug('Ignoring disconnect event because ip differs')
            return
        if self._clientPort and self._clientPort != clientPort:
            self.log.debug('Ignoring disconnect event because ports differ')
            return

        self.conf['clientIp'] = ''
        # This is a hack to mitigate the issue of spice-gtk not respecting the
        # configured secure channels. Spice-gtk is always connecting first to
        # a non-secure channel and the server tells the client then to connect
        # to a secure channel. However as a result of this we're getting events
        # of false positive disconnects and we need to ensure that we're really
        # having a disconnected client
        # This timer is supposed to delay the call to lock the desktop of the
        # guest. And only lock it, if it there was no new connect.
        # This is detected by the clientIp being set or not.
        #
        # Multiple desktopLock calls won't matter if we're really disconnected
        # It is not harmful. And the threads will exit after 2 seconds anyway.
        _DESKTOP_LOCK_TIMEOUT = 2
        timer = threading.Timer(_DESKTOP_LOCK_TIMEOUT, self._timedDesktopLock)
        timer.start()

    def onRTCUpdate(self, timeOffset):
        newTimeOffset = str(self._initTimeRTC + int(timeOffset))
        self.log.debug('new rtc offset %s', newTimeOffset)
        with self._confLock:
            self.conf['timeOffset'] = newTimeOffset

    def _getExtendCandidates(self):
        ret = []

        for drive in self._devices[hwclass.DISK]:
            if not (drive.chunked or drive.replicaChunked):
                continue

            try:
                capacity, alloc, physical = self._getExtendInfo(drive)
            except libvirt.libvirtError as e:
                self.log.error("Unable to get watermarks for drive %s: %s",
                               drive.name, e)
                continue

            ret.append((drive, drive.volumeID, capacity, alloc, physical))

        return ret

    def _getExtendInfo(self, drive):
        """
        Return extension info for a chunked drive or drive replicating to
        chunked replica volume.
        """
        capacity, alloc, physical = self._dom.blockInfo(drive.path, 0)

        # Libvirt reports watermarks only for the source drive, but for
        # file-based drives it reports the same alloc and physical, which
        # breaks our extend logic. Since drive is chunked, we must have a
        # disk-based replica, so we get the physical size from the replica.

        if not drive.chunked:
            replica = drive.diskReplicate
            volsize = self._getVolumeSize(replica["domainID"],
                                          replica["poolID"],
                                          replica["imageID"],
                                          replica["volumeID"])
            physical = volsize.apparentsize

        return capacity, alloc, physical

    def _shouldExtendVolume(self, drive, volumeID, capacity, alloc, physical):
        nextPhysSize = drive.getNextVolumeSize(physical, capacity)

        # NOTE: the intent of this check is to prevent faulty images to
        # trick qemu in requesting extremely large extensions (BZ#998443).
        # Probably the definitive check would be comparing the allocated
        # space with capacity + format_overhead. Anyway given that:
        #
        # - format_overhead is tricky to be computed (it depends on few
        #   assumptions that may change in the future e.g. cluster size)
        # - currently we allow only to extend by one chunk at time
        #
        # the current check compares alloc with the next volume size.
        # It should be noted that alloc cannot be directly compared with
        # the volume physical size as it includes also the clusters not
        # written yet (pending).
        if alloc > nextPhysSize:
            msg = ("Improbable extension request for volume %s on domain "
                   "%s, pausing the VM to avoid corruptions (capacity: %s, "
                   "allocated: %s, physical: %s, next physical size: %s)" %
                   (volumeID, drive.domainID, capacity, alloc, physical,
                    nextPhysSize))
            self.log.error(msg)
            self.pause(pauseCode='EOTHER')
            raise ImprobableResizeRequestError(msg)

        if physical >= drive.getMaxVolumeSize(capacity):
            # The volume was extended to the maximum size. physical may be
            # larger than maximum volume size since it is rounded up to the
            # next lvm extent.
            return False

        if physical - alloc < drive.watermarkLimit:
            return True
        return False

    def extendDrivesIfNeeded(self):
        try:
            extend = [x for x in self._getExtendCandidates()
                      if self._shouldExtendVolume(*x)]
        except ImprobableResizeRequestError:
            return False

        for drive, volumeID, capacity, alloc, physical in extend:
            self.log.info(
                "Requesting extension for volume %s on domain %s (apparent: "
                "%s, capacity: %s, allocated: %s, physical: %s)",
                volumeID, drive.domainID, drive.apparentsize, capacity,
                alloc, physical)
            self.extendDriveVolume(drive, volumeID, physical, capacity)

        return len(extend) > 0

    def extendDriveVolume(self, vmDrive, volumeID, curSize, capacity):
        """
        Extend drive volume and its replica volume during replication.

        Must be called only when the drive or its replica are chunked.
        """
        newSize = vmDrive.getNextVolumeSize(curSize, capacity)

        # If drive is replicated to a block device, we extend first the
        # replica, and handle drive later in __afterReplicaExtension.

        if vmDrive.replicaChunked:
            self.__extendDriveReplica(vmDrive, newSize)
        else:
            self.__extendDriveVolume(vmDrive, volumeID, newSize)

    def __refreshDriveVolume(self, volInfo):
        self.cif.irs.refreshVolume(volInfo['domainID'], volInfo['poolID'],
                                   volInfo['imageID'], volInfo['volumeID'])

    def __verifyVolumeExtension(self, volInfo):
        self.log.debug("Refreshing drive volume for %s (domainID: %s, "
                       "volumeID: %s)", volInfo['name'], volInfo['domainID'],
                       volInfo['volumeID'])

        self.__refreshDriveVolume(volInfo)
        volSize = self._getVolumeSize(volInfo['domainID'], volInfo['poolID'],
                                      volInfo['imageID'], volInfo['volumeID'])

        self.log.debug("Verifying extension for volume %s, requested size %s, "
                       "current size %s", volInfo['volumeID'],
                       volInfo['newSize'], volSize.apparentsize)

        if volSize.apparentsize < volInfo['newSize']:
            raise RuntimeError(
                "Volume extension failed for %s (domainID: %s, volumeID: %s)" %
                (volInfo['name'], volInfo['domainID'], volInfo['volumeID']))

        return volSize

    def __afterReplicaExtension(self, volInfo):
        self.__verifyVolumeExtension(volInfo)
        vmDrive = self._findDriveByName(volInfo['name'])
        if vmDrive.chunked:
            self.log.debug("Requesting extension for the original drive: %s "
                           "(domainID: %s, volumeID: %s)",
                           vmDrive.name, vmDrive.domainID, vmDrive.volumeID)
            self.__extendDriveVolume(vmDrive, vmDrive.volumeID,
                                     volInfo['newSize'])

    def __extendDriveVolume(self, vmDrive, volumeID, newSize):
        volInfo = {
            'domainID': vmDrive.domainID,
            'imageID': vmDrive.imageID,
            'internal': vmDrive.volumeID != volumeID,
            'name': vmDrive.name,
            'newSize': newSize,
            'poolID': vmDrive.poolID,
            'volumeID': volumeID,
        }
        self.log.debug("Requesting an extension for the volume: %s", volInfo)
        self.cif.irs.sendExtendMsg(
            vmDrive.poolID,
            volInfo,
            newSize,
            self.__afterVolumeExtension)

    def __extendDriveReplica(self, drive, newSize):
        volInfo = {
            'domainID': drive.diskReplicate['domainID'],
            'imageID': drive.diskReplicate['imageID'],
            'name': drive.name,
            'newSize': newSize,
            'poolID': drive.diskReplicate['poolID'],
            'volumeID': drive.diskReplicate['volumeID'],
        }
        self.log.debug("Requesting an extension for the volume "
                       "replication: %s", volInfo)
        self.cif.irs.sendExtendMsg(drive.poolID,
                                   volInfo,
                                   newSize,
                                   self.__afterReplicaExtension)

    def __afterVolumeExtension(self, volInfo):
        # Check if the extension succeeded.  On failure an exception is raised
        # TODO: Report failure to the engine.
        volSize = self.__verifyVolumeExtension(volInfo)

        # Only update apparentsize and truesize if we've resized the leaf
        if not volInfo['internal']:
            vmDrive = self._findDriveByName(volInfo['name'])
            vmDrive.apparentsize = volSize.apparentsize
            vmDrive.truesize = volSize.truesize

        try:
            self.cont()
        except libvirt.libvirtError:
            self.log.warn("VM %s can't be resumed", self.id, exc_info=True)
        self._setWriteWatermarks()

    def _acquireCpuLockWithTimeout(self):
        timeout = self._loadCorrectedTimeout(
            config.getint('vars', 'vm_command_timeout'))
        end = time.time() + timeout
        while not self._guestCpuLock.acquire(False):
            time.sleep(0.1)
            if time.time() > end:
                raise RuntimeError('waiting more that %ss for _guestCpuLock' %
                                   timeout)

    def cont(self, afterState=vmstatus.UP, guestCpuLocked=False,
             ignoreStatus=False):
        """
        Continue execution of the VM.

        :param ignoreStatus: True, if the operation must be performed
                             regardless of the VM's status, False otherwise.
                             Default: False

                             By default, cont() returns error if the VM is in
                             one of the following states:

                             vmstatus.MIGRATION_SOURCE
                                 Migration is in progress, VM status should not
                                 be changed till the migration finishes.

                             vmstatus.SAVING_STATE
                                 Hibernation is in progress, VM status should
                                 not be changed till the hibernation finishes.

                             vmstatus.DOWN
                                 VM is down, continuing is not possible from
                                 this state.
        """
        if not guestCpuLocked:
            self._acquireCpuLockWithTimeout()
        try:
            if (not ignoreStatus and
                    self.lastStatus in (vmstatus.MIGRATION_SOURCE,
                                        vmstatus.SAVING_STATE,
                                        vmstatus.DOWN)):
                self.log.error('cannot cont while %s', self.lastStatus)
                return response.error('unexpected')
            self._underlyingCont()
            self._setGuestCpuRunning(self._isDomainRunning(),
                                     guestCpuLocked=True)
            self._logGuestCpuStatus('continue')
            self._lastStatus = afterState
            try:
                with self._confLock:
                    del self.conf['pauseCode']
            except KeyError:
                pass
        finally:
            if not guestCpuLocked:
                self._guestCpuLock.release()

        self.send_status_event()
        return response.success()

    def pause(self, afterState=vmstatus.PAUSED, guestCpuLocked=False,
              pauseCode='NOERR'):
        if not guestCpuLocked:
            self._acquireCpuLockWithTimeout()
        try:
            with self._confLock:
                self.conf['pauseCode'] = pauseCode
            self._underlyingPause()
            self._setGuestCpuRunning(self._isDomainRunning(),
                                     guestCpuLocked=True)
            self._logGuestCpuStatus('pause')
            self._lastStatus = afterState
        finally:
            if not guestCpuLocked:
                self._guestCpuLock.release()

        self.send_status_event()
        return response.success()

    def _setGuestCpuRunning(self, isRunning, guestCpuLocked=False):
        """
        here we want to synchronize the access to guestCpuRunning
        made by callback with the pause/cont methods.
        To do so we reuse guestCpuLocked.
        """
        if not guestCpuLocked:
            self._acquireCpuLockWithTimeout()
        try:
            self._guestCpuRunning = isRunning
        finally:
            if not guestCpuLocked:
                self._guestCpuLock.release()

    def _syncGuestTime(self):
        """
        Try to set VM time to the current value.  This is typically useful when
        clock wasn't running on the VM for some time (e.g. during suspension or
        migration), especially if the time delay exceeds NTP tolerance.

        It is not guaranteed that the time is actually set (it depends on guest
        environment, especially QEMU agent presence) or that the set time is
        very precise (NTP in the guest should take care of it if needed).
        """
        t = time.time()
        seconds = int(t)
        nseconds = int((t - seconds) * 10**9)
        try:
            self._dom.setTime(time={'seconds': seconds, 'nseconds': nseconds})
        except libvirt.libvirtError as e:
            template = "Failed to set time: %s"
            code = e.get_error_code()
            if code == libvirt.VIR_ERR_AGENT_UNRESPONSIVE:
                self.log.debug(template, "QEMU agent unresponsive")
            elif code == libvirt.VIR_ERR_NO_SUPPORT:
                self.log.debug(template, "Not supported")
            else:
                self.log.error(template, e)
        except virdomain.NotConnectedError:
            # The highest priority is not to let this method crash and thus
            # disrupt its caller in any way.  So we swallow this error here,
            # to be absolutely safe.
            self.log.debug("Failed to set time: not connected")
        else:
            self.log.debug('Time updated to: %d.%09d', seconds, nseconds)

    def shutdown(self, delay, message, reboot, timeout, force):
        if self.lastStatus == vmstatus.DOWN:
            return response.error('noVM')

        delay = int(delay)

        self._guestEventTime = time.time()
        if reboot:
            self._guestEvent = vmstatus.REBOOT_IN_PROGRESS
            powerDown = VmReboot(self, delay, message, timeout, force,
                                 self._powerDownEvent)
        else:
            self._guestEvent = vmstatus.POWERING_DOWN
            powerDown = VmShutdown(self, delay, message, timeout, force,
                                   self._powerDownEvent)
        return powerDown.start()

    def _cleanupDrives(self, *drives):
        """
        Clean up drives related stuff. Sample usage:

        self._cleanupDrives()
        self._cleanupDrives(drive)
        self._cleanupDrives(drive1, drive2, drive3)
        self._cleanupDrives(*drives_list)
        """
        drives = drives or self._devices[hwclass.DISK]
        # clean them up
        with self._volPrepareLock:
            for drive in drives:
                try:
                    self._removeTransientDisk(drive)
                except Exception:
                    self.log.warning("Drive transient volume deletion failed "
                                     "for drive %s", drive, exc_info=True)
                    # Skip any exception as we don't want to interrupt the
                    # teardown process for any reason.
                try:
                    self.cif.teardownVolumePath(drive)
                except Exception:
                    self.log.exception("Drive teardown failure for %s",
                                       drive)

    def _cleanupFloppy(self):
        """
        Clean up floppy drive
        """
        if self.conf.get('volatileFloppy'):
            try:
                self.log.debug("Floppy %s cleanup" % self.conf['floppy'])
                utils.rmFile(self.conf['floppy'])
            except Exception:
                pass

    def _cleanupGuestAgent(self):
        """
        Try to stop the guest agent and clean up its socket
        """
        try:
            self.guestAgent.stop()
        except Exception:
            pass

        cleanup_guest_socket(self._guestSocketFile)

    def _reattachHostDevices(self):
        # reattach host devices
        for dev_name, _ in self._host_devices():
            self.log.debug('Reattaching device %s to host.' % dev_name)
            try:
                hostdev.reattach_detachable(dev_name)
            except hostdev.NoIOMMUSupportException:
                self.log.exception('Could not reattach device %s back to host '
                                   'due to missing IOMMU support.')

    def _host_devices(self):
        for device in self._devices[hwclass.NIC][:]:
            if device.is_hostdevice:
                yield device.hostdev, device

    def setDownStatus(self, code, exitReasonCode, exitMessage=''):
        if not exitMessage:
            exitMessage = vmexitreason.exitReasons.get(exitReasonCode,
                                                       'VM terminated')
        event_data = {}
        try:
            self.lastStatus = vmstatus.DOWN
            with self._confLock:
                self.conf['exitCode'] = code
                if 'restoreState' in self.conf:
                    self.conf['exitMessage'] = (
                        "Wake up from hibernation failed" +
                        ((":" + exitMessage) if exitMessage else ''))
                else:
                    self.conf['exitMessage'] = exitMessage
                self.conf['exitReason'] = exitReasonCode
            self.log.info("Changed state to Down: %s (code=%i)",
                          exitMessage, exitReasonCode)
            # Engine doesn't like duplicated events (e.g. Down, Down).
            # but this cannot happen in this flow, because
            # if some flows tries to setDownStatus a VM already Down,
            # it will explode with DoubleDownError, thus this code
            # will never reach this point and no event will be emitted.
            event_data = self._getExitedVmStats()
        except DoubleDownError:
            pass
        try:
            self.guestAgent.stop()
        except Exception:
            pass
        self.saveState()
        if event_data:
            self.send_status_event(**event_data)

    def status(self, fullStatus=True):
        # used by API.Global.getVMList
        if not fullStatus:
            return {'vmId': self.id, 'status': self.lastStatus,
                    'statusTime': self._get_status_time()}

        with self._confLock:
            self.conf['status'] = self.lastStatus
            # Filter out any internal keys
            status = dict((k, v) for k, v in self.conf.iteritems()
                          if not k.startswith("_"))
            status['guestDiskMapping'] = self.guestAgent.guestDiskMapping
            status['statusTime'] = self._get_status_time()
            return utils.picklecopy(status)

    def getStats(self):
        """
        used by API.Vm.getStats

        WARNING: this method should only gather statistics by copying data.
        Especially avoid costly and dangerous ditrect calls to the _dom
        attribute. Use the periodic operations instead!
        """

        stats = {'statusTime': self._get_status_time()}
        if self.lastStatus == vmstatus.DOWN:
            stats.update(self._getDownVmStats())
        else:
            stats.update(self._getConfigVmStats())
            stats.update(self._getRunningVmStats())
            stats['status'] = self._getVmStatus()
            stats.update(self._getGuestStats())
        return stats

    def _getDownVmStats(self):
        stats = {
            'vmId': self.conf['vmId'],
            'status': self.lastStatus
        }
        stats.update(self._getExitedVmStats())
        return stats

    def _getExitedVmStats(self):
        stats = {
            'exitCode': self.conf['exitCode'],
            'exitMessage': self.conf['exitMessage'],
            'exitReason': self.conf['exitReason']}
        if 'timeOffset' in self.conf:
            stats['timeOffset'] = self.conf['timeOffset']
        return stats

    def _getConfigVmStats(self):
        """
        provides all the stats which will not change after a VM is booted.
        Please note that some values are provided by client (engine)
        but can change as result as interaction with libvirt (display*)
        """
        stats = {
            'vmId': self.conf['vmId'],
            'vmName': self.name,
            'pid': self.conf['pid'],
            'vmType': self.conf['vmType'],
            'kvmEnable': self._kvmEnable,
            'acpiEnable': self.conf.get('acpiEnable', 'true')}
        if 'cdrom' in self.conf:
            stats['cdrom'] = self.conf['cdrom']
        if 'boot' in self.conf:
            stats['boot'] = self.conf['boot']
        return stats

    def _getRunningVmStats(self):
        """
        gathers all the stats which can change while a VM is running.
        """
        stats = {
            'elapsedTime': str(int(time.time() - self._startTime)),
            'monitorResponse': str(self._monitorResponse),
            'timeOffset': self.conf.get('timeOffset', '0'),
            'clientIp': self.conf.get('clientIp', ''),
        }

        with self._confLock:
            if 'pauseCode' in self.conf:
                stats['pauseCode'] = self.conf['pauseCode']
        if self.isMigrating():
            stats['migrationProgress'] = self.migrateStatus()['progress']

        try:
            vm_sample = sampling.stats_cache.get(self.id)
            decStats = vmstats.produce(self,
                                       vm_sample.first_value,
                                       vm_sample.last_value,
                                       vm_sample.interval)
            self._setUnresponsiveIfTimeout(stats, vm_sample.stats_age)
        except Exception:
            self.log.exception("Error fetching vm stats")
        else:
            stats.update(vmstats.translate(decStats))

        stats.update(self._getGraphicsStats())
        stats['hash'] = str(hash((self._domain.devices_hash,
                                  self.guestAgent.diskMappingHash)))
        if self._watchdogEvent:
            stats['watchdogEvent'] = self._watchdogEvent
        if self._numaInfo:
            stats['vNodeRuntimeInfo'] = self._numaInfo
        if self._vcpuLimit:
            stats['vcpuUserLimit'] = self._vcpuLimit

        stats.update(self._getVmJobsStats())

        stats.update(self._getVmTuneStats())
        return stats

    def _getVmTuneStats(self):
        stats = {}

        # Handling the case where quota is not set, setting to 0.
        # According to libvirt API:"A quota with value 0 means no value."
        # The value does not have to be present in some transient cases
        vcpu_quota = self._vcpuTuneInfo.get('vcpu_quota', _NO_CPU_QUOTA)
        if vcpu_quota != _NO_CPU_QUOTA:
            stats['vcpuQuota'] = str(vcpu_quota)

        # Handling the case where period is not set, setting to 0.
        # According to libvirt API:"A period with value 0 means no value."
        # The value does not have to be present in some transient cases
        vcpu_period = self._vcpuTuneInfo.get('vcpu_period', _NO_CPU_PERIOD)
        if vcpu_period != _NO_CPU_PERIOD:
            stats['vcpuPeriod'] = vcpu_period

        return stats

    def _getVmJobsStats(self):
        stats = {}

        # vmJobs = {} is a valid output and should be reported.
        # means 'jobs finishing' to Engine.
        #
        # default value for self._vmJobs is None, this means
        # "VDSM does not know yet", thus should not report anything to Engine.
        # Once Vm.updateVmJobs run at least once, VDSM will know for sure.
        if self._vmJobs is not None:
            stats['vmJobs'] = self._vmJobs

        return stats

    def _getVmStatus(self):
        def _getVmStatusFromGuest():
            GUEST_WAIT_TIMEOUT = 60
            now = time.time()
            if now - self._guestEventTime < 5 * GUEST_WAIT_TIMEOUT and \
                    self._guestEvent == vmstatus.POWERING_DOWN:
                return self._guestEvent
            if self.guestAgent and self.guestAgent.isResponsive() and \
                    self.guestAgent.getStatus():
                return self.guestAgent.getStatus()
            if now - self._guestEventTime < GUEST_WAIT_TIMEOUT:
                return self._guestEvent
            return vmstatus.UP

        statuses = (vmstatus.SAVING_STATE, vmstatus.RESTORING_STATE,
                    vmstatus.MIGRATION_SOURCE, vmstatus.MIGRATION_DESTINATION,
                    vmstatus.PAUSED, vmstatus.DOWN)
        if self.lastStatus in statuses:
            return self.lastStatus
        elif self.isMigrating():
            if self._migrationSourceThread.hibernating:
                return vmstatus.SAVING_STATE
            else:
                return vmstatus.MIGRATION_SOURCE
        elif self.lastStatus == vmstatus.UP:
            return _getVmStatusFromGuest()
        else:
            return self.lastStatus

    def _getGraphicsStats(self):
        def getInfo(dev):
            return {
                'type': dev.device,
                'port': dev.port,
                'tlsPort': dev.tlsPort,
                'ipAddress': dev.specParams.get('displayIp', '0'),
            }

        stats = {
            'displayInfo': [getInfo(dev)
                            for dev in self._devices[hwclass.GRAPHICS]],
        }
        if 'display' in self.conf:
            stats['displayType'] = self.conf['display']
            stats['displayPort'] = self.conf['displayPort']
            stats['displaySecurePort'] = self.conf['displaySecurePort']
            stats['displayIp'] = self.conf['displayIp']
        # else headless VM
        return stats

    def _getGuestStats(self):
        stats = self.guestAgent.getGuestInfo()
        realMemUsage = int(stats['memUsage'])
        if realMemUsage != 0:
            memUsage = (100 - float(realMemUsage) /
                        int(self.conf['memSize']) * 100)
        else:
            memUsage = 0
        stats['memUsage'] = utils.convertToStr(int(memUsage))
        return stats

    def isMigrating(self):
        return self._migrationSourceThread.isAlive()

    def hasTransientDisks(self):
        for drive in self._devices[hwclass.DISK]:
            if drive.transientDisk:
                return True
        return False

    def migrate(self, params):
        self._acquireCpuLockWithTimeout()
        try:
            if self.isMigrating():
                self.log.warning('vm already migrating')
                return response.error('exist')
            if self.hasTransientDisks():
                return response.error('transientErr')
            # while we were blocking, another migrationSourceThread could have
            # taken self Down
            if self._lastStatus == vmstatus.DOWN:
                return response.error('noVM')
            self._migrationSourceThread = migration.SourceThread(
                self, **params)
            self._migrationSourceThread.start()
            self._migrationSourceThread.getStat()
            self.send_status_event()
            return self._migrationSourceThread.status
        finally:
            self._guestCpuLock.release()

    def migrateStatus(self):
        return self._migrationSourceThread.getStat()

    def migrateCancel(self):
        self._acquireCpuLockWithTimeout()
        try:
            self._migrationSourceThread.stop()
            self._migrationSourceThread.status['status']['message'] = \
                'Migration process cancelled'
            return self._migrationSourceThread.status
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                return response.error('migCancelErr')
            raise
        except virdomain.NotConnectedError:
            return response.error('migCancelErr')
        finally:
            self._guestCpuLock.release()

    def _getSerialConsole(self):
        """
        Return serial console device.
        If no serial console device is available, return 'None'.
        """
        for console in self._devices[hwclass.CONSOLE]:
            if console.isSerial:
                return console
        return None

    def migrateChangeParams(self, params):
        self._acquireCpuLockWithTimeout()

        try:
            if self._migrationSourceThread.hibernating:
                return response.error('migNotInProgress')

            if not self.isMigrating():
                return response.error('migNotInProgress')

            if 'maxBandwidth' in params:
                self._migrationSourceThread.set_max_bandwidth(
                    int(params['maxBandwidth']))
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                return response.error('migNotInProgress')
            raise
        except virdomain.NotConnectedError:
            return response.error('migNotInProgress')

        finally:
            self._guestCpuLock.release()

        return response.success()

    def _customDevices(self):
        """
            Get all devices that have custom properties
        """

        for devType in self._devices:
            for dev in self._devices[devType]:
                if dev.custom:
                    yield dev

    def _appendDevices(self, domxml):
        """
        Create all devices and run before_device_create hook script for devices
        with custom properties

        The resulting device xml is cached in dev._deviceXML.
        """

        for devType in self._devices:
            for dev in self._devices[devType]:
                try:
                    deviceXML = dev.getXML().toxml(encoding='utf-8')
                except vmdevices.core.SkipDevice:
                    self.log.info('Skipping device %s.', dev.device)
                    continue

                if getattr(dev, "custom", {}):
                    deviceXML = hooks.before_device_create(
                        deviceXML, self.conf, dev.custom)

                dev._deviceXML = deviceXML
                domxml.appendDeviceXML(deviceXML)

    def _buildDomainXML(self):
        serial_console = self._getSerialConsole()

        domxml = vmxml.Domain(self.conf, self.log, self.arch)
        domxml.appendOs(use_serial_console=(serial_console is not None))

        if cpuarch.is_x86(self.arch):
            osd = osinfo.version()

            osVersion = osd.get('version', '') + '-' + osd.get('release', '')
            serialNumber = self.conf.get('serial', host.uuid())

            domxml.appendSysinfo(
                osname=constants.SMBIOS_OSNAME,
                osversion=osVersion,
                serialNumber=serialNumber)

        domxml.appendClock()

        if cpuarch.is_x86(self.arch):
            domxml.appendFeatures()

        domxml.appendCpu()

        domxml.appendNumaTune()

        domxml._appendAgentDevice(self._guestSocketFile.decode('utf-8'),
                                  vmchannels.DEVICE_NAME)
        domxml._appendAgentDevice(self._qemuguestSocketFile.decode('utf-8'),
                                  vmchannels.QEMU_GA_DEVICE_NAME)
        domxml.appendInput()

        if self.arch == cpuarch.PPC64:
            domxml.appendEmulator()

        self._appendDevices(domxml)

        for graphDev in self._devices[hwclass.GRAPHICS]:
            if graphDev.device == 'spice':
                domxml._devices.appendChild(graphDev.getSpiceVmcChannelsXML())
                break

        if serial_console is not None:
            domxml._devices.appendChild(serial_console.getSerialDeviceXML())

        for drive in self._devices[hwclass.DISK][:]:
            for leaseElement in drive.getLeasesXML():
                domxml._devices.appendChild(leaseElement)

        return domxml.toxml()

    def _cleanup(self):
        """
        General clean up routine
        """
        self._cleanupDrives()
        self._cleanupFloppy()
        self._cleanupGuestAgent()
        self._teardown_devices()
        cleanup_guest_socket(self._qemuguestSocketFile)
        # TODO: avoid reattach when Engine can tell free VFs otherwise
        self._reattachHostDevices()
        self._cleanupStatsCache()
        numa.invalidateNumaCache(self)
        for con in self._devices[hwclass.CONSOLE]:
            con.cleanup()

    def _teardown_devices(self, devices=None):
        """
        Runs after the underlying libvirt domain was destroyed.
        """
        if devices is None:
            devices = list(itertools.chain(*self._devices.values()))

        for device in devices:
            try:
                device.teardown()
            except Exception:
                self.log.exception('Failed to tear down device %s, device in '
                                   'inconsistent state', device.device)

    def _cleanupRecoveryFile(self):
        self._recovery_file.cleanup()

    def _cleanupStatsCache(self):
        try:
            sampling.stats_cache.remove(self.id)
        except KeyError:
            self.log.warn('timestamp already removed from stats cache')

    def _isDomainRunning(self):
        try:
            status = self._dom.info()
        except virdomain.NotConnectedError:
            # Known reasons for this:
            # * on migration destination, and migration not yet completed.
            # * self._dom may be disconnected asynchronously (_onQemuDeath).
            #   If so, the VM is shutting down or already shut down.
            return False
        else:
            return status[0] == libvirt.VIR_DOMAIN_RUNNING

    def _getUnderlyingVmDevicesInfo(self):
        """
        Obtain underlying vm's devices info from libvirt.
        """
        vmdevices.common.update_device_info(self, self._devices)

    def _updateAgentChannels(self):
        """
        We moved the naming of guest agent channel sockets. To keep backwards
        compatability we need to make symlinks from the old channel sockets, to
        the new naming scheme.
        This is necessary to prevent incoming migrations, restoring of VMs and
        the upgrade of VDSM with running VMs to fail on this.
        """
        for name, path in self._domain.all_channels():
            if name not in vmchannels.AGENT_DEVICE_NAMES:
                continue

            uuidPath = self._makeChannelPath(name)
            if path != uuidPath:
                # When this path is executed, we're having VM created on
                # VDSM > 4.13

                # The to be created symlink might not have been cleaned up due
                # to an unexpected stop of VDSM therefore We're going to clean
                # it up now
                if os.path.islink(uuidPath):
                    os.unlink(uuidPath)

                # We don't want an exception to be thrown when the path already
                # exists
                if not os.path.exists(uuidPath):
                    os.symlink(path, uuidPath)
                else:
                    self.log.error("Failed to make a agent channel symlink "
                                   "from %s -> %s for channel %s", path,
                                   uuidPath, name)

    def _domDependentInit(self):
        if self._destroy_requested.is_set():
            # reaching here means that Vm.destroy() was called before we could
            # handle it. We must handle it now
            try:
                self._dom.destroy()
            except Exception:
                pass
            raise DestroyedOnStartupError()

        if not self._dom.connected:
            raise MissingLibvirtDomainError(vmexitreason.LIBVIRT_START_FAILED)

        self._updateDomainDescriptor()

        # REQUIRED_FOR migrate from vdsm-4.16
        #
        # We need to clean out unknown devices that are created for
        # RNG devices by VDSM 3.5 and are left in the configuration
        # after upgrade to 3.6.
        self._fixLegacyRngConf()

        self._getUnderlyingVmDevicesInfo()
        self._updateAgentChannels()

        # Currently there is no protection agains mirroring a network twice,
        if not self.recovering:
            for nic in self._devices[hwclass.NIC]:
                if hasattr(nic, 'portMirroring'):
                    for network in nic.portMirroring:
                        supervdsm.getProxy().setPortMirroring(network,
                                                              nic.name)

        self._guestEventTime = self._startTime
        sampling.stats_cache.add(self.id)
        try:
            self.guestAgent.start()
        except Exception:
            self.log.exception("Failed to connect to guest agent channel")

        try:
            if self.conf.get('enableGuestEvents', False):
                if self.lastStatus == vmstatus.MIGRATION_DESTINATION:
                    self.guestAgent.events.after_migration()
                elif self.lastStatus == vmstatus.RESTORING_STATE:
                    self.guestAgent.events.after_hibernation()
        except Exception:
            self.log.exception("Unexpected error on guest event notification")

        # Drop enableGuestEvents from conf - Not required from here anymore
        self.conf.pop('enableGuestEvents', None)

        for con in self._devices[hwclass.CONSOLE]:
            con.prepare()

        self._guestCpuRunning = self._isDomainRunning()
        self._logGuestCpuStatus('domain initialization')
        if self.lastStatus not in (vmstatus.MIGRATION_DESTINATION,
                                   vmstatus.RESTORING_STATE):
            self._initTimePauseCode = self._readPauseCode()
        if not self.recovering and self._initTimePauseCode:
            with self._confLock:
                self.conf['pauseCode'] = self._initTimePauseCode
            if self._initTimePauseCode == 'ENOSPC':
                self.cont()

        if not self.recovering or 'pid' not in self.conf:
            with self._confLock:
                self.conf['pid'] = str(self._getPid())

        nice = int(self.conf.get('nice', '0'))
        nice = max(min(nice, 19), 0)

        # if cpuShares weren't configured we derive the value from the
        # niceness, cpuShares has no unit, it is only meaningful when
        # compared to other VMs (and can't be negative)
        cpuShares = int(self.conf.get('cpuShares', str((20 - nice) * 51)))
        cpuShares = max(cpuShares, 0)

        try:
            self._dom.setSchedulerParameters({'cpu_shares': cpuShares})
        except Exception:
            self.log.warning('failed to set Vm niceness', exc_info=True)

        self._updateVcpuTuneInfo()
        self._updateVcpuLimit()

    def _setup_devices(self):
        """
        Runs before the underlying libvirt domain is created.

        Handle setup of all devices. If some device cannot be setup,
        go through the devices that were successfully setup and tear
        them down, logging all exceptions we encounter. Exception is then
        raised as we cannot continue the VM creation due to device failures.
        """
        done = []

        for dev_objects in self._devices.values():
            for dev_object in dev_objects[:]:
                try:
                    dev_object.setup()
                except Exception:
                    self.log.exception("Failed to setup device %s",
                                       dev_object.device)
                    self._teardown_devices(done)
                    raise
                else:
                    done.append(dev_object)

    def _run(self):
        self.log.info("VM wrapper has started")
        dev_spec_map = self._devSpecMapFromConf()

        # recovery flow note:
        # we do not start disk stats collection here since
        # in the recovery flow irs may not be ready yet.
        # Disk stats collection is started from clientIF at the end
        # of the recovery process.
        if not self.recovering:
            self._preparePathsForDrives(dev_spec_map[hwclass.DISK])
            self._prepareTransientDisks(dev_spec_map[hwclass.DISK])
            self._updateDevices(dev_spec_map)
            # We need to save conf here before we actually run VM.
            # It's not enough to save conf only on status changes as we did
            # before, because if vdsm will restarted between VM run and conf
            # saving we will fail in inconsistent state during recovery.
            # So, to get proper device objects during VM recovery flow
            # we must to have updated conf before VM run
            self.saveState()
        else:
            # we need to fix the graphics device configuration in the
            # case VDSM is upgraded from 3.4 to 3.5 on the host without
            # rebooting it. Evident on, but not limited to, the HE case.
            self._fixLegacyGraphicsConf()

        self._devices = self._devMapFromDevSpecMap(dev_spec_map)

        # We should set this event as a last part of drives initialization
        self._pathsPreparedEvent.set()

        initDomain = 'migrationDest' not in self.conf
        # we need to complete the initialization, including
        # domDependentInit, after the migration is completed.

        if not self.recovering:
            self._setup_devices()

        if self.recovering:
            self._dom = virdomain.Notifying(
                self._connection.lookupByUUIDString(self.id),
                self._timeoutExperienced)
        elif 'migrationDest' in self.conf:
            pass  # self._dom will be disconnected until migration ends.
        elif 'restoreState' in self.conf:
            # TODO: for unknown historical reasons, we call this hook also
            # on this flow. Issues:
            # - we will also call the more specific before_vm_dehibernate
            # - we feed the hook with wrong XML
            # - we ignore the output of the hook
            hooks.before_vm_start(self._buildDomainXML(), self.conf)

            fromSnapshot = self.conf.get('restoreFromSnapshot', False)
            with self._confLock:
                srcDomXML = self.conf.pop('_srcDomXML')
            if fromSnapshot:
                srcDomXML = self._correctDiskVolumes(srcDomXML)
                srcDomXML = self._correctGraphicsConfiguration(srcDomXML)
            hooks.before_vm_dehibernate(srcDomXML, self.conf,
                                        {'FROM_SNAPSHOT': str(fromSnapshot)})

            # TODO: this is debug information. For 3.6.x we still need to
            # see the XML even with 'info' as default level.
            self.log.info(srcDomXML)

            fname = self.cif.prepareVolumePath(self.conf['restoreState'])
            try:
                if fromSnapshot:
                    self._connection.restoreFlags(fname, srcDomXML, 0)
                else:
                    self._connection.restore(fname)
            finally:
                self.cif.teardownVolumePath(self.conf['restoreState'])

            self._dom = virdomain.Notifying(
                self._connection.lookupByUUIDString(self.id),
                self._timeoutExperienced)
        else:

            flags = libvirt.VIR_DOMAIN_NONE
            with self._confLock:
                if 'launchPaused' in self.conf:
                    flags |= libvirt.VIR_DOMAIN_START_PAUSED
                    self.conf['pauseCode'] = 'NOERR'
                    del self.conf['launchPaused']
            hooks.dump_vm_launch_flags_to_file(self.id, flags)
            try:
                domxml = hooks.before_vm_start(self._buildDomainXML(),
                                               self.conf)
                flags = hooks.load_vm_launch_flags_from_file(self.id)

                # TODO: this is debug information. For 3.6.x we still need to
                # see the XML even with 'info' as default level.
                self.log.info(domxml)

                self._dom = virdomain.Notifying(
                    self._connection.createXML(domxml, flags),
                    self._timeoutExperienced)
                hooks.after_vm_start(self._dom.XMLDesc(0), self.conf)
                for dev in self._customDevices():
                    hooks.after_device_create(dev._deviceXML, self.conf,
                                              dev.custom)
            finally:
                hooks.remove_vm_launch_flags_file(self.id)

        if initDomain:
            self._domDependentInit()

    def _updateDevices(self, devices):
        """
        Update self.conf with updated devices
        For old type vmParams, new 'devices' key will be
        created with all devices info
        """
        newDevices = []
        for dev in devices.values():
            newDevices.extend(dev)

        with self._confLock:
            self.conf['devices'] = newDevices

    def _correctDiskVolumes(self, srcDomXML):
        """
        Replace each volume in the given XML with the latest volume
        that the image has.
        Each image has a newer volume than the one that appears in the
        XML, which was the latest volume of the image at the time the
        snapshot was taken, since we create new volume when we preview
        or revert to snapshot.
        """
        parsedSrcDomXML = _domParseStr(srcDomXML)

        allDiskDeviceXmlElements = parsedSrcDomXML.childNodes[0]. \
            getElementsByTagName('devices')[0].getElementsByTagName('disk')

        snappableDiskDeviceXmlElements = \
            _filterSnappableDiskDevices(allDiskDeviceXmlElements)

        for snappableDiskDeviceXmlElement in snappableDiskDeviceXmlElements:
            self._changeDisk(snappableDiskDeviceXmlElement)

        return parsedSrcDomXML.toxml()

    def _correctGraphicsConfiguration(self, domXML):
        """
        Fix the configuration of graphics device after resume.
        Make sure the ticketing settings are right
        """

        domObj = ET.fromstring(domXML)
        for devXml in domObj.findall('.//devices/graphics'):
            try:
                devObj = self._lookupDeviceByIdentification(
                    hwclass.GRAPHICS, devXml.get('type'))
            except LookupError:
                self.log.warning('configuration mismatch: graphics device '
                                 'type %s found in domain XML, but not among '
                                 'VM devices' % devXml.get('type'))
            else:
                devObj.setupPassword(devXml)
        return ET.tostring(domObj)

    def _changeDisk(self, diskDeviceXmlElement):
        diskType = diskDeviceXmlElement.getAttribute('type')

        if diskType not in ['file', 'block']:
            return

        diskSerial = diskDeviceXmlElement. \
            getElementsByTagName('serial')[0].childNodes[0].nodeValue

        for vmDrive in self._devices[hwclass.DISK]:
            if vmDrive.serial == diskSerial:
                # update the type
                diskDeviceXmlElement.setAttribute(
                    'type', 'block' if vmDrive.blockDev else 'file')

                # update the path
                diskDeviceXmlElement.getElementsByTagName('source')[0]. \
                    setAttribute('dev' if vmDrive.blockDev else 'file',
                                 vmDrive.path)

                # update the format (the disk might have been collapsed)
                diskDeviceXmlElement.getElementsByTagName('driver')[0]. \
                    setAttribute('type',
                                 'qcow2' if vmDrive.format == 'cow' else 'raw')

                break

    def hotplugNic(self, params):
        if self.isMigrating():
            return response.error('migInProgress')

        nicParams = params['nic']
        nic = vmdevices.network.Interface(self.conf, self.log, **nicParams)
        nicXml = nic.getXML().toprettyxml(encoding='utf-8')
        nicXml = hooks.before_nic_hotplug(nicXml, self.conf,
                                          params=nic.custom)
        nic._deviceXML = nicXml
        # TODO: this is debug information. For 3.6.x we still need to
        # see the XML even with 'info' as default level.
        self.log.info("Hotplug NIC xml: %s", nicXml)

        try:
            if nic.is_hostdevice:
                hostdev.detach_detachable(nicParams[hwclass.HOSTDEV])
            self._dom.attachDevice(nicXml)
        except libvirt.libvirtError as e:
            self.log.exception("Hotplug failed")
            nicXml = hooks.after_nic_hotplug_fail(
                nicXml, self.conf, params=nic.custom)
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return response.error('noVM')
            return response.error('hotplugNic', e.message)
        else:
            # FIXME!  We may have a problem here if vdsm dies right after
            # we sent command to libvirt and before save conf. In this case
            # we will gather almost all needed info about this NIC from
            # the libvirt during recovery process.
            device_conf = self._devices[hwclass.NIC]
            device_conf.append(nic)
            with self._confLock:
                self.conf['devices'].append(nicParams)
            self.saveState()
            vmdevices.network.Interface.update_device_info(self, device_conf)
            hooks.after_nic_hotplug(nicXml, self.conf,
                                    params=nic.custom)

        if hasattr(nic, 'portMirroring'):
            mirroredNetworks = []
            try:
                for network in nic.portMirroring:
                    supervdsm.getProxy().setPortMirroring(network, nic.name)
                    mirroredNetworks.append(network)
            # The better way would be catch the proper exception.
            # One of such exceptions is TrafficControlException, but
            # I am not sure that we'll get it for all traffic control errors.
            # In any case we need below rollback for all kind of failures.
            except Exception as e:
                self.log.exception("setPortMirroring for network %s failed",
                                   network)
                nicParams['portMirroring'] = mirroredNetworks
                self.hotunplugNic({'nic': nicParams})
                return response.error('hotplugNic', e.message)

        return {'status': doneCode, 'vmList': self.status()}

    def _lookupDeviceByIdentification(self, devType, devIdent):
        for dev in self._devices[devType][:]:
            try:
                if dev.device == devIdent:
                    return dev
            except AttributeError:
                continue
        raise LookupError('Device object for device identified as %s '
                          'of type %s not found' % (devIdent, devType))

    def hostdevHotplug(self, dev_specs):
        if self.isMigrating():
            return response.error('migInProgress')

        dev_objects = []
        for dev_spec in dev_specs:
            dev_object = vmdevices.hostdevice.HostDevice(self.conf, self.log,
                                                         **dev_spec)
            dev_objects.append(dev_object)
            try:
                dev_object.setup()
            except libvirt.libvirtError:
                # We couldn't detach one of the devices. Halt.
                self.log.exception('Could not detach a device from a host.')
                return response.error('hostdevDetachErr')

        assigned_devices = []

        # Hard part is done, we have detached all devices without errors.
        # We now have to add devices to the VM while ignoring placeholders.
        for dev_spec, dev_object in zip(dev_specs, dev_objects):
            try:
                dev_xml = dev_object.getXML().toprettyxml(encoding='utf-8')
            except vmdevices.core.SkipDevice:
                self.log.info('Skipping device %s.', dev_object.device)
                continue

            dev_object._deviceXML = dev_xml
            self.log.info("Hotplug hostdev xml: %s", dev_xml)

            try:
                self._dom.attachDevice(dev_xml)
            except libvirt.libvirtError:
                self.log.exception('Skipping device %s.', dev_object.device)
                continue

            assigned_devices.append(dev_object.device)

            self._devices[hwclass.HOSTDEV].append(dev_object)

            with self._confLock:
                self.conf['devices'].append(dev_spec)
            self.saveState()
            vmdevices.hostdevice.HostDevice.update_device_info(
                self, self._devices[hwclass.HOSTDEV])

        return response.success(assignedDevices=assigned_devices)

    def hostdevHotunplug(self, dev_names):
        if self.isMigrating():
            return response.error('migInProgress')

        device_objects = []
        unplugged_devices = []

        for dev_name in dev_names:
            dev_object = None
            for dev in self._devices[hwclass.HOSTDEV][:]:
                if dev.device == dev_name:
                    dev_object = dev
                    device_objects.append(dev)
                    break

            if dev_object:
                device_xml = dev_object.getXML().toprettyxml(encoding='utf-8')
                self.log.debug('Hotunplug hostdev xml: %s', device_xml)
            else:
                self.log.error('Hotunplug hostdev failed (continuing) - '
                               'device not found: %s', dev_name)
                continue

            self._devices[hwclass.HOSTDEV].remove(dev_object)
            dev_spec = None
            for dev in self.conf['devices'][:]:
                if (dev['type'] == hwclass.HOSTDEV and
                        dev['device'] == dev_object.device):
                    dev_spec = dev
                    with self._confLock:
                        self.conf['devices'].remove(dev)
                    break

            self.saveState()

            try:
                self._dom.detachDevice(device_xml)
                self._waitForDeviceRemoval(dev_object)
            except HotunplugTimeout as e:
                self.log.error('%s', e)
                self._hostdev_hotunplug_restore(dev_object, dev_spec)
                continue
            except libvirt.libvirtError as e:
                self.log.exception('Hotunplug failed (continuing)')
                self._hostdev_hotunplug_restore(dev_object, dev_spec)
                continue

            unplugged_devices.append(dev_name)

        return response.success(unpluggedDevices=unplugged_devices)

    def _hostdev_hotunplug_restore(self, dev_object, dev_spec):
        with self._confLock:
            self.conf['devices'].append(dev_spec)
        self._devices[hwclass.HOSTDEV].append(dev_object)
        self.saveState()

    def _lookupDeviceByAlias(self, devType, alias):
        for dev in self._devices[devType][:]:
            try:
                if dev.alias == alias:
                    return dev
            except AttributeError:
                continue
        raise LookupError('Device instance for device identified by alias %s '
                          'not found' % alias)

    def _lookupConfByAlias(self, alias):
        for devConf in self.conf['devices'][:]:
            if devConf['type'] == hwclass.NIC and \
                    devConf['alias'] == alias:
                return devConf
        raise LookupError('Configuration of device identified by alias %s not'
                          'found' % alias)

    def _lookupDeviceByPath(self, path):
        for dev in self._devices[hwclass.DISK][:]:
            try:
                if dev.path == path:
                    return dev
            except AttributeError:
                continue
        raise LookupError('Device instance for device with path {0} not found'
                          ''.format(path))

    def _lookupConfByPath(self, path):
        for devConf in self.conf['devices'][:]:
            if devConf.get('path') == path:
                return devConf
        raise LookupError('Configuration of device with path {0} not found'
                          ''.format(path))

    def _updateInterfaceDevice(self, params):
        try:
            netDev = self._lookupDeviceByAlias(hwclass.NIC,
                                               params['alias'])
            netConf = self._lookupConfByAlias(params['alias'])

            linkValue = 'up' if utils.tobool(params.get('linkActive',
                                             netDev.linkActive)) else 'down'
            network = params.get('network', netDev.network)
            if network == '':
                network = net_api.DUMMY_BRIDGE
                linkValue = 'down'
            custom = params.get('custom', {})
            specParams = params.get('specParams')

            netsToMirror = params.get('portMirroring',
                                      netConf.get('portMirroring', []))

            with self.setLinkAndNetwork(netDev, netConf, linkValue, network,
                                        custom, specParams):
                with self.updatePortMirroring(netConf, netsToMirror):
                    return {'status': doneCode, 'vmList': self.status()}
        except (LookupError,
                SetLinkAndNetworkError,
                UpdatePortMirroringError) as e:
            return response.error('updateDevice', e.message)

    @contextmanager
    def migration_parameters(self, params):
        with self._confLock:
            self.conf['_migrationParams'] = params
        try:
            yield
        finally:
            with self._confLock:
                del self.conf['_migrationParams']

    @contextmanager
    def setLinkAndNetwork(self, dev, conf, linkValue, networkValue, custom,
                          specParams=None):
        vnicXML = dev.getXML()
        source = vnicXML.getElementsByTagName('source')[0]
        source.setAttribute('bridge', networkValue)
        try:
            link = vnicXML.getElementsByTagName('link')[0]
        except IndexError:
            link = vnicXML.appendChildWithArgs('link')
        link.setAttribute('state', linkValue)
        if (specParams and
                ('inbound' in specParams or 'outbound' in specParams)):
            oldBandwidths = vnicXML.getElementsByTagName('bandwidth')
            oldBandwidth = oldBandwidths[0] if len(oldBandwidths) else None
            newBandwidth = dev.paramsToBandwidthXML(specParams, oldBandwidth)
            if oldBandwidth is None:
                vnicXML.appendChild(newBandwidth)
            else:
                vnicXML.replaceChild(newBandwidth, oldBandwidth)
        vnicStrXML = vnicXML.toprettyxml(encoding='utf-8')
        try:
            try:
                vnicStrXML = hooks.before_update_device(vnicStrXML, self.conf,
                                                        custom)
                self._dom.updateDeviceFlags(vnicStrXML,
                                            libvirt.VIR_DOMAIN_AFFECT_LIVE)
                dev._deviceXML = vnicStrXML
                self.log.info("Nic has been updated:\n %s" % vnicStrXML)
                hooks.after_update_device(vnicStrXML, self.conf, custom)
            except Exception as e:
                self.log.warn('Request failed: %s', vnicStrXML, exc_info=True)
                hooks.after_update_device_fail(vnicStrXML, self.conf, custom)
                raise SetLinkAndNetworkError(e.message)
            yield
        except Exception:
            # Rollback link and network.
            self.log.warn('Rolling back link and net for: %s', dev.alias,
                          exc_info=True)
            self._dom.updateDeviceFlags(vnicXML.toxml(encoding='utf-8'),
                                        libvirt.VIR_DOMAIN_AFFECT_LIVE)
            raise
        else:
            # Update the device and the configuration.
            dev.network = conf['network'] = networkValue
            conf['linkActive'] = linkValue == 'up'
            setattr(dev, 'linkActive', linkValue == 'up')
            dev.custom = custom

    @contextmanager
    def updatePortMirroring(self, conf, networks):
        devName = conf['name']
        netsToDrop = [net for net in conf.get('portMirroring', [])
                      if net not in networks]
        netsToAdd = [net for net in networks
                     if net not in conf.get('portMirroring', [])]
        mirroredNetworks = []
        droppedNetworks = []
        try:
            for network in netsToDrop:
                supervdsm.getProxy().unsetPortMirroring(network, devName)
                droppedNetworks.append(network)
            for network in netsToAdd:
                supervdsm.getProxy().setPortMirroring(network, devName)
                mirroredNetworks.append(network)
            yield
        except Exception as e:
            self.log.exception(
                "%s for network %s failed",
                'setPortMirroring' if network in netsToAdd else
                'unsetPortMirroring',
                network)
            # In case we fail, we rollback the Network mirroring.
            for network in mirroredNetworks:
                supervdsm.getProxy().unsetPortMirroring(network, devName)
            for network in droppedNetworks:
                supervdsm.getProxy().setPortMirroring(network, devName)
            raise UpdatePortMirroringError(e.message)
        else:
            # Update the conf with the new mirroring.
            conf['portMirroring'] = networks

    def _updateGraphicsDevice(self, params):
        graphics = self._findGraphicsDeviceXMLByType(params['graphicsType'])
        if graphics:
            result = self._setTicketForGraphicDev(
                graphics, params['password'], params['ttl'],
                params.get('existingConnAction'), params['params'])
            if result['status']['code'] == 0:
                result['vmList'] = self.status()
            return result
        else:
            return response.error('updateDevice')

    def updateDevice(self, params):
        if params.get('deviceType') == hwclass.NIC:
            return self._updateInterfaceDevice(params)
        elif params.get('deviceType') == hwclass.GRAPHICS:
            return self._updateGraphicsDevice(params)
        else:
            return response.error('noimpl')

    def hotunplugNic(self, params):
        if self.isMigrating():
            return response.error('migInProgress')

        nicParams = params['nic']

        # Find NIC object in vm's NICs list
        nic = None
        for dev in self._devices[hwclass.NIC][:]:
            if dev.macAddr.lower() == nicParams['macAddr'].lower():
                nic = dev
                break

        if nic:
            if 'portMirroring' in nicParams:
                for network in nicParams['portMirroring']:
                    supervdsm.getProxy().unsetPortMirroring(network, nic.name)

            nicXml = nic.getXML().toprettyxml(encoding='utf-8')
            hooks.before_nic_hotunplug(nicXml, self.conf,
                                       params=nic.custom)
            # TODO: this is debug information. For 3.6.x we still need to
            # see the XML even with 'info' as default level.
            self.log.info("Hotunplug NIC xml: %s", nicXml)
        else:
            self.log.error("Hotunplug NIC failed - NIC not found: %s",
                           nicParams)
            return response.error('hotunplugNic', "NIC not found")

        # Remove found NIC from vm's NICs list
        if nic:
            self._devices[hwclass.NIC].remove(nic)
        # Find and remove NIC device from vm's conf
        nicDev = None
        for dev in self.conf['devices'][:]:
            if (dev['type'] == hwclass.NIC and
                    dev['macAddr'].lower() == nicParams['macAddr'].lower()):
                with self._confLock:
                    self.conf['devices'].remove(dev)
                nicDev = dev
                break

        self.saveState()

        try:
            self._dom.detachDevice(nicXml)
            self._waitForDeviceRemoval(nic)
            # TODO: avoid reattach when Engine can tell free VFs otherwise
            if nic.is_hostdevice:
                hostdev.reattach_detachable(nic.hostdev)
        except HotunplugTimeout as e:
            self.log.error("%s", e)
            self._rollback_nic_hotunplug(nicDev, nic)
            hooks.after_nic_hotunplug_fail(nicXml, self.conf,
                                           params=nic.custom)
            return response.error('hotunplugNic', "%s" % e)
        except libvirt.libvirtError as e:
            self.log.exception("Hotunplug failed")
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return response.error('noVM')
            self._rollback_nic_hotunplug(nicDev, nic)
            hooks.after_nic_hotunplug_fail(nicXml, self.conf,
                                           params=nic.custom)
            return response.error('hotunplugNic', e.message)

        hooks.after_nic_hotunplug(nicXml, self.conf,
                                  params=nic.custom)
        return {'status': doneCode, 'vmList': self.status()}

    # Restore NIC device in vm's conf and _devices
    def _rollback_nic_hotunplug(self, nic_dev, nic):
        if nic_dev:
            with self._confLock:
                self.conf['devices'].append(nic_dev)
        if nic:
            self._devices[hwclass.NIC].append(nic)
        self.saveState()

    def hotplugMemory(self, params):

        if self.isMigrating():
            return errCode['migInProgress']

        memParams = params.get('memory', {})
        device = vmdevices.core.Memory(self.conf, self.log, **memParams)

        deviceXml = device.getXML().toprettyxml(encoding='utf-8')
        deviceXml = hooks.before_memory_hotplug(deviceXml)
        device._deviceXML = deviceXml
        self.log.debug("Hotplug memory xml: %s", deviceXml)

        try:
            self._dom.attachDevice(deviceXml)
        except libvirt.libvirtError as e:
            self.log.exception("hotplugMemory failed")
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return errCode['noVM']
            return response.error('hotplugMem', e.message)

        self._devices[hwclass.MEMORY].append(device)
        with self._confLock:
            self.conf['devices'].append(memParams)
        self._updateDomainDescriptor()
        device.update_device_info(self, self._devices[hwclass.MEMORY])
        # TODO: this is raceful (as the similar code of hotplugDisk
        # and hotplugNic, as a concurrent call of hotplug can change
        # vm.conf before we return.
        self.saveState()

        hooks.after_memory_hotplug(deviceXml)

        return {'status': doneCode, 'vmList': self.status()}

    def setNumberOfCpus(self, numberOfCpus):

        if self.isMigrating():
            return response.error('migInProgress')

        self.log.debug("Setting number of cpus to : %s", numberOfCpus)
        hooks.before_set_num_of_cpus()
        try:
            self._dom.setVcpusFlags(numberOfCpus,
                                    libvirt.VIR_DOMAIN_AFFECT_CURRENT)
        except libvirt.libvirtError as e:
            self.log.exception("setNumberOfCpus failed")
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return response.error('noVM')
            return response.error('setNumberOfCpusErr', e.message)

        self.conf['smp'] = str(numberOfCpus)
        self.saveState()
        hooks.after_set_num_of_cpus()
        return {'status': doneCode, 'vmList': self.status()}

    def _updateVcpuLimit(self):
        qos = self._getVmPolicy()
        if qos is not None:
            try:
                vcpuLimit = qos.getElementsByTagName("vcpuLimit")
                self._vcpuLimit = vcpuLimit[0].childNodes[0].data
            except IndexError:
                # missing vcpuLimit node
                self._vcpuLimit = None

    def updateVmPolicy(self, params):
        """
        Update the QoS policy settings for VMs.

        The params argument contains the actual properties we are about to
        set. It must not be empty.

        Supported properties are:

        vcpuLimit - the CPU usage hard limit
        ioTune - the IO limits

        In the case not all properties are provided, the missing properties'
        setting will be left intact.

        If there is an error during the processing, this function
        immediately stops and returns. Remaining properties are not
        processed.

        :param params: dictionary mapping property name to its value
        :type params: dict[str] -> anything

        :return: standard vdsm result structure
        """

        if self.isMigrating():
            return response.error('migInProgress')

        if not params:
            self.log.error("updateVmPolicy got an empty policy.")
            return response.error('MissParam',
                                  'updateVmPolicy got an empty policy.')

        #
        # Get the current QoS block
        metadata_modified = False
        qos = self._getVmPolicy()
        if qos is None:
            return response.error('updateVmPolicyErr')

        #
        # Process provided properties, remove property after it is processed

        if 'vcpuLimit' in params:
            # Remove old value
            vcpuLimit = qos.getElementsByTagName("vcpuLimit")
            if vcpuLimit:
                qos.removeChild(vcpuLimit[0])

            vcpuLimit = vmxml.Element("vcpuLimit")
            vcpuLimit.appendTextNode(str(params["vcpuLimit"]))
            qos.appendChild(vcpuLimit)

            metadata_modified = True
            self._vcpuLimit = params.pop('vcpuLimit')

        if 'ioTune' in params:
            ioTuneParams = params["ioTune"]

            for ioTune in ioTuneParams:
                if ("path" in ioTune) or ("name" in ioTune):
                    continue

                self.log.debug("IoTuneParams: %s", str(ioTune))

                try:
                    # All 4 IDs are required to identify a device
                    # If there is a valid reason why not all 4 are required,
                    # please change the code

                    disk = self._findDriveByUUIDs({
                        'domainID': ioTune["domainID"],
                        'poolID': ioTune["poolID"],
                        'imageID': ioTune["imageID"],
                        'volumeID': ioTune["volumeID"]})

                    self.log.debug("Device path: %s", disk.path)
                    ioTune["name"] = disk.name
                    ioTune["path"] = disk.path

                except LookupError as e:
                    return response.error('updateVmPolicyErr', e.message)

            # Make sure the top level element exists
            ioTuneList = qos.getElementsByTagName("ioTune")
            if not ioTuneList:
                ioTuneElement = vmxml.Element("ioTune")
                ioTuneList.append(ioTuneElement)
                qos.appendChild(ioTuneElement)
                metadata_modified = True

            if update_io_tune_dom(ioTuneList[0], ioTuneParams) > 0:
                metadata_modified = True

            del params['ioTune']

        # Check remaining fields in params and report the list of unsupported
        # params to the log

        if params:
            self.log.warn("updateVmPolicy got unknown parameters: %s",
                          ", ".join(params.iterkeys()))

        #
        # Save modified metadata

        if metadata_modified:
            metadata_xml = qos.toprettyxml()

            try:
                self._dom.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                                      metadata_xml, METADATA_VM_TUNE_PREFIX,
                                      METADATA_VM_TUNE_URI, 0)
            except libvirt.libvirtError as e:
                self.log.exception("updateVmPolicy failed")
                if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return response.error('noVM')
                else:
                    return response.error('updateVmPolicyErr', e.message)

        return {'status': doneCode}

    def _getVmPolicy(self):
        """
        This method gets the current qos block from the libvirt metadata.
        If there is not any, it will create a new empty DOM tree with
        the <qos> root element.

        :return: XML DOM object representing the root qos element
        """

        metadata_xml = "<%s></%s>" % (METADATA_VM_TUNE_ELEMENT,
                                      METADATA_VM_TUNE_ELEMENT)

        try:
            metadata_xml = self._dom.metadata(
                libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                METADATA_VM_TUNE_URI, 0)
        except libvirt.libvirtError as e:
            if e.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                self.log.exception("getVmPolicy failed")
                return None

        metadata = _domParseStr(metadata_xml)
        return metadata.getElementsByTagName(METADATA_VM_TUNE_ELEMENT)[0]

    def _findDeviceByNameOrPath(self, device_name, device_path):
        for device in self._devices[hwclass.DISK]:
            if ((device.name == device_name
                or ("path" in device and device["path"] == device_path))
                    and isVdsmImage(device)):
                return device
        else:
            return None

    def getIoTunePolicy(self):
        tunables = []
        qos = self._getVmPolicy()
        ioTuneList = qos.getElementsByTagName("ioTune")
        if not ioTuneList or not ioTuneList[0].hasChildNodes():
            return response.success(ioTunePolicy=[])

        for device in ioTuneList[0].getElementsByTagName("device"):
            tunables.append(io_tune_dom_to_values(device))

        return response.success(ioTunePolicy=tunables)

    def getIoTune(self):
        resultList = []

        for device in self.getDiskDevices():
            if not isVdsmImage(device):
                continue

            try:
                res = self._dom.blockIoTune(
                    device.name,
                    libvirt.VIR_DOMAIN_AFFECT_LIVE)

                # use only certain fields, otherwise
                # Drive._validateIoTuneParams will not pass
                ioTune = {k: res[k] for k in (
                    'total_bytes_sec', 'read_bytes_sec',
                    'write_bytes_sec', 'total_iops_sec',
                    'write_iops_sec', 'read_iops_sec')}

                resultList.append({
                    'name': device.name,
                    'path': device.path,
                    'ioTune': ioTune})

            except libvirt.libvirtError as e:
                self.log.exception("getVmIoTune failed")
                if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return response.error('noVM')
                else:
                    return response.error('updateIoTuneErr', e.message)

        return response.success(ioTune=resultList)

    def setIoTune(self, tunables):
        for io_tune_change in tunables:
            device_name = io_tune_change.get('name', None)
            device_path = io_tune_change.get('path', None)
            io_tune = io_tune_change['ioTune']

            # Find the proper device object
            found_device = self._findDeviceByNameOrPath(device_name,
                                                        device_path)
            if found_device is None:
                return response.error(
                    'updateIoTuneErr',
                    "Device {} not found".format(device_name))

            # Merge the update with current values
            dom = found_device.getXML()
            io_dom_list = dom.getElementsByTagName("iotune")
            old_io_tune = {}
            if io_dom_list:
                collect_inner_elements(io_dom_list[0], old_io_tune)
                old_io_tune.update(io_tune)
                io_tune = old_io_tune

            # Verify the ioTune params
            try:
                found_device._validateIoTuneParams(io_tune)
            except ValueError:
                return self._reportException(key='updateIoTuneErr',
                                             msg='Invalid ioTune value')

            try:
                self._dom.setBlockIoTune(found_device.name, io_tune,
                                         libvirt.VIR_DOMAIN_AFFECT_LIVE)
            except libvirt.libvirtError as e:
                self.log.exception("setVmIoTune failed")
                if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return response.error('noVM')
                else:
                    return response.error('updateIoTuneErr', e.message)

            # Update both the ioTune arguments and device xml DOM
            # so we are still up-to-date
            # TODO: improve once libvirt gets support for iotune events
            #       see https://bugzilla.redhat.com/show_bug.cgi?id=1114492
            if io_dom_list:
                dom.removeChild(io_dom_list[0])
            io_dom = vmxml.Element("iotune")
            io_tune_values_to_dom(io_tune, io_dom)
            dom.appendChild(io_dom)
            found_device.specParams['ioTune'] = io_tune

            # Make sure the cached XML representation is valid as well
            xml = found_device.getXML().toprettyxml(encoding='utf-8')
            # TODO: this is debug information. For 3.6.x we still need to
            # see the XML even with 'info' as default level.
            self.log.info("New device XML for %s: %s",
                          found_device.name, xml)
            found_device._deviceXML = xml

        return {'status': doneCode}

    def _createTransientDisk(self, diskParams):
        if (diskParams.get('shared', None) !=
           vmdevices.storage.DRIVE_SHARED_TYPE.TRANSIENT):
            return

        # FIXME: This should be replaced in future the support for transient
        # disk in libvirt (BZ#832194)
        driveFormat = (
            qemuimg.FORMAT.QCOW2 if diskParams['format'] == 'cow' else
            qemuimg.FORMAT.RAW
        )

        transientHandle, transientPath = tempfile.mkstemp(
            dir=config.get('vars', 'transient_disks_repository'),
            prefix="%s-%s." % (diskParams['domainID'], diskParams['volumeID']))

        try:
            qemuimg.create(transientPath, format=qemuimg.FORMAT.QCOW2,
                           backing=diskParams['path'],
                           backingFormat=driveFormat)
            os.fchmod(transientHandle, 0o660)
        except Exception:
            os.unlink(transientPath)  # Closing after deletion is correct
            self.log.exception("Failed to create the transient disk for "
                               "volume %s", diskParams['volumeID'])
        finally:
            os.close(transientHandle)

        diskParams['path'] = transientPath
        diskParams['format'] = 'cow'

    def _removeTransientDisk(self, drive):
        if drive.transientDisk:
            os.unlink(drive.path)

    def hotplugDisk(self, params):
        if self.isMigrating():
            return response.error('migInProgress')

        diskParams = params.get('drive', {})
        diskParams['path'] = self.cif.prepareVolumePath(diskParams)

        if isVdsmImage(diskParams):
            self._normalizeVdsmImg(diskParams)
            self._createTransientDisk(diskParams)

        self.updateDriveIndex(diskParams)
        drive = vmdevices.storage.Drive(self.conf, self.log, **diskParams)

        if drive.hasVolumeLeases:
            return response.error('noimpl')

        driveXml = drive.getXML().toprettyxml(encoding='utf-8')
        # TODO: this is debug information. For 3.6.x we still need to
        # see the XML even with 'info' as default level.
        self.log.info("Hotplug disk xml: %s" % (driveXml))

        driveXml = hooks.before_disk_hotplug(driveXml, self.conf,
                                             params=drive.custom)
        drive._deviceXML = driveXml
        try:
            self._dom.attachDevice(driveXml)
        except libvirt.libvirtError as e:
            self.log.exception("Hotplug failed")
            self.cif.teardownVolumePath(diskParams)
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return response.error('noVM')
            return response.error('hotplugDisk', e.message)
        else:
            # FIXME!  We may have a problem here if vdsm dies right after
            # we sent command to libvirt and before save conf. In this case
            # we will gather almost all needed info about this drive from
            # the libvirt during recovery process.
            device_conf = self._devices[hwclass.DISK]
            device_conf.append(drive)

            with self._confLock:
                self.conf['devices'].append(diskParams)
            self.saveState()
            vmdevices.storage.Drive.update_device_info(self, device_conf)
            hooks.after_disk_hotplug(driveXml, self.conf,
                                     params=drive.custom)

        return {'status': doneCode, 'vmList': self.status()}

    def hotunplugDisk(self, params):
        if self.isMigrating():
            return response.error('migInProgress')

        diskParams = params.get('drive', {})
        diskParams['path'] = self.cif.prepareVolumePath(diskParams)

        try:
            drive = self._findDriveByUUIDs(diskParams)
        except LookupError:
            self.log.error("Hotunplug disk failed - Disk not found: %s",
                           diskParams)
            return response.error('hotunplugDisk', "Disk not found")

        if drive.hasVolumeLeases:
            return response.error('noimpl')

        driveXml = drive.getXML().toprettyxml(encoding='utf-8')
        # TODO: this is debug information. For 3.6.x we still need to
        # see the XML even with 'info' as default level.
        self.log.info("Hotunplug disk xml: %s", driveXml)

        hooks.before_disk_hotunplug(driveXml, self.conf,
                                    params=drive.custom)
        try:
            self._dom.detachDevice(driveXml)
            self._waitForDeviceRemoval(drive)
        except HotunplugTimeout as e:
            self.log.error("%s", e)
            return response.error('hotunplugDisk', "%s" % e)
        except libvirt.libvirtError as e:
            self.log.exception("Hotunplug failed")
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return response.error('noVM')
            return response.error('hotunplugDisk', e.message)
        else:
            self._devices[hwclass.DISK].remove(drive)

            # Find and remove disk device from vm's conf
            for dev in self.conf['devices'][:]:
                if dev['type'] == hwclass.DISK and dev['path'] == drive.path:
                    with self._confLock:
                        self.conf['devices'].remove(dev)
                    break

            self.saveState()
            hooks.after_disk_hotunplug(driveXml, self.conf,
                                       params=drive.custom)
            self._cleanupDrives(drive)

        return {'status': doneCode, 'vmList': self.status()}

    def _waitForDeviceRemoval(self, device):
        """
        As stated in libvirt documentary, after detaching a device using
        virDomainDetachDeviceFlags, we need to verify that this device
        has actually been detached:
        libvirt.org/html/libvirt-libvirt-domain.html#virDomainDetachDeviceFlags

        This function waits for the device to be detached.

        Currently we use virDomainDetachDevice. However- That function behaves
        the same in that matter. (Currently it is not documented at libvirt's
        API docs- but after contacting libvirt's guys it turned out that this
        is true. Bug 1257280 opened for fixing the documentation.)
        TODO: remove this comment when the documentation will be fixed.

        :param device: Device to wait for
        """
        self.log.debug("Waiting for hotunplug to finish")
        with utils.stopwatch("Hotunplug device %s" % device.name):
            deadline = (utils.monotonic_time() +
                        config.getfloat('vars', 'hotunplug_timeout'))
            sleep_time = config.getfloat('vars', 'hotunplug_check_interval')
            while device.is_attached_to(self._dom.XMLDesc(0)):
                time.sleep(sleep_time)
                if utils.monotonic_time() > deadline:
                    raise HotunplugTimeout("Timeout detaching device %s"
                                           % device.name)

    def _readPauseCode(self):
        state, reason = self._dom.state(0)

        if (state == libvirt.VIR_DOMAIN_PAUSED and
           reason == libvirt.VIR_DOMAIN_PAUSED_IOERROR):

            diskErrors = self._dom.diskErrors()
            for device, error in diskErrors.iteritems():
                if error == libvirt.VIR_DOMAIN_DISK_ERROR_NO_SPACE:
                    self.log.warning('device %s out of space', device)
                    return 'ENOSPC'
                elif error == libvirt.VIR_DOMAIN_DISK_ERROR_UNSPEC:
                    # Mapping to 'EOTHER' may not be exact.
                    # It is still safer than EIO given the VDSM mechanics.
                    self.log.warning('device %s reported I/O error',
                                     device)
                    return 'EOTHER'
                # else error == libvirt.VIR_DOMAIN_DISK_ERROR_NONE
                # so no worries.

        return 'NOERR'

    def isDomainReadyForCommands(self):
        """
        Returns True if the domain is reported to be in the safest condition
        to accept commands.
        False negative (domain is reported NOT ready, but it is) is possible
        False positive (domain is reported ready, but it is NOT) is avoided
        """
        try:
            state, details, stateTime = self._dom.controlInfo()
        except virdomain.NotConnectedError:
            # this method may be called asynchronously by periodic
            # operations. Thus, we must use a try/except block
            # to avoid racy checks.
            return False
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                # same as NotConnectedError above: possible race on shutdown
                return False
            else:
                raise
        else:
            return state == libvirt.VIR_DOMAIN_CONTROL_OK

    def _timeoutExperienced(self, timeout):
        if timeout:
            self._monitorResponse = -1
        else:
            self._monitorResponse = 0

    def _completeIncomingMigration(self):
        if 'restoreState' in self.conf:
            self.cont()
            with self._confLock:
                del self.conf['restoreState']
                fromSnapshot = self.conf.pop('restoreFromSnapshot', False)
            hooks.after_vm_dehibernate(self._dom.XMLDesc(0), self.conf,
                                       {'FROM_SNAPSHOT': fromSnapshot})
            self._syncGuestTime()
        elif 'migrationDest' in self.conf:
            if self._needToWaitForMigrationToComplete():
                usedTimeout = self._waitForUnderlyingMigration()
                self._attachLibvirtDomainAfterMigration(
                    self._incomingMigrationFinished.isSet(), usedTimeout)
            # else domain connection already established earlier
            self._domDependentInit()
            del self.conf['migrationDest']
            hooks.after_vm_migrate_destination(self._dom.XMLDesc(0), self.conf)

            for dev in self._customDevices():
                hooks.after_device_migrate_destination(
                    dev._deviceXML, self.conf, dev.custom)

            # We refrain from syncing time in this path.  There are two basic
            # reasons:
            # 1. The jump change in the time (as performed by QEMU) may cause
            #    undesired effects like unnecessary timeouts, false alerts
            #    (think about logging excessive SQL command execution times),
            #    etc.  This is not what users expect when performing live
            #    migrations.
            # 2. The user can simply run NTP on the VM to keep the time right
            #    and smooth after migrations.  On the contrary to suspensions,
            #    there is no danger of excessive delays preventing NTP from
            #    operation.

        with self._confLock:
            if 'guestIPs' in self.conf:
                del self.conf['guestIPs']
            if 'guestFQDN' in self.conf:
                del self.conf['guestFQDN']
            if 'username' in self.conf:
                del self.conf['username']
        self.saveState()
        self.log.info("End of migration")

    def _needToWaitForMigrationToComplete(self):
        if not self.recovering:
            # if not recovering, we are in a base flow and need
            # to wait for migration to complete
            return True

        try:
            if not self._isDomainRunning():
                # migration still in progress during recovery
                return True
        except libvirt.libvirtError:
            self.log.exception('migration failed while recovering!')
            raise MigrationError()
        else:
            self.log.info('migration completed while recovering!')
            return False

    def _waitForUnderlyingMigration(self):
        timeout = config.getint('vars', 'migration_destination_timeout')
        self.log.debug("Waiting %s seconds for end of migration", timeout)
        self._incomingMigrationFinished.wait(timeout)
        return timeout

    def _attachLibvirtDomainAfterMigration(self, migrationFinished, timeout):
        try:
            # Would fail if migration isn't successful,
            # or restart vdsm if connection to libvirt was lost
            self._dom = virdomain.Notifying(
                self._connection.lookupByUUIDString(self.id),
                self._timeoutExperienced)

            if not migrationFinished:
                state = self._dom.state(0)
                if state[0] == libvirt.VIR_DOMAIN_PAUSED:
                    if state[1] == libvirt.VIR_DOMAIN_PAUSED_MIGRATION:
                        raise MigrationError("Migration Error - Timed out "
                                             "(did not receive success "
                                             "event)")
                self.log.debug("NOTE: incomingMigrationFinished event has "
                               "not been set and wait timed out after %d "
                               "seconds. Current VM state: %d, reason %d. "
                               "Continuing with VM initialization anyway.",
                               timeout, state[0], state[1])
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                if not migrationFinished:
                    newMsg = ('%s - Timed out '
                              '(did not receive success event)' %
                              (e.args[0] if len(e.args) else
                               'Migration Error'))
                    e.args = (newMsg,) + e.args[1:]
                raise MigrationError(e.get_error_message())
            raise

    def _underlyingCont(self):
        hooks.before_vm_cont(self._dom.XMLDesc(0), self.conf)
        self._dom.resume()

    def _underlyingPause(self):
        hooks.before_vm_pause(self._dom.XMLDesc(0), self.conf)
        self._dom.suspend()

    def _findDriveByName(self, name):
        for device in self._devices[hwclass.DISK][:]:
            if device.name == name:
                return device
        raise LookupError("No such drive: '%s'" % name)

    def _findDriveByUUIDs(self, drive):
        """Find a drive given its definition"""

        if "domainID" in drive:
            tgetDrv = (drive["domainID"], drive["imageID"],
                       drive["volumeID"])

            for device in self._devices[hwclass.DISK][:]:
                if not hasattr(device, "domainID"):
                    continue
                if (device.domainID, device.imageID,
                        device.volumeID) == tgetDrv:
                    return device

        elif "GUID" in drive:
            for device in self._devices[hwclass.DISK][:]:
                if not hasattr(device, "GUID"):
                    continue
                if device.GUID == drive["GUID"]:
                    return device

        elif "UUID" in drive:
            for device in self._devices[hwclass.DISK][:]:
                if not hasattr(device, "UUID"):
                    continue
                if device.UUID == drive["UUID"]:
                    return device

        elif drive.get('diskType') == DISK_TYPE.NETWORK:
            for device in self._devices[hwclass.DISK][:]:
                if device.diskType != DISK_TYPE.NETWORK:
                    continue
                if device.path == drive["path"]:
                    return device

        raise LookupError("No such drive: '%s'" % drive)

    def _findDriveConfigByName(self, name):
        devices = self.conf["devices"][:]
        for device in devices:
            if device['type'] == hwclass.DISK and device.get("name") == name:
                return device
        raise LookupError("No such disk %r" % name)

    def updateDriveVolume(self, vmDrive):
        if not vmDrive.device == 'disk' or not isVdsmImage(vmDrive):
            return

        try:
            volSize = self._getVolumeSize(
                vmDrive.domainID, vmDrive.poolID, vmDrive.imageID,
                vmDrive.volumeID)
        except StorageUnavailableError as e:
            self.log.error("Unable to update drive %s volume size: %s",
                           vmDrive.name, e)
            return

        vmDrive.truesize = volSize.truesize
        vmDrive.apparentsize = volSize.apparentsize

    def updateDriveParameters(self, driveParams):
        """Update the drive with the new volume information"""

        # Updating the vmDrive object
        for vmDrive in self._devices[hwclass.DISK][:]:
            if vmDrive.name == driveParams["name"]:
                for k, v in driveParams.iteritems():
                    setattr(vmDrive, k, v)
                self.updateDriveVolume(vmDrive)
                break
        else:
            self.log.error("Unable to update the drive object for: %s",
                           driveParams["name"])

        # Updating the VM configuration
        try:
            conf = self._findDriveConfigByName(driveParams["name"])
        except LookupError:
            self.log.error("Unable to update the device configuration ",
                           "for disk %s", driveParams["name"])
        else:
            with self._confLock:
                conf.update(driveParams)
            self.saveState()

    def freeze(self):
        """
        Freeze every mounted filesystems within the guest (hence guest agent
        may be required depending on hypervisor used).
        """
        self.log.info("Freezing guest filesystems")

        try:
            frozen = self._dom.fsFreeze()
        except libvirt.libvirtError as e:
            self.log.warning("Unable to freeze guest filesystems: %s", e)
            code = e.get_error_code()
            if code == libvirt.VIR_ERR_AGENT_UNRESPONSIVE:
                name = "nonresp"
            elif code == libvirt.VIR_ERR_NO_SUPPORT:
                name = "unsupportedOperationErr"
            else:
                name = "freezeErr"
            return response.error(name, message=e.get_error_message())

        self.log.info("%d guest filesystems frozen", frozen)
        return response.success()

    def thaw(self):
        """
        Thaw every mounted filesystems within the guest (hence guest agent may
        be required depending on hypervisor used).
        """
        self.log.info("Thawing guest filesystems")

        try:
            thawed = self._dom.fsThaw()
        except libvirt.libvirtError as e:
            self.log.warning("Unable to thaw guest filesystems: %s", e)
            code = e.get_error_code()
            if code == libvirt.VIR_ERR_AGENT_UNRESPONSIVE:
                name = "nonresp"
            elif code == libvirt.VIR_ERR_NO_SUPPORT:
                name = "unsupportedOperationErr"
            else:
                name = "thawErr"
            return response.error(name, message=e.get_error_message())

        self.log.info("%d guest filesystems thawed", thawed)
        return response.success()

    def snapshot(self, snapDrives, memoryParams, frozen=False):
        """Live snapshot command"""

        def _diskSnapshot(vmDev, newPath, sourceType):
            """Libvirt snapshot XML"""

            disk = vmxml.Element('disk', name=vmDev, snapshot='external',
                                 type=sourceType)
            args = {'type': sourceType}
            if sourceType == 'file':
                args['file'] = newPath
            elif sourceType == 'block':
                args['dev'] = newPath
            disk.appendChildWithArgs('source', **args)
            return disk

        def _normSnapDriveParams(drive):
            """Normalize snapshot parameters"""

            if "baseVolumeID" in drive:
                baseDrv = {"device": "disk",
                           "domainID": drive["domainID"],
                           "imageID": drive["imageID"],
                           "volumeID": drive["baseVolumeID"]}
                tgetDrv = baseDrv.copy()
                tgetDrv["volumeID"] = drive["volumeID"]

            elif "baseGUID" in drive:
                baseDrv = {"GUID": drive["baseGUID"]}
                tgetDrv = {"GUID": drive["GUID"]}

            elif "baseUUID" in drive:
                baseDrv = {"UUID": drive["baseUUID"]}
                tgetDrv = {"UUID": drive["UUID"]}

            else:
                baseDrv, tgetDrv = (None, None)

            return baseDrv, tgetDrv

        def _rollbackDrives(newDrives):
            """Rollback the prepared volumes for the snapshot"""

            for vmDevName, drive in newDrives.iteritems():
                try:
                    self.cif.teardownVolumePath(drive)
                except Exception:
                    self.log.exception("Unable to teardown drive: %s",
                                       vmDevName)

        def _memorySnapshot(memoryVolumePath):
            """Libvirt snapshot XML"""

            return vmxml.Element('memory',
                                 snapshot='external',
                                 file=memoryVolumePath)

        def _vmConfForMemorySnapshot():
            """Returns the needed vm configuration with the memory snapshot"""

            return {'restoreFromSnapshot': True,
                    '_srcDomXML': self._dom.XMLDesc(0),
                    'elapsedTimeOffset': time.time() - self._startTime}

        def _padMemoryVolume(memoryVolPath, sdUUID):
            sdType = sd.name2type(
                self.cif.irs.getStorageDomainInfo(sdUUID)['info']['type'])
            if sdType in sd.FILE_DOMAIN_TYPES:
                if sdType == sd.NFS_DOMAIN:
                    oop.getProcessPool(sdUUID).fileUtils. \
                        padToBlockSize(memoryVolPath)
                else:
                    fileUtils.padToBlockSize(memoryVolPath)

        snap = vmxml.Element('domainsnapshot')
        disks = vmxml.Element('disks')
        newDrives = {}
        vmDrives = {}

        if self.isMigrating():
            return response.error('migInProgress')

        for drive in snapDrives:
            baseDrv, tgetDrv = _normSnapDriveParams(drive)

            try:
                self._findDriveByUUIDs(tgetDrv)
            except LookupError:
                # The vm is not already using the requested volume for the
                # snapshot, continuing.
                pass
            else:
                # The snapshot volume is the current one, skipping
                self.log.debug("The volume is already in use: %s", tgetDrv)
                continue  # Next drive

            try:
                vmDrive = self._findDriveByUUIDs(baseDrv)
            except LookupError:
                # The volume we want to snapshot doesn't exist
                self.log.error("The base volume doesn't exist: %s", baseDrv)
                return response.error('snapshotErr')

            if vmDrive.hasVolumeLeases:
                self.log.error('disk %s has volume leases', vmDrive.name)
                return response.error('noimpl')

            if vmDrive.transientDisk:
                self.log.error('disk %s is a transient disk', vmDrive.name)
                return response.error('transientErr')

            vmDevName = vmDrive.name

            newDrives[vmDevName] = tgetDrv.copy()
            newDrives[vmDevName]["poolID"] = vmDrive.poolID
            newDrives[vmDevName]["name"] = vmDevName
            newDrives[vmDevName]["format"] = "cow"

            # We need to keep track of the drive object because we cannot
            # safely access the blockDev property until after prepareVolumePath
            vmDrives[vmDevName] = vmDrive

        preparedDrives = {}

        for vmDevName, vmDevice in newDrives.iteritems():
            # Adding the device before requesting to prepare it as we want
            # to be sure to teardown it down even when prepareVolumePath
            # failed for some unknown issue that left the volume active.
            preparedDrives[vmDevName] = vmDevice
            try:
                newDrives[vmDevName]["path"] = \
                    self.cif.prepareVolumePath(newDrives[vmDevName])
            except Exception:
                self.log.exception('unable to prepare the volume path for '
                                   'disk %s', vmDevName)
                _rollbackDrives(preparedDrives)
                return response.error('snapshotErr')

            snapType = 'block' if vmDrives[vmDevName].blockDev else 'file'
            snapelem = _diskSnapshot(vmDevName, newDrives[vmDevName]["path"],
                                     snapType)
            disks.appendChild(snapelem)

        snap.appendChild(disks)

        snapFlags = (libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_REUSE_EXT |
                     libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_NO_METADATA)

        if memoryParams:
            # Save the needed vm configuration
            # TODO: this, as other places that use pickle.dump
            # directly to files, should be done with outOfProcess
            vmConfVol = memoryParams['dstparams']
            vmConfVolPath = self.cif.prepareVolumePath(vmConfVol)
            vmConf = _vmConfForMemorySnapshot()
            try:
                # Use r+ to avoid truncating the file, see BZ#1282239
                with open(vmConfVolPath, "r+") as f:
                    pickle.dump(vmConf, f)
            finally:
                self.cif.teardownVolumePath(vmConfVol)

            # Adding the memory volume to the snapshot xml
            memoryVol = memoryParams['dst']
            memoryVolPath = self.cif.prepareVolumePath(memoryVol)
            snap.appendChild(_memorySnapshot(memoryVolPath))
        else:
            snapFlags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY

        # When creating memory snapshot libvirt will pause the vm
        should_freeze = not (memoryParams or frozen)

        snapxml = snap.toprettyxml()
        # TODO: this is debug information. For 3.6.x we still need to
        # see the XML even with 'info' as default level.
        self.log.info(snapxml)

        # We need to stop the collection of the stats for two reasons, one
        # is to prevent spurious libvirt errors about missing drive paths
        # (since we're changing them), and also to prevent to trigger a drive
        # extension for the new volume with the apparent size of the old one
        # (the apparentsize is updated as last step in updateDriveParameters)
        self.stopDisksStatsCollection()

        try:
            if should_freeze:
                freezed = self.freeze()
            try:
                self.log.info("Taking a live snapshot (drives=%s, memory=%s)",
                              ', '.join(drive["name"] for drive in
                                        newDrives.values()),
                              memoryParams is not None)
                self._dom.snapshotCreateXML(snapxml, snapFlags)
                self.log.info("Completed live snapshot")
            except libvirt.libvirtError:
                self.log.exception("Unable to take snapshot")
                return response.error('snapshotErr')
            finally:
                # Must always thaw, even if freeze failed; in case the guest
                # did freeze the filesystems, but failed to reply in time.
                # Libvirt is using same logic (see src/qemu/qemu_driver.c).
                if should_freeze:
                    self.thaw()

            # We are padding the memory volume with block size of zeroes
            # because qemu-img truncates files such that their size is
            # round down to the closest multiple of block size (bz 970559).
            # This code should be removed once qemu-img will handle files
            # with size that is not multiple of block size correctly.
            if memoryParams:
                _padMemoryVolume(memoryVolPath, memoryVol['domainID'])

            for drive in newDrives.values():  # Update the drive information
                try:
                    self.updateDriveParameters(drive)
                except Exception:
                    # Here it's too late to fail, the switch already happened
                    # and there's nothing we can do, we must to proceed anyway
                    # to report the live snapshot success.
                    self.log.exception("Failed to update drive information"
                                       " for '%s'", drive)
        finally:
            self.startDisksStatsCollection()
            if memoryParams:
                self.cif.teardownVolumePath(memoryVol)

        # Returning quiesce to notify the manager whether the guest agent
        # froze and flushed the filesystems or not.
        quiesce = should_freeze and freezed["status"]["code"] == 0
        return {'status': doneCode, 'quiesce': quiesce}

    def diskReplicateStart(self, srcDisk, dstDisk):
        try:
            drive = self._findDriveByUUIDs(srcDisk)
        except LookupError:
            self.log.error("Unable to find the disk for '%s'", srcDisk)
            return response.error('imageErr')

        if drive.hasVolumeLeases:
            return response.error('noimpl')

        if drive.transientDisk:
            return response.error('transientErr')

        replica = dstDisk.copy()

        replica['device'] = 'disk'
        replica['format'] = 'cow'
        replica.setdefault('cache', drive.cache)
        replica.setdefault('propagateErrors', drive.propagateErrors)

        # First mark the disk as replicated, so if we crash after the volume is
        # prepared, we clean up properly in diskReplicateFinish.
        try:
            self._setDiskReplica(drive, replica)
        except Exception:
            self.log.error("Unable to set the replication for disk '%s' with "
                           "destination '%s'", drive.name, replica)
            return response.error('replicaErr')

        try:
            replica['path'] = self.cif.prepareVolumePath(replica)
            try:
                # Add information required during replication, and persist it
                # so migration can continue after vdsm crash.
                if utils.isBlockDevice(replica['path']):
                    replica['diskType'] = DISK_TYPE.BLOCK
                else:
                    replica['diskType'] = DISK_TYPE.FILE
                self._updateDiskReplica(drive)

                self._startDriveReplication(drive)
            except Exception:
                self.cif.teardownVolumePath(replica)
                raise
        except Exception:
            self.log.exception("Unable to start replication for %s to %s",
                               drive.name, replica)
            self._delDiskReplica(drive)
            return response.error('replicaErr')

        if drive.chunked or drive.replicaChunked:
            try:
                capacity, alloc, physical = self._getExtendInfo(drive)
                self.extendDriveVolume(drive, drive.volumeID, physical,
                                       capacity)
            except Exception:
                self.log.exception("Initial extension request failed for %s",
                                   drive.name)

        return {'status': doneCode}

    def diskReplicateFinish(self, srcDisk, dstDisk):
        try:
            drive = self._findDriveByUUIDs(srcDisk)
        except LookupError:
            self.log.error("Drive not found (srcDisk: %r)", srcDisk)
            return response.error('imageErr')

        if drive.hasVolumeLeases:
            self.log.error("Drive has volume leases, replication not "
                           "supported (drive: %r, srcDisk: %r)",
                           drive.name, srcDisk)
            return response.error('noimpl')

        if drive.transientDisk:
            self.log.error("Transient disk, replication not supported "
                           "(drive: %r, srcDisk: %r)", drive.name, srcDisk)
            return response.error('transientErr')

        if not drive.isDiskReplicationInProgress():
            self.log.error("No replication in progress (drive: %r, "
                           "srcDisk: %r)", drive.name, srcDisk)
            return response.error('replicaErr')

        # Looking for the replication blockJob info (checking its presence)
        blkJobInfo = self._dom.blockJobInfo(drive.name, 0)

        if (not isinstance(blkJobInfo, dict)
                or 'cur' not in blkJobInfo or 'end' not in blkJobInfo):
            self.log.error("Replication job not found (drive: %r, "
                           "srcDisk: %r, job: %r)",
                           drive.name, srcDisk, blkJobInfo)

            # Making sure that we don't have any stale information
            self._delDiskReplica(drive)
            return response.error('replicaErr')

        # Checking if we reached the replication mode ("mirroring" in libvirt
        # and qemu terms)
        if blkJobInfo['cur'] != blkJobInfo['end']:
            self.log.error("Replication job unfinished (drive: %r, "
                           "srcDisk: %r, job: %r)",
                           drive.name, srcDisk, blkJobInfo)
            return response.error('unavail')

        dstDiskCopy = dstDisk.copy()

        # Updating the destination disk device and name, the device is used by
        # prepareVolumePath (required to fill the new information as the path)
        # and the name is used by updateDriveParameters.
        dstDiskCopy.update({'device': drive.device, 'name': drive.name})
        dstDiskCopy['path'] = self.cif.prepareVolumePath(dstDiskCopy)

        if srcDisk != dstDisk:
            self.log.debug("Stopping the disk replication switching to the "
                           "destination drive: %s", dstDisk)
            blockJobFlags = libvirt.VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT
            diskToTeardown = srcDisk

            # We need to stop the stats collection in order to avoid spurious
            # errors from the stats threads during the switch from the old
            # drive to the new one. This applies only to the case where we
            # actually switch to the destination.
            self.stopDisksStatsCollection()
        else:
            self.log.debug("Stopping the disk replication remaining on the "
                           "source drive: %s", dstDisk)
            blockJobFlags = 0
            diskToTeardown = drive.diskReplicate

        try:
            # Stopping the replication
            self._dom.blockJobAbort(drive.name, blockJobFlags)
        except Exception:
            self.log.exception("Unable to stop the replication for"
                               " the drive: %s", drive.name)
            try:
                self.cif.teardownVolumePath(drive.diskReplicate)
            except Exception:
                # There is nothing we can do at this point other than logging
                self.log.exception("Unable to teardown the replication "
                                   "destination disk")
            return response.error('changeDisk')  # Finally is evaluated
        else:
            try:
                self.cif.teardownVolumePath(diskToTeardown)
            except Exception:
                # There is nothing we can do at this point other than logging
                self.log.exception("Unable to teardown the previous chain: %s",
                                   diskToTeardown)
            self.updateDriveParameters(dstDiskCopy)
        finally:
            self._delDiskReplica(drive)
            self.startDisksStatsCollection()

        return {'status': doneCode}

    def _startDriveReplication(self, drive):
        destxml = drive.getReplicaXML().toprettyxml()
        self.log.debug("Replicating drive %s to %s", drive.name, destxml)

        flags = (libvirt.VIR_DOMAIN_BLOCK_COPY_SHALLOW |
                 libvirt.VIR_DOMAIN_BLOCK_COPY_REUSE_EXT)

        # TODO: Remove fallback when using libvirt >= 1.2.9.
        try:
            self._dom.blockCopy(drive.name, destxml, flags=flags)
        except libvirt.libvirtError as e:
            if e.get_error_code() != libvirt.VIR_ERR_NO_SUPPORT:
                raise

            self.log.warning("blockCopy not supported, using blockRebase")

            base = drive.diskReplicate["path"]
            self.log.debug("Replicating drive %s to %s", drive.name, base)

            flags = (libvirt.VIR_DOMAIN_BLOCK_REBASE_COPY |
                     libvirt.VIR_DOMAIN_BLOCK_REBASE_REUSE_EXT |
                     libvirt.VIR_DOMAIN_BLOCK_REBASE_SHALLOW)

            if drive.diskReplicate["diskType"] == DISK_TYPE.BLOCK:
                flags |= libvirt.VIR_DOMAIN_BLOCK_REBASE_COPY_DEV

            self._dom.blockRebase(drive.name, base, flags=flags)

    def _setDiskReplica(self, drive, replica):
        """
        This utility method is used to set the disk replication information
        both in the live object used by vdsm and the vm configuration
        dictionary that is stored on disk (so that the information is not
        lost across restarts).
        """
        if drive.isDiskReplicationInProgress():
            raise RuntimeError("Disk '%s' already has an ongoing "
                               "replication" % drive.name)

        conf = self._findDriveConfigByName(drive.name)
        with self._confLock:
            conf['diskReplicate'] = replica
        self.saveState()

        drive.diskReplicate = replica

    def _updateDiskReplica(self, drive):
        """
        Update the persisted copy of drive replica.
        """
        if not drive.isDiskReplicationInProgress():
            raise RuntimeError("Disk '%s' does not have an ongoing "
                               "replication" % drive.name)

        conf = self._findDriveConfigByName(drive.name)
        with self._confLock:
            conf['diskReplicate'] = drive.diskReplicate
        self.saveState()

    def _delDiskReplica(self, drive):
        """
        This utility method is the inverse of _setDiskReplica, look at the
        _setDiskReplica description for more information.
        """
        del drive.diskReplicate

        conf = self._findDriveConfigByName(drive.name)
        with self._confLock:
            del conf['diskReplicate']
        self.saveState()

    def _diskSizeExtendCow(self, drive, newSizeBytes):
        try:
            # Due to an old bug in libvirt (BZ#963881) this call used to be
            # broken for NFS domains when squash_root was enabled.  This has
            # been fixed since libvirt-0.10.2-29
            curVirtualSize = self._dom.blockInfo(drive.name)[0]
        except libvirt.libvirtError:
            self.log.exception("An error occurred while getting the current "
                               "disk size")
            return response.error('resizeErr')

        if curVirtualSize > newSizeBytes:
            self.log.error(
                "Requested extension size %s for disk %s is smaller "
                "than the current size %s", newSizeBytes, drive.name,
                curVirtualSize)
            return response.error('resizeErr')

        # Uncommit the current volume size (mark as in transaction)
        self._setVolumeSize(drive.domainID, drive.poolID, drive.imageID,
                            drive.volumeID, 0)

        try:
            self._dom.blockResize(drive.name, newSizeBytes,
                                  libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
        except libvirt.libvirtError:
            self.log.exception(
                "An error occurred while trying to extend the disk %s "
                "to size %s", drive.name, newSizeBytes)
            return response.error('updateDevice')
        finally:
            # Note that newVirtualSize may be larger than the requested size
            # because of rounding in qemu.
            try:
                newVirtualSize = self._dom.blockInfo(drive.name)[0]
            except libvirt.libvirtError:
                self.log.exception("An error occurred while getting the "
                                   "updated disk size")
                return response.error('resizeErr')
            self._setVolumeSize(drive.domainID, drive.poolID, drive.imageID,
                                drive.volumeID, newVirtualSize)

        return {'status': doneCode, 'size': str(newVirtualSize)}

    def _diskSizeExtendRaw(self, drive, newSizeBytes):
        # Picking up the volume size extension
        self.__refreshDriveVolume({
            'domainID': drive.domainID, 'poolID': drive.poolID,
            'imageID': drive.imageID, 'volumeID': drive.volumeID,
        })

        volSize = self._getVolumeSize(
            drive.domainID, drive.poolID, drive.imageID, drive.volumeID)

        # For the RAW device we use the volumeInfo apparentsize rather
        # than the (possibly) wrong size provided in the request.
        if volSize.apparentsize != newSizeBytes:
            self.log.info(
                "The requested extension size %s is different from "
                "the RAW device size %s", newSizeBytes, volSize.apparentsize)

        # At the moment here there's no way to fetch the previous size
        # to compare it with the new one. In the future blockInfo will
        # be able to return the value (fetched from qemu).

        try:
            self._dom.blockResize(drive.name, volSize.apparentsize,
                                  libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
        except libvirt.libvirtError:
            self.log.warn(
                "Libvirt failed to notify the new size %s to the "
                "running VM, the change will be available at the ",
                "reboot", volSize.apparentsize, exc_info=True)
            return response.error('updateDevice')

        return {'status': doneCode, 'size': str(volSize.apparentsize)}

    def diskSizeExtend(self, driveSpecs, newSizeBytes):
        try:
            newSizeBytes = int(newSizeBytes)
        except ValueError:
            return response.error('resizeErr')

        try:
            drive = self._findDriveByUUIDs(driveSpecs)
        except LookupError:
            return response.error('imageErr')

        try:
            if drive.format == "cow":
                return self._diskSizeExtendCow(drive, newSizeBytes)
            else:
                return self._diskSizeExtendRaw(drive, newSizeBytes)
        except Exception:
            self.log.exception("Unable to extend disk %s to size %s",
                               drive.name, newSizeBytes)
            return response.error('updateDevice')

    def onWatchdogEvent(self, action):
        def actionToString(action):
            # the following action strings come from the comments of
            # virDomainEventWatchdogAction in include/libvirt/libvirt.h
            # of libvirt source.
            actionStrings = ("No action, watchdog ignored",
                             "Guest CPUs are paused",
                             "Guest CPUs are reset",
                             "Guest is forcibly powered off",
                             "Guest is requested to gracefully shutdown",
                             "No action, a debug message logged")

            try:
                return actionStrings[action]
            except IndexError:
                return "Received unknown watchdog action(%s)" % action

        actionEnum = ['ignore', 'pause', 'reset', 'destroy', 'shutdown', 'log']
        self._watchdogEvent["time"] = time.time()
        self._watchdogEvent["action"] = actionEnum[action]
        self.log.info("Watchdog event comes from guest %s. "
                      "Action: %s", self.name,
                      actionToString(action))

    def changeCD(self, cdromspec):
        if isinstance(cdromspec, basestring):
            # < 4.0 - known cdrom interface/index
            drivespec = cdromspec
            if cpuarch.is_ppc(self.arch):
                blockdev = 'sda'
            else:
                blockdev = 'hdc'
        else:
            # > 4.0 - variable cdrom interface/index
            drivespec = cdromspec['path']
            blockdev = vmdevices.storage.makeName(
                cdromspec['iface'], cdromspec['index'])

        return self._changeBlockDev('cdrom', blockdev, drivespec)

    def changeFloppy(self, drivespec):
        return self._changeBlockDev('floppy', 'fda', drivespec)

    def _changeBlockDev(self, vmDev, blockdev, drivespec):
        try:
            path = self.cif.prepareVolumePath(drivespec)
        except VolumeError:
            return response.error('imageErr')
        diskelem = vmxml.Element('disk', type='file', device=vmDev)
        diskelem.appendChildWithArgs('source', file=path)
        diskelem.appendChildWithArgs('target', dev=blockdev)

        try:
            self._dom.updateDeviceFlags(
                diskelem.toxml(), libvirt.VIR_DOMAIN_DEVICE_MODIFY_FORCE)
        except Exception:
            self.log.debug("updateDeviceFlags failed", exc_info=True)
            self.cif.teardownVolumePath(drivespec)
            return response.error('changeDisk')
        if vmDev in self.conf:
            self.cif.teardownVolumePath(self.conf[vmDev])

        self.conf[vmDev] = path
        return {'status': doneCode, 'vmList': self.status()}

    def setTicket(self, otp, seconds, connAct, params):
        """
        setTicket defaults to the first graphic device.
        use updateDevice to select the device.
        """
        try:
            graphics = self._domain.get_device_elements('graphics')[0]
        except IndexError:
            return response.error('ticketErr',
                                  'no graphics devices configured')
        return self._setTicketForGraphicDev(
            graphics, otp, seconds, connAct, params)

    def _setTicketForGraphicDev(self, graphics, otp, seconds, connAct, params):
        graphics.setAttribute('passwd', otp.value)
        if int(seconds) > 0:
            validto = time.strftime('%Y-%m-%dT%H:%M:%S',
                                    time.gmtime(time.time() + float(seconds)))
            graphics.setAttribute('passwdValidTo', validto)
        if connAct is not None and graphics.getAttribute('type') == 'spice':
            graphics.setAttribute('connected', connAct)
        hooks.before_vm_set_ticket(self._domain.xml, self.conf, params)
        try:
            self._dom.updateDeviceFlags(graphics.toxml(), 0)
            disconnectAction = params.get('disconnectAction',
                                          ConsoleDisconnectAction.LOCK_SCREEN)
            self._consoleDisconnectAction = disconnectAction
        except virdomain.TimeoutError as tmo:
            res = response.error('ticketErr', unicode(tmo))
        else:
            hooks.after_vm_set_ticket(self._domain.xml, self.conf, params)
            res = {'status': doneCode}
        return res

    def _reviveTicket(self, newlife):
        """
        Revive an existing ticket, if it has expired or about to expire.
        Needs to be called only if Vm.hasSpice == True
        """
        graphics = self._findGraphicsDeviceXMLByType('spice')  # cannot fail
        validto = max(time.strptime(graphics.getAttribute('passwdValidTo'),
                                    '%Y-%m-%dT%H:%M:%S'),
                      time.gmtime(time.time() + newlife))
        graphics.setAttribute(
            'passwdValidTo', time.strftime('%Y-%m-%dT%H:%M:%S', validto))
        graphics.setAttribute('connected', 'keep')
        self._dom.updateDeviceFlags(graphics.toxml(), 0)

    def _findGraphicsDeviceXMLByType(self, deviceType):
        """
        libvirt (as in 1.2.3) supports only one graphic device per type
        """
        for graphics in _domParseStr(
            self._dom.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE)). \
                childNodes[0].getElementsByTagName('graphics'):
            if graphics.getAttribute('type') == deviceType:
                return graphics
        # no graphics device configured
        return None

    def onIOError(self, blockDevAlias, err, action):
        """
        Called back by IO_ERROR_REASON event

        Old -rhev versions of QEMU provided detailed reason ('eperm', 'eio',
        'enospc', 'eother'), but they are been obsoleted and patches moved
        upstream.
        Newer QEMUs distinguish only between 'enospc' and 'anything else',
        and modern libvirts follow through reporting only two reasons:
        'enospc' or '' (empty string) for 'anything else'.
        """
        reason = err.upper() if err else "EOTHER"

        if action == libvirt.VIR_DOMAIN_EVENT_IO_ERROR_PAUSE:
            self.log.info('abnormal vm stop device %s error %s',
                          blockDevAlias, err)
            with self._confLock:
                self.conf['pauseCode'] = reason
            self._setGuestCpuRunning(False)
            self._logGuestCpuStatus('onIOError')
            if reason == 'ENOSPC':
                if not self.extendDrivesIfNeeded():
                    self.log.info("No VM drives were extended")

            self._send_ioerror_status_event(reason, blockDevAlias)

        elif action == libvirt.VIR_DOMAIN_EVENT_IO_ERROR_REPORT:
            self.log.info('I/O error %s device %s reported to guest OS',
                          reason, blockDevAlias)
        else:
            # we do not support and do not expect other values
            self.log.warning('unexpected action %i on device %s error %s',
                             action, blockDevAlias, reason)

    def _send_ioerror_status_event(self, reason, alias):
        io_error_info = {'alias': alias}
        try:
            drive = self._lookupDeviceByAlias(hwclass.DISK, alias)
        except LookupError:
            self.log.warning('unknown disk alias: %s', alias)
        else:
            io_error_info['name'] = drive.name
            io_error_info['path'] = drive.path

        self.send_status_event(pauseCode=reason, ioerror=io_error_info)

    @property
    def hasSpice(self):
        return (self.conf.get('display') == 'qxl' or
                any(dev['device'] == 'spice'
                    for dev in self.conf.get('devices', [])
                    if dev['type'] == hwclass.GRAPHICS))

    @property
    def name(self):
        return self.conf['vmName']

    def _getPid(self):
        try:
            pid = supervdsm.getProxy().getVmPid(
                self.name.encode('utf-8'))
        except (IOError, ValueError):
            self.log.error('cannot read pid')
            raise
        else:
            if pid <= 0:
                raise ValueError('read invalid pid: %i' % pid)
            return pid

    def _updateDomainDescriptor(self):
        domainXML = self._dom.XMLDesc(0)
        self._domain = DomainDescriptor(domainXML)

    def _ejectFloppy(self):
        if 'volatileFloppy' in self.conf:
            utils.rmFile(self.conf['floppy'])
        self._changeBlockDev('floppy', 'fda', '')

    def releaseVm(self, gracefulAttempts=1):
        """
        Stop VM and release all resources
        """

        # delete the payload devices
        for drive in self._devices[hwclass.DISK]:
            if (hasattr(drive, 'specParams') and
                    'vmPayload' in drive.specParams):
                supervdsm.getProxy().removeFs(drive.path)

        with self._releaseLock:
            if self._released.is_set():
                return response.success()

            # unsetting mirror network will clear both mirroring
            # (on the same network).
            for nic in self._devices[hwclass.NIC]:
                if hasattr(nic, 'portMirroring'):
                    for network in nic.portMirroring[:]:
                        supervdsm.getProxy().unsetPortMirroring(network,
                                                                nic.name)
                        nic.portMirroring.remove(network)

            self.log.info('Release VM resources')
            self.lastStatus = vmstatus.POWERING_DOWN
            # Terminate the VM's creation thread.
            self._incomingMigrationFinished.set()
            self.guestAgent.stop()
            if self._dom.connected:
                result = self._destroyVm(gracefulAttempts)
                if response.is_error(result):
                    return result

            # Wait for any Live Merge cleanup threads.  This will only block in
            # the extremely rare case where a VM is being powered off at the
            # same time as a live merge is being finalized.  These threads
            # finish quickly unless there are storage connection issues.
            for t in self._liveMergeCleanupThreads.values():
                t.join()

            self._cleanup()

            self.cif.irs.inappropriateDevices(self.id)

            hooks.after_vm_destroy(self._domain.xml, self.conf)
            for dev in self._customDevices():
                hooks.after_device_destroy(dev._deviceXML, self.conf,
                                           dev.custom)

            self._released.set()

        return response.success()

    def _destroyVm(self, gracefulAttempts=1):
        for idx in range(gracefulAttempts):
            self.log.info("_destroyVmGraceful attempt #%i", idx)
            res, safe_to_force = self._destroyVmGraceful()
            if not response.is_error(res):
                return res

        if safe_to_force:
            res = self._destroyVmForceful()
        return res

    def _destroyVmGraceful(self):
        safe_to_force = False
        try:
            self._dom.destroyFlags(libvirt.VIR_DOMAIN_DESTROY_GRACEFUL)
        except libvirt.libvirtError as e:
            # after succesfull migraions
            if (self.lastStatus == vmstatus.DOWN and
                    e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN):
                self.log.info("VM '%s' already down and destroyed",
                              self.conf['vmId'])
            else:
                self.log.warning(
                    "Failed to destroy VM '%s' gracefully (error=%i)",
                    self.id, e.get_error_code())
                if e.get_error_code() in (libvirt.VIR_ERR_OPERATION_FAILED,
                                          libvirt.VIR_ERR_SYSTEM_ERROR,):
                    safe_to_force = True
                return response.error('destroyErr'), safe_to_force
        return response.success(), safe_to_force

    def _destroyVmForceful(self):
        try:
            self._dom.destroy()
        except libvirt.libvirtError as e:
            self.log.warning(
                "Failed to destroy VM '%s' forcefully (error=%i)",
                self.id, e.get_error_code())
            return response.error('destroyErr')
        return response.success()

    def _deleteVm(self):
        """
        Clean VM from the system
        """
        try:
            del self.cif.vmContainer[self.id]
        except KeyError:
            self.log.exception("Failed to delete VM %s", self.id)
        else:
            self._cleanupRecoveryFile()
            self.log.debug("Total desktops after destroy of %s is %d",
                           self.conf['vmId'], len(self.cif.vmContainer))

    def destroy(self, gracefulAttempts=1):
        self.log.debug('destroy Called')

        result = self.doDestroy(gracefulAttempts)
        if response.is_error(result):
            return result
        # Clean VM from the system
        self._deleteVm()

        return response.success()

    def doDestroy(self, gracefulAttempts):
        for dev in self._customDevices():
            hooks.before_device_destroy(dev._deviceXML, self.conf,
                                        dev.custom)

        hooks.before_vm_destroy(self._domain.xml, self.conf)
        with self._shutdownLock:
            self._shutdownReason = vmexitreason.ADMIN_SHUTDOWN
        self._destroy_requested.set()

        return self.releaseVm(gracefulAttempts)

    def acpiShutdown(self):
        with self._shutdownLock:
            self._shutdownReason = vmexitreason.ADMIN_SHUTDOWN
        try:
            self._dom.shutdownFlags(libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN)
        except virdomain.NotConnectedError:
            # the VM was already shut off asynchronously,
            # so ignore error and quickly exit
            self.log.warning('failed to invoke acpiShutdown: '
                             'domain not connected')
            return response.error('down')
        else:
            return response.success()

    def acpiReboot(self):
        try:
            self._dom.reboot(libvirt.VIR_DOMAIN_REBOOT_ACPI_POWER_BTN)
        except virdomain.NotConnectedError:
            # the VM was already shut off asynchronously,
            # so ignore error and quickly exit
            self.log.warning('failed to invoke acpiReboot: '
                             'domain not connected')
            return response.error('down')
        else:
            return response.success()

    def setBalloonTarget(self, target):

        if not self._dom.connected:
            return response.error('balloonErr')
        try:
            target = int(target)
            self._dom.setMemory(target)
        except ValueError:
            return self._reportException(
                key='balloonErr', msg='an integer is required for target')
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return self._reportException(key='noVM')
            return self._reportException(key='balloonErr', msg=e.message)
        else:
            for dev in self.conf['devices']:
                if dev['type'] == hwclass.BALLOON and \
                        dev['specParams']['model'] != 'none':
                    dev['target'] = target
            # persist the target value to make it consistent after recovery
            self.saveState()
            return {'status': doneCode}

    def setCpuTuneQuota(self, quota):
        try:
            self._dom.setSchedulerParameters({'vcpu_quota': int(quota)})
        except ValueError:
            return response.error('cpuTuneErr',
                                  'an integer is required for period')
        except libvirt.libvirtError as e:
            return self._reportException(key='cpuTuneErr', msg=e.message)
        else:
            # libvirt may change the value we set, so we must get fresh data
            return self._updateVcpuTuneInfo()

    def setCpuTunePeriod(self, period):
        try:
            self._dom.setSchedulerParameters({'vcpu_period': int(period)})
        except ValueError:
            return response.error('cpuTuneErr',
                                  'an integer is required for period')
        except libvirt.libvirtError as e:
            return self._reportException(key='cpuTuneErr', msg=e.message)
        else:
            # libvirt may change the value we set, so we must get fresh data
            return self._updateVcpuTuneInfo()

    def _updateVcpuTuneInfo(self):
        try:
            self._vcpuTuneInfo = self._dom.schedulerParameters()
        except libvirt.libvirtError as e:
            return self._reportException(key='cpuTuneErr', msg=e.message)
        else:
            return {'status': doneCode}

    def _reportException(self, key, msg=None):
        """
        Convert an exception to an error status.
        This method should be called only within exception-handling context.
        """
        self.log.exception("Operation failed")
        return response.error(key, msg)

    def _setWriteWatermarks(self):
        """
        Define when to receive an event about high write to guest image
        Currently unavailable by libvirt.
        """
        pass

    def onLibvirtLifecycleEvent(self, event, detail, opaque):
        self.log.debug('event %s detail %s opaque %s',
                       eventToString(event), detail, opaque)
        if event == libvirt.VIR_DOMAIN_EVENT_STOPPED:
            if (detail == libvirt.VIR_DOMAIN_EVENT_STOPPED_MIGRATED and
                    self.lastStatus == vmstatus.MIGRATION_SOURCE):
                hooks.after_vm_migrate_source(self._domain.xml, self.conf)
                for dev in self._customDevices():
                    hooks.after_device_migrate_source(
                        dev._deviceXML, self.conf, dev.custom)
            elif (detail == libvirt.VIR_DOMAIN_EVENT_STOPPED_SAVED and
                    self.lastStatus == vmstatus.SAVING_STATE):
                hooks.after_vm_hibernate(self._domain.xml, self.conf)
            else:
                if detail == libvirt.VIR_DOMAIN_EVENT_STOPPED_SHUTDOWN:
                    with self._shutdownLock:
                        if self._shutdownReason is None:
                            # do not overwrite admin shutdown, if present
                            self._shutdownReason = vmexitreason.USER_SHUTDOWN
                self._onQemuDeath()
        elif event == libvirt.VIR_DOMAIN_EVENT_SUSPENDED:
            self._setGuestCpuRunning(False)
            self._logGuestCpuStatus('onSuspend')
            if detail == libvirt.VIR_DOMAIN_EVENT_SUSPENDED_PAUSED:
                # Libvirt sometimes send the SUSPENDED/SUSPENDED_PAUSED event
                # after RESUMED/RESUMED_MIGRATED (when VM status is PAUSED
                # when migration completes, see qemuMigrationFinish function).
                # In this case self._dom is disconnected because the function
                # _completeIncomingMigration didn't update it yet.
                try:
                    domxml = self._dom.XMLDesc(0)
                except virdomain.NotConnectedError:
                    pass
                else:
                    hooks.after_vm_pause(domxml, self.conf)

        elif event == libvirt.VIR_DOMAIN_EVENT_RESUMED:
            self._setGuestCpuRunning(True)
            self._logGuestCpuStatus('onResume')
            if detail == libvirt.VIR_DOMAIN_EVENT_RESUMED_UNPAUSED:
                # This is not a real solution however the safest way to handle
                # this for now. Ultimately we need to change the way how we are
                # creating self._dom.
                # The event handler delivers the domain instance in the
                # callback however we do not use it.
                try:
                    domxml = self._dom.XMLDesc(0)
                except virdomain.NotConnectedError:
                    pass
                else:
                    hooks.after_vm_cont(domxml, self.conf)
            elif (detail == libvirt.VIR_DOMAIN_EVENT_RESUMED_MIGRATED and
                  self.lastStatus == vmstatus.MIGRATION_DESTINATION):
                self._incomingMigrationFinished.set()

    def _updateDevicesDomxmlCache(self, xml):
        """
            Devices cache their device's XML, which is used for per-device
            hooks. The cache is lost when a VM migrates because that info
            isn't sent, and so the cache needs to be updated at the
            destination.
            We update the cache by finding each device in the dom xml.
        """

        aliasToDevice = {}
        for devType in self._devices:
            for dev in self._devices[devType]:
                if hasattr(dev, 'alias'):
                    aliasToDevice[dev.alias] = dev
                elif devType == hwclass.WITHOUT_ALIAS:
                    # we expect these failures, we don't log
                    # to not confuse the user
                    pass
                else:
                    self.log.error("Alias not found for device type %s "
                                   "during migration at destination host" %
                                   devType)

        for deviceXML in vmxml.all_devices(xml):
            aliasElement = deviceXML.getElementsByTagName('alias')
            if aliasElement:
                alias = aliasElement[0].getAttribute('name')

                if alias in aliasToDevice:
                    aliasToDevice[alias]._deviceXML = deviceXML.toxml()
            elif deviceXML.tagName == hwclass.GRAPHICS:
                # graphics device do not have aliases, must match by type
                graphicsType = deviceXML.getAttribute('type')
                for devObj in self._devices[hwclass.GRAPHICS]:
                    if devObj.device == graphicsType:
                        devObj._deviceXML = deviceXML.toxml()

    def waitForMigrationDestinationPrepare(self):
        """Wait until paths are prepared for migration destination"""
        # Wait for the VM to start its creation. There is no reason to start
        # the timed waiting for path preparation before the work has started.
        self.log.debug('migration destination: waiting for VM creation')
        self._vmCreationEvent.wait()
        prepareTimeout = self._loadCorrectedTimeout(
            config.getint('vars', 'migration_listener_timeout'), doubler=5)
        self.log.debug('migration destination: waiting %ss '
                       'for path preparation', prepareTimeout)
        self._pathsPreparedEvent.wait(prepareTimeout)
        if not self._pathsPreparedEvent.isSet():
            self.log.debug('Timeout while waiting for path preparation')
            return False
        with self._confLock:
            srcDomXML = self.conf.pop('_srcDomXML').encode('utf-8')
        self._updateDevicesDomxmlCache(srcDomXML)

        for dev in self._customDevices():
            hooks.before_device_migrate_destination(
                dev._deviceXML, self.conf, dev.custom)

        hooks.before_vm_migrate_destination(srcDomXML, self.conf)
        return True

    def getBlockJob(self, drive):
        for job in self.conf['_blockJobs'].values():
            if all([bool(drive[x] == job['disk'][x])
                    for x in ('imageID', 'domainID', 'volumeID')]):
                return job
        raise LookupError("No block job found for drive '%s'", drive.name)

    def trackBlockJob(self, jobID, drive, base, top, strategy):
        driveSpec = dict((k, drive[k]) for k in
                         ('poolID', 'domainID', 'imageID', 'volumeID'))
        with self._confLock:
            try:
                job = self.getBlockJob(drive)
            except LookupError:
                newJob = {'jobID': jobID, 'disk': driveSpec,
                          'baseVolume': base, 'topVolume': top,
                          'strategy': strategy, 'blockJobType': 'commit'}
                self.conf['_blockJobs'][jobID] = newJob
            else:
                self.log.error("Cannot add block job %s.  A block job with id "
                               "%s already exists for image %s", jobID,
                               job['jobID'], drive['imageID'])
                raise BlockJobExistsError()
        self.saveState()

    def untrackBlockJob(self, jobID):
        with self._confLock:
            try:
                del self.conf['_blockJobs'][jobID]
            except KeyError:
                # If there was contention on the confLock, this may have
                # already been removed
                return False
        self.saveState()
        return True

    def _activeLayerCommitReady(self, jobInfo):
        try:
            pivot = libvirt.VIR_DOMAIN_BLOCK_JOB_TYPE_ACTIVE_COMMIT
        except AttributeError:
            return False
        if (jobInfo['cur'] == jobInfo['end'] and jobInfo['type'] == pivot):
            return True
        return False

    @property
    def hasVmJobs(self):
        """
        Return True if there are VM jobs to monitor
        """
        with self._jobsLock:
            # we always do a full check the first time we run.
            # This may be wasteful on normal flow,
            # but covers pretty nicely the recovering flow.
            return self._vmJobs is None or bool(self.conf['_blockJobs'])

    def updateVmJobs(self):
        self._vmJobs = self.queryBlockJobs()

    def queryBlockJobs(self):
        def startCleanup(job, drive, needPivot):
            t = LiveMergeCleanupThread(self, job, drive, needPivot)
            t.start()
            self._liveMergeCleanupThreads[job['jobID']] = t

        jobsRet = {}
        # We need to take the jobs lock here to ensure that we don't race with
        # another call to merge() where the job has been recorded but not yet
        # started.
        with self._jobsLock:
            for storedJob in self.conf['_blockJobs'].values():
                jobID = storedJob['jobID']
                cleanThread = self._liveMergeCleanupThreads.get(jobID)
                if cleanThread and cleanThread.isSuccessful():
                    # Handle successful jobs early because the job just needs
                    # to be untracked and the stored disk info might be stale
                    # anyway (ie. after active layer commit).
                    self.untrackBlockJob(jobID)
                    continue

                drive = self._findDriveByUUIDs(storedJob['disk'])
                entry = {'id': jobID, 'jobType': 'block',
                         'blockJobType': storedJob['blockJobType'],
                         'bandwidth': 0, 'cur': '0', 'end': '0',
                         'imgUUID': storedJob['disk']['imageID']}

                liveInfo = None
                if 'gone' not in storedJob:
                    try:
                        liveInfo = self._dom.blockJobInfo(drive.name, 0)
                    except libvirt.libvirtError:
                        self.log.exception("Error getting block job info")
                        jobsRet[jobID] = entry
                        continue

                if liveInfo:
                    entry['bandwidth'] = liveInfo['bandwidth']
                    entry['cur'] = str(liveInfo['cur'])
                    entry['end'] = str(liveInfo['end'])
                    doPivot = self._activeLayerCommitReady(liveInfo)
                else:
                    # Libvirt has stopped reporting this job so we know it will
                    # never report it again.
                    doPivot = False
                    storedJob['gone'] = True
                if not liveInfo or doPivot:
                    if not cleanThread:
                        # There is no cleanup thread so the job must have just
                        # ended.  Spawn an async cleanup.
                        startCleanup(storedJob, drive, doPivot)
                    elif cleanThread.isAlive():
                        # Let previously started cleanup thread continue
                        self.log.debug("Still waiting for block job %s to be "
                                       "synchronized", jobID)
                    elif not cleanThread.isSuccessful():
                        # At this point we know the thread is not alive and the
                        # cleanup failed.  Retry it with a new thread.
                        startCleanup(storedJob, drive, doPivot)
                jobsRet[jobID] = entry
        return jobsRet

    def merge(self, driveSpec, baseVolUUID, topVolUUID, bandwidth, jobUUID):
        if not caps.getLiveMergeSupport():
            self.log.error("Live merge is not supported on this host")
            return response.error('mergeErr')

        bandwidth = int(bandwidth)
        if jobUUID is None:
            jobUUID = str(uuid.uuid4())

        try:
            drive = self._findDriveByUUIDs(driveSpec)
        except LookupError:
            return response.error('imageErr')

        # Check that libvirt exposes full volume chain information
        chains = self._driveGetActualVolumeChain([drive])
        if drive['alias'] not in chains:
            self.log.error("merge: libvirt does not support volume chain "
                           "monitoring.  Unable to perform live merge.")
            return response.error('mergeErr')

        base = top = None
        for v in drive.volumeChain:
            if v['volumeID'] == baseVolUUID:
                base = v['path']
            if v['volumeID'] == topVolUUID:
                top = v['path']
        if base is None:
            self.log.error("merge: base volume '%s' not found", baseVolUUID)
            return response.error('mergeErr')
        if top is None:
            self.log.error("merge: top volume '%s' not found", topVolUUID)
            return response.error('mergeErr')

        try:
            baseInfo = self._getVolumeInfo(drive.domainID, drive.poolID,
                                           drive.imageID, baseVolUUID)
            topInfo = self._getVolumeInfo(drive.domainID, drive.poolID,
                                          drive.imageID, topVolUUID)
        except StorageUnavailableError:
            self.log.error("Unable to get volume information")
            return errCode['mergeErr']

        # If base is a shared volume then we cannot allow a merge.  Otherwise
        # We'd corrupt the shared volume for other users.
        if baseInfo['voltype'] == 'SHARED':
            self.log.error("Refusing to merge into a shared volume")
            return errCode['mergeErr']

        # Indicate that we expect libvirt to maintain the relative paths of
        # backing files.  This is necessary to ensure that a volume chain is
        # visible from any host even if the mountpoint is different.
        flags = libvirt.VIR_DOMAIN_BLOCK_COMMIT_RELATIVE

        if topVolUUID == drive.volumeID:
            # Pass a flag to libvirt to indicate that we expect a two phase
            # block job.  In the first phase, data is copied to base.  Once
            # completed, an event is raised to indicate that the job has
            # transitioned to the second phase.  We must then tell libvirt to
            # pivot to the new active layer (baseVolUUID).
            flags |= libvirt.VIR_DOMAIN_BLOCK_COMMIT_ACTIVE

        # Make sure we can merge into the base in case the drive was enlarged.
        if not self._can_merge_into(drive, baseInfo, topInfo):
            return errCode['destVolumeTooSmall']

        # Take the jobs lock here to protect the new job we are tracking from
        # being cleaned up by queryBlockJobs() since it won't exist right away
        with self._jobsLock:
            try:
                self.trackBlockJob(jobUUID, drive, baseVolUUID, topVolUUID,
                                   'commit')
            except BlockJobExistsError:
                self.log.error("A block job is already active on this disk")
                return response.error('mergeErr')
            self.log.info("Starting merge with jobUUID='%s'", jobUUID)

            try:
                ret = self._dom.blockCommit(drive.path, base, top, bandwidth,
                                            flags)
                if ret != 0:
                    raise RuntimeError("blockCommit failed rc:%i", ret)
            except (RuntimeError, libvirt.libvirtError):
                self.log.exception("Live merge failed (job: %s)", jobUUID)
                self.untrackBlockJob(jobUUID)
                return response.error('mergeErr')

        # blockCommit will cause data to be written into the base volume.
        # Perform an initial extension to ensure there is enough space to
        # copy all the required data.  Normally we'd use monitoring to extend
        # the volume on-demand but internal watermark information is not being
        # reported by libvirt so we must do the full extension up front.  In
        # the worst case, the allocated size of 'base' should be increased by
        # the allocated size of 'top' plus one additional chunk to accomodate
        # additional writes to 'top' during the live merge operation.
        if drive.chunked and baseInfo['format'] == 'COW':
            capacity, alloc, physical = self._getExtendInfo(drive)
            baseSize = int(baseInfo['apparentsize'])
            topSize = int(topInfo['apparentsize'])
            maxAlloc = baseSize + topSize
            self.extendDriveVolume(drive, baseVolUUID, maxAlloc, capacity)

        # Trigger the collection of stats before returning so that callers
        # of getVmStats after this returns will see the new job
        self.updateVmJobs()

        return {'status': doneCode}

    def _can_merge_into(self, drive, base_info, top_info):
        # If the drive was resized the top volume could be larger than the
        # base volume.  Libvirt can handle this situation for file-based
        # volumes and block qcow volumes (where extension happens dynamically).
        # Raw block volumes cannot be extended by libvirt so we require ovirt
        # engine to extend them before calling merge.  Check here.
        if not drive.blockDev or base_info['format'] != 'RAW':
            return True

        if int(base_info['capacity']) < int(top_info['capacity']):
            self.log.warning("The base volume is undersized and cannot be "
                             "extended (base capacity: %s, top capacity: %s)",
                             base_info['capacity'], top_info['capacity'])
            return False
        return True

    def _diskXMLGetVolumeChainInfo(self, diskXML, drive):
        def find_element_by_name(doc, name):
            for child in doc.childNodes:
                if child.nodeName == name:
                    return child
            return None

        def pathToVolID(drive, path):
            for vol in drive.volumeChain:
                if os.path.realpath(vol['path']) == os.path.realpath(path):
                    return vol['volumeID']
            raise LookupError("Unable to find VolumeID for path '%s'", path)

        volChain = []
        while True:
            sourceXML = find_element_by_name(diskXML, 'source')
            if not sourceXML:
                break
            sourceAttr = ('file', 'dev')[drive.blockDev]
            path = sourceXML.getAttribute(sourceAttr)

            # TODO: Allocation information is not available in the XML.  Switch
            # to the new interface once it becomes available in libvirt.
            alloc = None
            bsXML = find_element_by_name(diskXML, 'backingStore')
            if not bsXML:
                self.log.warning("<backingStore/> missing from backing "
                                 "chain for drive %s", drive.name)
                break
            diskXML = bsXML
            entry = VolumeChainEntry(pathToVolID(drive, path), path, alloc)
            volChain.insert(0, entry)
        return volChain or None

    def _driveGetActualVolumeChain(self, drives):
        def lookupDeviceXMLByAlias(domXML, targetAlias):
            for deviceXML, alias in _devicesWithAlias(domXML):
                if alias == targetAlias:
                    return deviceXML
            raise LookupError("Unable to find matching XML for device %s",
                              targetAlias)

        ret = {}
        self._updateDomainDescriptor()
        for drive in drives:
            alias = drive['alias']
            diskXML = lookupDeviceXMLByAlias(self._domain.xml, alias)
            volChain = self._diskXMLGetVolumeChainInfo(diskXML, drive)
            if volChain:
                ret[alias] = volChain
        return ret

    def _syncVolumeChain(self, drive):
        def getVolumeInfo(device, volumeID):
            for info in device['volumeChain']:
                if info['volumeID'] == volumeID:
                    return utils.picklecopy(info)

        if not isVdsmImage(drive):
            self.log.debug("Skipping drive '%s' which is not a vdsm image",
                           drive.name)
            return

        curVols = [x['volumeID'] for x in drive.volumeChain]
        chains = self._driveGetActualVolumeChain([drive])
        try:
            chain = chains[drive['alias']]
        except KeyError:
            self.log.debug("Unable to determine volume chain. Skipping volume "
                           "chain synchronization for drive %s", drive.name)
            return

        volumes = [entry.uuid for entry in chain]
        activePath = chain[-1].path
        self.log.debug("vdsm chain: %s, libvirt chain: %s", curVols, volumes)

        # Ask the storage to sync metadata according to the new chain
        res = self.cif.irs.imageSyncVolumeChain(drive.domainID, drive.imageID,
                                                drive['volumeID'], volumes)
        if res['status']['code'] != 0:
            self.log.error("Unable to synchronize volume chain to storage")
            raise StorageUnavailableError()

        if (set(curVols) == set(volumes)):
            return

        volumeID = volumes[-1]
        res = self.cif.irs.getVolumeInfo(drive.domainID, drive.poolID,
                                         drive.imageID, volumeID)
        if res['status']['code'] != 0:
            self.log.error("Unable to get info of volume %s (domain: %s image:"
                           " %s)", volumeID, drive.domainID, drive.imageID)
            raise RuntimeError("Unable to get volume info")
        driveFormat = res['info']['format'].lower()

        # Sync this VM's data strctures.  Ugh, we're storing the same info in
        # two places so we need to change it twice.
        device = self._lookupConfByPath(drive['path'])
        if drive.volumeID != volumeID:
            # If the active layer changed:
            #  Update the disk path, volumeID, volumeInfo, and format members
            volInfo = getVolumeInfo(device, volumeID)

            # Path must be set with the value being used by libvirt
            device['path'] = drive.path = volInfo['path'] = activePath
            device['format'] = drive.format = driveFormat
            device['volumeID'] = drive.volumeID = volumeID
            device['volumeInfo'] = drive.volumeInfo = volInfo
            for v in device['volumeChain']:
                if v['volumeID'] == volumeID:
                    v['path'] = activePath

        # Remove any components of the volumeChain which are no longer present
        newChain = [x for x in device['volumeChain']
                    if x['volumeID'] in volumes]
        device['volumeChain'] = drive.volumeChain = newChain

    def _fixLegacyGraphicsConf(self):
        with self._confLock:
            if not vmdevices.graphics.getFirstGraphics(self.conf):
                self.conf['devices'].extend(self.getConfGraphics())

    def _fixLegacyRngConf(self):
        def _is_legacy_rng_device_conf(dev):
            """
            Returns True if dev is a legacy (3.5) RNG device conf,
            False otherwise.
            """
            return dev['type'] == hwclass.RNG and (
                'specParams' not in dev or
                'source' not in dev['specParams']
            )

        with self._confLock:
            self._devices[hwclass.RNG] = [dev for dev
                                          in self._devices[hwclass.RNG]
                                          if 'source' in dev.specParams]
            self.conf['devices'] = [dev for dev
                                    in self.conf['devices']
                                    if not _is_legacy_rng_device_conf(dev)]

    def getDiskDevices(self):
        return self._devices[hwclass.DISK]

    def getNicDevices(self):
        return self._devices[hwclass.NIC]

    def getBalloonDevicesConf(self):
        for dev in self.conf['devices']:
            if dev['type'] == hwclass.BALLOON:
                yield dev

    @property
    def sdIds(self):
        """
        Returns a list of the ids of the storage domains in use by the VM.
        """
        return set(device.domainID
                   for device in self._devices[hwclass.DISK]
                   if device['device'] == 'disk' and isVdsmImage(device))

    def _logGuestCpuStatus(self, reason):
        self.log.info('CPU %s: %s',
                      'running' if self._guestCpuRunning else 'stopped',
                      reason)

    def _setUnresponsiveIfTimeout(self, stats, statsAge):
        if (not self.isMigrating()
                and statsAge > config.getint('vars', 'vm_command_timeout')
                and stats['monitorResponse'] != '-1'):
            self.log.warning('monitor become unresponsive'
                             ' (command timeout, age=%s)',
                             statsAge)
            stats['monitorResponse'] = '-1'

    def updateNumaInfo(self):
        self._numaInfo = numa.getVmNumaNodeRuntimeInfo(self)

    @property
    def hasGuestNumaNode(self):
        return 'guestNumaNodes' in self.conf

    # Accessing storage

    def _getVolumeSize(self, domainID, poolID, imageID, volumeID):
        """ Return volume size info by accessing storage """
        res = self.cif.irs.getVolumeSize(domainID, poolID, imageID, volumeID)
        if res['status']['code'] != 0:
            raise StorageUnavailableError(
                "Unable to get volume size for domain %s volume %s" %
                (domainID, volumeID))
        return VolumeSize(int(res['apparentsize']), int(res['truesize']))

    def _getVolumeInfo(self, domainID, poolID, imageID, volumeID):
        res = self.cif.irs.getVolumeInfo(domainID, poolID, imageID, volumeID)
        if res['status']['code'] != 0:
            raise StorageUnavailableError(
                "Unable to get volume info for domain %s volume %s" %
                (domainID, volumeID))
        return res['info']

    def _setVolumeSize(self, domainID, poolID, imageID, volumeID, size):
        res = self.cif.irs.setVolumeSize(domainID, poolID, imageID, volumeID,
                                         size)
        if res['status']['code'] != 0:
            raise StorageUnavailableError(
                "Unable to set volume size to %s for domain %s volume %s" %
                (size, domainID, volumeID))


class LiveMergeCleanupThread(threading.Thread):
    def __init__(self, vm, job, drive, doPivot):
        threading.Thread.__init__(self)
        self.setDaemon(True)
        self.vm = vm
        self.job = job
        self.drive = drive
        self.doPivot = doPivot
        self.success = False

    def tryPivot(self):
        # We call imageSyncVolumeChain which will mark the current leaf
        # ILLEGAL.  We do this before requesting a pivot so that we can
        # properly recover the VM in case we crash.  At this point the
        # active layer contains the same data as its parent so the ILLEGAL
        # flag indicates that the VM should be restarted using the parent.
        newVols = [vol['volumeID'] for vol in self.drive.volumeChain
                   if vol['volumeID'] != self.drive.volumeID]
        self.vm.cif.irs.imageSyncVolumeChain(self.drive.domainID,
                                             self.drive.imageID,
                                             self.drive['volumeID'], newVols)

        # A pivot changes the top volume being used for the VM Disk.  Until
        # we can correct our metadata following the pivot we should not
        # attempt to collect disk stats.
        # TODO: Stop collection only for the live merge disk
        self.vm.stopDisksStatsCollection()

        self.vm.log.info("Requesting pivot to complete active layer commit "
                         "(job %s)", self.job['jobID'])
        try:
            flags = libvirt.VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT
            ret = self.vm._dom.blockJobAbort(self.drive.name, flags)
        except:
            self.vm.startDisksStatsCollection()
            raise
        else:
            if ret != 0:
                self.vm.log.error("Pivot failed for job %s (rc=%i)",
                                  self.job['jobID'], ret)
                raise RuntimeError("pivot failed")
            self._waitForXMLUpdate()
        self.vm.log.info("Pivot completed (job %s)", self.job['jobID'])

    def update_base_size(self):
        # If the drive size was extended just after creating the snapshot which
        # we are removing, the size of the top volume might be larger than the
        # size of the base volume.  In that case libvirt has enlarged the base
        # volume automatically as part of the blockCommit operation.  Update
        # our metadata to reflect this change.
        topVolUUID = self.job['topVolume']
        baseVolUUID = self.job['baseVolume']
        topVolInfo = self.vm._getVolumeInfo(self.drive.domainID,
                                            self.drive.poolID,
                                            self.drive.imageID, topVolUUID)
        self.vm._setVolumeSize(self.drive.domainID, self.drive.poolID,
                               self.drive.imageID, baseVolUUID,
                               topVolInfo['capacity'])

    @utils.traceback()
    def run(self):
        self.update_base_size()
        if self.doPivot:
            self.tryPivot()
        self.vm.log.info("Synchronizing volume chain after live merge "
                         "(job %s)", self.job['jobID'])
        self.vm._syncVolumeChain(self.drive)
        if self.doPivot:
            self.vm.startDisksStatsCollection()
        self.success = True
        self.vm.log.info("Synchronization completed (job %s)",
                         self.job['jobID'])

    def isSuccessful(self):
        """
        Returns True if this phase completed successfully.
        """
        return self.success

    def _waitForXMLUpdate(self):
        # Libvirt version 1.2.8-16.el7_1.2 introduced a bug where the
        # synchronous call to blockJobAbort will return before the domain XML
        # has been updated.  This makes it look like the pivot failed when it
        # actually succeeded.  This means that vdsm state will not be properly
        # synchronized and we may start the vm with a stale volume in the
        # future.  See https://bugzilla.redhat.com/show_bug.cgi?id=1202719 for
        # more details.
        # TODO: Remove once we depend on a libvirt with this bug fixed.

        # We expect libvirt to show that the original leaf has been removed
        # from the active volume chain.
        origVols = sorted([x['volumeID'] for x in self.drive.volumeChain])
        expectedVols = origVols[:]
        expectedVols.remove(self.drive.volumeID)

        alias = self.drive['alias']
        self.vm.log.info("Waiting for libvirt to update the XML after pivot "
                         "of drive %s completed", alias)
        while True:
            # This operation should complete in either one or two iterations of
            # this loop.  Until libvirt updates the XML there is nothing to do
            # but wait.  While we wait we continue to tell engine that the job
            # is ongoing.  If we are still in this loop when the VM is powered
            # off, the merge will be resolved manually by engine using the
            # reconcileVolumeChain verb.
            chains = self.vm._driveGetActualVolumeChain([self.drive])
            if alias not in chains.keys():
                raise RuntimeError("Failed to retrieve volume chain for "
                                   "drive %s.  Pivot failed.", alias)
            curVols = sorted([entry.uuid for entry in chains[alias]])

            if curVols == origVols:
                time.sleep(1)
            elif curVols == expectedVols:
                self.vm.log.info("The XML update has been completed")
                break
            else:
                self.vm.log.error("Bad volume chain found for drive %s. "
                                  "Previous chain: %s, Expected chain: %s, "
                                  "Actual chain: %s", alias, origVols,
                                  expectedVols, curVols)
                raise RuntimeError("Bad volume chain found")


def _devicesWithAlias(domXML):
    return vmxml.filter_devices_with_alias(vmxml.all_devices(domXML))
