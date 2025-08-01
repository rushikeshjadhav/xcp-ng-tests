from __future__ import annotations

import logging
import os
import tempfile

import lib.commands as commands
import lib.efi as efi
from lib.basevm import BaseVM
from lib.common import (
    PackageManagerEnum,
    expand_scope_relative_nodeid,
    parse_xe_dict,
    safe_split,
    shortened_nodeid,
    strtobool,
    wait_for,
    wait_for_not,
)
from lib.snapshot import Snapshot
from lib.vbd import VBD
from lib.vif import VIF

from typing import TYPE_CHECKING, List, Literal, Optional, Union, overload

if TYPE_CHECKING:
    from lib.host import Host

class VM(BaseVM):
    def __init__(self, uuid: str, host: 'Host'):
        super().__init__(uuid, host)
        self.ip: Optional[str] = None
        self.previous_host = None # previous host when migrated or being migrated
        self.is_windows = self.param_get('platform', 'device_id', accept_unknown_key=True) == '0002'
        self.is_uefi = self.param_get('HVM-boot-params', 'firmware', accept_unknown_key=True) == 'uefi'

    def power_state(self) -> str:
        return self.param_get('power-state')

    def is_running(self):
        return self.power_state() == 'running'

    def is_halted(self):
        return self.power_state() == 'halted'

    def is_suspended(self):
        return self.power_state() == 'suspended'

    def is_paused(self):
        return self.power_state() == 'paused'

    # `on` can be an host name-label or UUID
    def start(self, on=None):
        msg_starts_on = f" (on host {on})" if on else ""
        logging.info("Start VM" + msg_starts_on)
        args = {'uuid': self.uuid}
        if on is not None:
            args['on'] = on
        return self.host.xe('vm-start', args)

    def shutdown(self, force=False, verify=False, force_if_fails=False):
        assert not (force and force_if_fails), "force and force_if_fails cannot be both True"
        logging.info("Shutdown VM" + (" (force)" if force else ""))

        try:
            ret = self.host.xe('vm-shutdown', {'uuid': self.uuid, 'force': force})
            if verify:
                wait_for(self.is_halted, "Wait for VM halted")
        except Exception as e:
            if force_if_fails:
                logging.warning("Shutdown failed: %s" % e)
                ret = self.shutdown(force=True, verify=verify)
            else:
                raise

        return ret

    def reboot(self, force=False, verify=False):
        logging.info("Reboot VM")
        ret = self.host.xe('vm-reboot', {'uuid': self.uuid, 'force': force})
        if verify:
            # No need to verify that the reboot actually happened because the xe command
            # does that for us already (it only finishes once the reboot started).
            # So we just wait for the VM to be operational again
            self.wait_for_vm_running_and_ssh_up()
        return ret

    def try_get_and_store_ip(self):
        ip = self.param_get('networks', '0/ip', accept_unknown_key=True)

        # An IP that starts with 169.254. is not a real routable IP.
        # VMs may return such an IP before they get an actual one from DHCP.
        if not ip or ip.startswith('169.254.'):
            return False
        else:
            logging.info("VM IP: %s" % ip)
            self.ip = ip
            return True

    @overload
    def ssh(self, cmd: Union[str, List[str]], *, check: bool = True, simple_output: Literal[True] = True,
            background: Literal[False] = False, decode: Literal[True] = True) -> str:
        ...

    @overload
    def ssh(self, cmd: Union[str, List[str]], *, check: bool = True, simple_output: Literal[True] = True,
            background: Literal[False] = False, decode: Literal[False]) -> bytes:
        ...

    @overload
    def ssh(self, cmd: Union[str, List[str]], *, check: bool = True, simple_output: Literal[False],
            background: Literal[False] = False, decode: bool = True) -> commands.SSHResult:
        ...

    @overload
    def ssh(self, cmd: Union[str, List[str]], *, check: bool = True, simple_output: bool = True,
            background: Literal[True], decode: bool = True) -> None:
        ...

    @overload
    def ssh(self, cmd: Union[str, List[str]], *, check=True, simple_output=True, background=False, decode=True) \
            -> Union[str, bytes, commands.SSHResult, None]:
        ...

    def ssh(self, cmd, *, check=True, simple_output=True, background=False, decode=True):
        # raises by default for any nonzero return code
        assert self.ip is not None
        return commands.ssh(self.ip, cmd, check=check, simple_output=simple_output, background=background,
                            decode=decode)

    def ssh_with_result(self, cmd) -> commands.SSHResult:
        # doesn't raise if the command's return is nonzero, unless there's a SSH error
        return commands.ssh_with_result(self.ip, cmd)

    def scp(self, src, dest, check=True, suppress_fingerprint_warnings=True, local_dest=False):
        # Stop execution if scp() is used on Windows VMs as some OpenSSH releases for Windows don't
        # have support for the scp legacy protocol. Callers must use vm.sftp_put() instead
        assert not self.is_windows, "You cannot use scp() on Windows VMs. Please use vm.sftp_put() instead"

        return commands.scp(
            self.ip, src, dest, check=check,
            suppress_fingerprint_warnings=suppress_fingerprint_warnings,
            local_dest=local_dest
        )

    def sftp_put(self, src, dest, check=True, suppress_fingerprint_warnings=True):
        cmd = f"put {src} {dest}"
        return commands.sftp(self.ip, [cmd], check, suppress_fingerprint_warnings)

    def is_ssh_up(self):
        try:
            return self.ssh_with_result(['true']).returncode == 0
        except commands.SSHCommandFailed:
            # probably not up yet
            return False

    def is_management_agent_up(self):
        return (
            self.param_get("PV-drivers-version", "major", accept_unknown_key=True) is not None
            # HACK: workaround for Windows XS guest agents not updating major version after resume
            or self.param_get("PV-drivers-version", "xenbus", accept_unknown_key=True) is not None
        )

    def wait_for_os_booted(self):
        wait_for(self.is_running, "Wait for VM running")
        # waiting for the IP:
        # - allows to make sure the OS actually started (on VMs that have the management agent)
        # - allows to store the IP for future use in the VM object
        wait_for(self.try_get_and_store_ip, "Wait for VM IP", timeout_secs=5 * 60)
        # now wait also for the management agent to have started
        wait_for(self.is_management_agent_up, "Wait for management agent up")

    def wait_for_vm_running_and_ssh_up(self):
        self.wait_for_os_booted()
        wait_for(self.is_ssh_up, "Wait for SSH up")

    def ssh_touch_file(self, filepath):
        logging.info("Create file on VM (%s)" % filepath)
        self.ssh(['touch', filepath])
        if not self.is_windows:
            self.ssh(['sync', filepath])
        logging.info("Check file created")
        self.ssh(['test -f ' + filepath])

    def suspend(self, verify=False):
        logging.info("Suspend VM")
        self.host.xe('vm-suspend', {'uuid': self.uuid})
        if verify:
            wait_for(self.is_suspended, "Wait for VM suspended")

    def resume(self):
        logging.info("Resume VM")
        self.host.xe('vm-resume', {'uuid': self.uuid})

    def pause(self, verify=False):
        logging.info("Pause VM")
        self.host.xe('vm-pause', {'uuid': self.uuid})
        if verify:
            wait_for(self.is_paused, "Wait for VM paused")

    def unpause(self):
        logging.info("Unpause VM")
        self.host.xe('vm-unpause', {'uuid': self.uuid})

    def _disk_list(self):
        return self.host.xe('vm-disk-list', {'uuid': self.uuid, 'vbd-params': ''}, minimal=True)

    def destroy(self, verify=False):
        if not self.is_halted():
            self.shutdown(force=True)

        # Note: not using xe vm-uninstall (which would be convenient) because it leaves a VDI behind
        # See https://github.com/xapi-project/xen-api/issues/4145
        for vdi_uuid in self.vdi_uuids():
            self.destroy_vdi(vdi_uuid)
        self.host.xe('vm-destroy', {'uuid': self.uuid})

        if verify:
            wait_for_not(self.exists, "Wait for VM destroyed")

    def exists(self):
        return self.host.pool_has_vm(self.uuid)

    def exists_on_previous_pool(self):
        assert self.previous_host is not None
        return self.previous_host.pool_has_vm(self.uuid)

    def migrate(self, target_host, sr=None, network=None):
        msg = "Migrate VM to host %s" % target_host
        params = {
            'uuid': self.uuid,
            'host-uuid': target_host.uuid,
            'live': self.is_running()
        }
        cross_pool = self.host.pool.uuid != target_host.pool.uuid
        if sr is not None:
            if self.get_sr().uuid == sr.uuid:
                # Same SR, no need to migrate storage
                sr = None
            else:
                msg += " (SR: %s)" % sr.uuid
        if network is not None:
            msg += " (Network: %s)" % network
        logging.info(msg)

        storage_motion = cross_pool or sr is not None or network is not None
        if storage_motion:
            remote_master = target_host.pool.master
            params['remote-master'] = remote_master.hostname_or_ip
            params['remote-username'] = remote_master.user
            params['remote-password'] = remote_master.password

            if sr is not None:
                sr_uuid = sr.uuid
            else:
                sr_uuid = target_host.xe('pool-param-get', {'uuid': target_host.pool.uuid, 'param-name': 'default-SR'})
            vdi_map = {}
            for vdi_uuid in self.vdi_uuids():
                vdi_map[vdi_uuid] = sr_uuid
            params['vdi'] = vdi_map

            if cross_pool:
                # VIF mapping is only required for cross pool migration
                if network is None:
                    network = remote_master.management_network()

                vif_map = {}
                for vif in self.vifs():
                    vif_map[vif.uuid] = network
                params['vif'] = vif_map

        self.host.xe('vm-migrate', params)

        self.previous_host = self.host
        self.host = target_host

    def snapshot(self, ignore_vdis=None):
        logging.info("Snapshot VM")
        args = {'uuid': self.uuid, 'new-name-label': 'Snapshot of %s' % self.uuid}
        if ignore_vdis:
            args['ignore-vdi-uuids'] = ','.join(ignore_vdis)
        return Snapshot(self.host.xe('vm-snapshot', args), self.host)

    def checkpoint(self) -> Snapshot:
        logging.info("Checkpoint VM")
        return Snapshot(self.host.xe('vm-checkpoint', {'uuid': self.uuid,
                                                       'new-name-label': 'Checkpoint of %s' % self.uuid}),
                        self.host)

    def vifs(self):
        _vifs = []
        for vif_uuid in safe_split(self.host.xe('vif-list', {'vm-uuid': self.uuid}, minimal=True)):
            _vifs.append(VIF(vif_uuid, self))
        return _vifs

    def create_vif(self, vif_num, *, network_uuid=None, network_name=None):
        assert bool(network_uuid) != bool(network_name), \
            "create_vif needs network_uuid XOR network_name"
        if network_name:
            network_uuid = self.host.pool.network_named(network_name)
        assert network_uuid, f"No UUID given, and network name {network_name!r} not found"
        logging.info("Create VIF %d to network %r on VM %s", vif_num, network_uuid, self.uuid)
        self.host.xe('vif-create', {'vm-uuid': self.uuid,
                                    'device': str(vif_num),
                                    'network-uuid': network_uuid,
                                    })

    def is_running_on_host(self, host):
        return self.is_running() and self.param_get('resident-on') == host.uuid

    def get_residence_host(self):
        assert self.is_running()
        host_uuid = self.param_get('resident-on')
        return self.host.pool.get_host_by_uuid(host_uuid)

    def start_background_process(self, cmd: str) -> str:
        script = "/tmp/bg_process.sh"
        pidfile = "/tmp/bg_process.pid"
        with tempfile.NamedTemporaryFile('w') as f:
            f.writelines([
                'echo $$>%s\n' % pidfile,
                cmd + '\n'
            ])
            f.flush()
            if self.is_windows:
                # Use sftp instead of scp for lack of legacy scp protocol in OpenSSH for Windows 11.
                # sftp doesn't know /tmp from the git-bash environment which is mapped to
                # /Users/root/AppData/Local/Temp for the user root so copy the script there.
                self.sftp_put(f.name, script.replace("/tmp/", "/Users/root/AppData/Local/Temp/"))
            else:
                self.scp(f.name, script)

            # https://stackoverflow.com/questions/29142/getting-ssh-to-execute-a-command-in-the-background-on-target-machine
            # ... and run the command through a bash shell so that output redirection both works on Linux and FreeBSD.
            # It is a documented requirement that bash is present on all test VMs.
            remote_cmd = f"bash {script}"
            if not self.is_windows:
                remote_cmd = f'nohup bash -c "{remote_cmd} &>/dev/null &"'
            self.ssh(remote_cmd, background=True)

            wait_for(lambda: self.ssh_with_result(['test', '-f', pidfile]).returncode == 0,
                     "wait for pid file %s to exist" % pidfile)
            pid = self.ssh(['cat', pidfile])
            self.ssh(['rm', '-f', script])
            self.ssh(['rm', '-f', pidfile])
            return pid

    def pid_exists(self, pid):
        return self.ssh_with_result(['kill', '-s', '0', pid]).returncode == 0

    def execute_script(self, script_contents, simple_output=True):
        with tempfile.NamedTemporaryFile('w') as f:
            f.write(script_contents)
            f.flush()
            self.scp(f.name, f.name)
            try:
                logging.debug(f"[{self.ip}] # Will execute this temporary script:\n{script_contents.strip()}")
                # Use bash to run the script, to avoid being hit by differences between shells, for example on FreeBSD
                # It is a documented requirement that bash is present on all test VMs.
                res = self.ssh(['bash', f.name], simple_output=simple_output)
                return res
            finally:
                self.ssh(['rm', '-f', f.name])

    def distro(self):
        """
        Returns the distro name as detected by the guest tools.

        If the distro name was not detected, the result will be an empty string.
        """
        script = "eval $(xe-linux-distribution)\n"
        script += "echo $os_distro\n"
        return self.execute_script(script)

    def tools_version_dict(self):
        """
        Returns the guest tools version as detected by the guest tools, as a {major:, minor:, micro:, build:} dict.

        Values are strings.
        """
        return parse_xe_dict(self.param_get('PV-drivers-version'))

    def tools_version(self):
        """ Returns the tools version in the form major.minor.micro-build. """
        version_dict = self.tools_version_dict()
        return "{major}.{minor}.{micro}-{build}".format(**version_dict)

    def file_exists(self, filepath, regular_file=True):
        """Returns True if the file exists, otherwise returns False."""
        option = '-f' if regular_file else '-e'
        return self.ssh_with_result(['test', option, filepath]).returncode == 0

    def detect_package_manager(self):
        """ Heuristic to determine the package manager on a unix distro. """
        if self.file_exists('/usr/bin/rpm') or self.file_exists('/bin/rpm'):
            return PackageManagerEnum.RPM
        elif self.file_exists('/usr/bin/apt-get'):
            return PackageManagerEnum.APT_GET
        else:
            return PackageManagerEnum.UNKNOWN

    def insert_cd(self, vdi_name):
        logging.info("Insert CD %r in VM %s", vdi_name, self.uuid)
        self.host.xe('vm-cd-insert', {'uuid': self.uuid, 'cd-name': vdi_name})

    def insert_guest_tools_iso(self):
        self.insert_cd('guest-tools.iso')

    def eject_cd(self):
        logging.info("Ejecting CD from VM %s", self.uuid)
        self.host.xe('vm-cd-eject', {'uuid': self.uuid})

    # *** Common reusable test fragments
    def test_snapshot_on_running_vm(self):
        self.wait_for_vm_running_and_ssh_up()
        snapshot = self.snapshot()
        try:
            filepath = '/tmp/%s' % snapshot.uuid
            self.ssh_touch_file(filepath)
            snapshot.revert()
            self.start()
            self.wait_for_vm_running_and_ssh_up()
            logging.info("Check file does not exist anymore")
            self.ssh(['test ! -f ' + filepath])
        finally:
            snapshot.destroy(verify=True)

    def get_messages(self, name):
        args = {
            'obj-uuid': self.uuid,
            'name': name,
            'params': 'uuid',
        }

        lines = self.host.xe('message-list', args).splitlines()

        # Extracts uuids from lines of: "uuid ( RO) : <uuid>"
        return [e.split(':')[1].strip() for e in lines if e]

    def rm_messages(self, name):
        msgs = self.get_messages(name)

        for msg in msgs:
            self.host.xe('message-destroy', {'uuid': msg})

    def sign_efi_bins(self, db: efi.EFIAuth):
        with tempfile.TemporaryDirectory() as directory:
            for remote_bin in self.get_all_efi_bins():
                local_bin = os.path.join(directory, os.path.basename(remote_bin))
                self.scp(remote_bin, local_bin, local_dest=True)
                signed = db.sign_image(local_bin)
                self.scp(signed, remote_bin)

    def set_efi_var(self, var: str, guid: efi.GUID, attrs: bytes, data: bytes):
        """Sets the data and attrs for an EFI variable and GUID."""
        assert len(attrs) == 4

        efivarfs = '/sys/firmware/efi/efivars/%s-%s' % (var, guid.as_str())
        tmp_efivarfs = '/tmp/%s-%s' % (var, guid.as_str())

        if self.file_exists(efivarfs):
            self.ssh(['chattr', '-i', efivarfs])

        try:
            with tempfile.NamedTemporaryFile('wb') as f:
                f.write(attrs)
                f.write(data)
                f.flush()

                # Copy the key in 2 steps because the sftp protocol now used by
                # scp is not able to write directly to efivarfs. Also use 'cat'
                # instead of 'cp' since it doesn't work in Alpine image (because
                # cp is busybox in Alpine)
                self.scp(f.name, tmp_efivarfs)
                self.ssh(["cat", tmp_efivarfs, ">", efivarfs])
        finally:
            self.ssh(["rm", "-f", tmp_efivarfs], check=False)

    def get_efi_var(self, var, guid):
        """Returns a 2-tuple of (attrs, data) for an EFI variable."""
        efivarfs = '/sys/firmware/efi/efivars/%s-%s' % (var, guid.as_str())

        if not self.file_exists(efivarfs):
            return b''

        data = self.ssh(['cat', efivarfs], decode=False)

        # The efivarfs file starts with the attributes, which are 4 bytes long
        return data[4:]

    def clear_uefi_variables(self):
        """
        Remove all UEFI variables.

        This makes it look like the VM is new, in the eyes of uefistored/varstored,
        and so it will propagate certs from disk to its NVRAM when it boots next.

        Some VMs will not boot anymore after such an operation. Seen with debian VMs, for example.
        """
        self.param_remove('NVRAM', 'EFI-variables')

    def get_all_efi_bins(self) -> List[str]:
        magicsz = str(len(efi.EFI_HEADER_MAGIC))
        files = self.ssh(
            [
                'for', 'file', 'in', '$(find', '/boot', '-type', 'f);',
                'do', 'echo', '$file', '$(head', '-c', magicsz, '$file);',
                'done'
            ],
            decode=False).split(b'\n')

        magic = efi.EFI_HEADER_MAGIC.encode('ascii')
        binaries: List[str] = []
        for f in files:
            if magic in f:
                # Avoid decoding an unsplit f, as some headers are not utf8
                # decodable
                fpath = f.split()[0].decode('ascii')
                binaries.append(fpath)

        return binaries

    def get_vtpm_uuid(self):
        return self.host.xe('vtpm-list', {'vm-uuid': self.uuid}, minimal=True)

    def create_vtpm(self):
        logging.info("Creating vTPM for vm %s" % self.uuid)
        return self.host.xe('vtpm-create', {'vm-uuid': self.uuid})

    def destroy_vtpm(self):
        vtpm_uuid = self.get_vtpm_uuid()
        assert vtpm_uuid, "A vTPM must be present"
        logging.info("Destroying vTPM %s" % vtpm_uuid)
        return self.host.xe('vtpm-destroy', {'uuid': vtpm_uuid}, force=True)

    def create_vbd(self, device, vdi_uuid):
        logging.info("Create VBD %r for VDI %r on VM %s", device, vdi_uuid, self.uuid)
        vbd_uuid = self.host.xe('vbd-create', {'vm-uuid': self.uuid,
                                               'device': device,
                                               'vdi-uuid': vdi_uuid,
                                               })
        logging.info("New VBD %s", vbd_uuid)
        return VBD(vbd_uuid, self, device)

    def create_cd_vbd(self, device, userdevice):
        logging.info("Create CD VBD %r on VM %s", device, self.uuid)
        vbd_uuid = self.host.xe('vbd-create', {'vm-uuid': self.uuid,
                                               'device': device,
                                               'type': 'CD',
                                               'mode': 'RO',
                                               })
        vbd = VBD(vbd_uuid, self, device)
        vbd.param_set(param_name="userdevice", value=userdevice)
        logging.info("New VBD %s", vbd_uuid)
        return vbd

    def clone(self, *, name=None):
        if name is None:
            name = self.name() + '_clone_for_tests'
        logging.info("Clone VM")
        uuid = self.host.xe('vm-clone', {'uuid': self.uuid, 'new-name-label': name})
        return VM(uuid, self.host)

    def install_uefi_certs(self, auths):
        """
        Install UEFI certs to the VM's NVRAM store.

        The auths parameter is a list of EFIAuth objects.
        Their attributes are:
        - name: 'PK', 'KEK', 'db' or 'dbx'
        - auth: path to a local file on the tester's environment
        """
        for auth in auths:
            assert auth.name in ['PK', 'KEK', 'db', 'dbx']
        logging.info(f"Installing UEFI certs to VM {self.uuid}: {[auth.name for auth in auths]}")
        for auth in auths:
            dest = self.host.ssh(['mktemp'])
            self.host.scp(auth.auth, dest)
            self.host.ssh([
                'varstore-set', self.uuid, auth.guid.as_str(), auth.name,
                str(efi.EFI_AT_ATTRS), dest
            ])
            self.host.ssh(['rm', '-f', dest])

    def booted_with_secureboot(self):
        """ Returns True if the VM is on and SecureBoot is confirmed to be on from within the VM. """
        if not self.is_uefi:
            return False
        if self.is_windows:
            output = self.ssh(['powershell.exe', 'Confirm-SecureBootUEFI'])
            if output == 'True':
                return True
            if output == 'False':
                return False
            raise Exception(
                "Output of powershell.exe Confirm-SecureBootUEFI should be either True or False. "
                "Got: %s" % output
            )
        else:
            # Previously, we would call tail -c1 directly, and it worked in almost all our test VMs,
            # but it turns out CentOS 7 can't handle tail -c1 on that special file (can't "seek").
            # So we need to cat then pipe into tail.
            last_byte = self.ssh(
                ["cat /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c | tail -c1"],
                decode=False
            )
            if last_byte == b'\x01':
                return True
            if last_byte == b'\x00':
                return False
            raise Exception(
                "SecureBoot's hexadecimal value should have been either b'\\x01' or b'\\x00'. "
                "Got: %r" % last_byte
            )

    def is_in_uefi_shell(self):
        """
        Returns True if it can be established that the UEFI shell is currently running.

        To achieve this, we exploit the pseudo-terminal associated with the VM's serial output, from dom0.
        We connect to the "serial" pty of the VM, input "ver^M" and wait for an expected output.

        The whole operation can take several seconds.
        """
        dom_id = self.param_get('dom-id')
        res_host = self.get_residence_host()
        pty = res_host.ssh(['xenstore-read', f'/local/domain/{dom_id}/serial/0/tty'])
        tmp_file = res_host.ssh(['mktemp'])
        session = f"detached-cat-{self.uuid}"
        ret = False
        try:
            res_host.ssh(['screen', '-dmS', session])
            # run `cat` on the pty in a background screen session and redirect to a tmp file.
            # `cat` will run until we kill the session.
            res_host.ssh(['screen', '-S', session, '-X', 'stuff', f'"cat {pty} > {tmp_file}^M"'])
            # Send the `ver` command to the pty.
            # The first \r is meant to give us access to the shell prompt in case we arrived
            # before the end of the 5s countdown during the UEFI shell startup.
            # The second \r submits the command to the UEFI shell.
            res_host.ssh(['echo', '-e', r'"\rver\r"', '>', pty])
            try:
                wait_for(
                    lambda: "UEFI Interactive Shell" in res_host.ssh(['cat', '-v', tmp_file]),
                    "Wait for UEFI shell response in pty output",
                    10
                )
                ret = True
            except TimeoutError as e:
                logging.debug(e)
                pass
        finally:
            res_host.ssh(['screen', '-S', session, '-X', 'quit'], check=False)
            res_host.ssh(['rm', '-f', tmp_file], check=False)
        return ret

    def set_uefi_setup_mode(self):
        # Note that in XCP-ng 8.2, the VM won't stay in setup mode, because uefistored
        # will add PK and other certs if available when the guest boots.
        logging.info(f"Set VM {self.uuid} to UEFI setup mode")
        self.host.ssh(["varstore-sb-state", self.uuid, "setup"])

    def set_uefi_user_mode(self):
        # Setting user mode propagates the host's certificates to the VM
        logging.info(f"Set VM {self.uuid} to UEFI user mode")
        self.host.ssh(["varstore-sb-state", self.uuid, "user"])

    def is_cert_present(self, key):
        res = self.host.ssh(['varstore-get', self.uuid, efi.get_secure_boot_guid(key).as_str(), key],
                            check=False, simple_output=False, decode=False)
        return res.returncode == 0

    @overload
    def execute_powershell_script(self, script_contents: str,
                                  simple_output: Literal[True] = True,
                                  prepend: str = "$ProgressPreference = 'SilentlyContinue';") -> str:
        ...

    @overload
    def execute_powershell_script(self, script_contents: str,
                                  simple_output: Literal[False],
                                  prepend: str = "$ProgressPreference = 'SilentlyContinue';") -> commands.SSHResult:
        ...

    def execute_powershell_script(
            self,
            script_contents: str,
            simple_output=True,
            prepend="$ProgressPreference = 'SilentlyContinue';"):
        # ProgressPreference is needed to suppress any clixml progress output,
        # as it's not filtered away from stdout by default, and we're grabbing stdout.
        assert self.is_windows
        if prepend is not None:
            script_contents = prepend + script_contents
        cmd = commands.encode_powershell_command(script_contents)
        return self.ssh(
            f"powershell.exe -nologo -noprofile -noninteractive -encodedcommand {cmd}",
            simple_output=simple_output,
        )

    def run_powershell_command(self, program: str, args: str):
        """
        Run command under powershell to retrieve exit codes higher than 255.

        Backslash-safe.
        """
        assert self.is_windows
        output = self.execute_powershell_script(
            f"Write-Output (Start-Process -Wait -PassThru {program} -ArgumentList '{args}').ExitCode")
        return int(output)

    def start_background_powershell(self, cmd: str):
        """
        Run command under powershell in the background.

        Backslash-safe.
        """
        assert self.is_windows
        encoded_command = commands.encode_powershell_command(cmd)
        self.ssh(
            "powershell.exe -noprofile -noninteractive Invoke-WmiMethod -Class Win32_Process -Name Create "
            f"-ArgumentList \\'powershell.exe -noprofile -noninteractive -encodedcommand {encoded_command}\\'"
        )

    def is_windows_pv_device_installed(self):
        """Checks for the install state of **any** Xen/XenServer PV devices."""
        output = self.execute_powershell_script(
            r"""Get-PnpDevice -PresentOnly |
Where-Object CompatibleID -icontains 'PCI\VEN_5853' |
Select-Object -ExpandProperty Problem"""
        )
        # There may be multiple platform PCI devices (e.g. one default, one vendor).
        # In some cases (e.g. installing our tools on VMs with vendor devices), the default and vendor
        # devices may have different statuses (default = installed, vendor = not installed).
        # For now, make sure all of them share the same status since our tools do not support vendor devices anyway.
        statuses = output.splitlines()
        logging.debug(f"Installed Xen device status: {statuses}")
        if all(x == "CM_PROB_NONE" for x in statuses):
            return True
        elif all(x == "CM_PROB_FAILED_INSTALL" for x in statuses):
            return False
        else:
            raise Exception(f"Unknown problem status {statuses}")

    def are_windows_services_present(self):
        """Checks for the presence of **any** Xen/XenServer PV services."""
        output = self.execute_powershell_script(
            r"""$null -ne (Get-Service xenagent,xenbus,xenbus_monitor,xencons,xencons_monitor,xendisk,xenfilt,xenhid,
xeniface,XenInstall,xennet,XenSvc,xenvbd,xenvif,xenvkbd -ErrorAction SilentlyContinue)""")
        return strtobool(output)

    def are_windows_drivers_present(self):
        """Checks for the presence of **any** installed PV drivers, activated or not."""
        output = self.execute_powershell_script(
            r"""$null -ne (Get-ChildItem $env:windir\INF\oem*.inf |
ForEach-Object {Get-Content $_} |
Select-String "AddService=(xenbus|xencons|xendisk|xenfilt|xenhid|xeniface|xennet|xenvbd|xenvif|xenvkbd)")""")
        return strtobool(output)

    def are_windows_tools_working(self):
        assert self.is_windows
        return self.is_windows_pv_device_installed() and strtobool(self.param_get("PV-drivers-detected"))

    def are_windows_tools_uninstalled(self):
        assert self.is_windows
        return (
            not self.is_windows_pv_device_installed()
            and not self.are_windows_services_present()
            and not self.are_windows_drivers_present()
        )

    def save_to_cache(self, cache_id):
        logging.info("Save VM %s to cache for %r as a clone" % (self.uuid, cache_id))

        while True:
            old_vm = self.host.cached_vm(cache_id, sr_uuid=self.host.main_sr_uuid())
            if old_vm is None:
                break
            logging.info("Destroying old cache %s first", old_vm.uuid)
            old_vm.destroy()

        clone = self.clone(name=f"{self.name()} cache")
        logging.info(f"Marking VM {clone.uuid} as cached")
        clone.param_set('name-description', self.host.vm_cache_key(cache_id))


def vm_cache_key_from_def(vm_def, ref_nodeid, test_gitref):
    vm_name = vm_def["name"]
    image_test = vm_def["image_test"]
    image_vm = vm_def.get("image_vm", vm_name)
    image_scope = vm_def.get("image_scope", "module")
    nodeid = shortened_nodeid(expand_scope_relative_nodeid(image_test, image_scope, ref_nodeid))
    image_key = f"{nodeid}-{image_vm}-{test_gitref}"

    from data import IMAGE_EQUIVS
    return IMAGE_EQUIVS.get(image_key, image_key)
