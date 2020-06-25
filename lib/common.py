import json
import subprocess
import time
from subprocess import CalledProcessError

# Common VM images used in tests
def vm_image(vm_key):
    from data import VM_IMAGES, DEF_VM_URL
    url = VM_IMAGES[vm_key]
    if not url.startswith('http'):
        url = DEF_VM_URL + url
    return url

def wait_for(fn, msg=None, timeout_secs=120, retry_delay_secs=2, invert=False):
    if msg is not None:
        print(msg)
    time_left = timeout_secs
    while True:
        ret = fn()
        if not invert and ret:
            return
        if invert and not ret:
            return
        time_left -= retry_delay_secs
        if time_left <= 0:
            expected = 'True' if not invert else 'False'
            raise Exception("Timeout reached while waiting for fn call to yield %s (%s)." % (expected, timeout_secs))
        time.sleep(retry_delay_secs)

def wait_for_not(*args, **kwargs):
    return wait_for(*args, **kwargs, invert=True)

def xo_cli(action, args={}, check=True, simple_output=True):
    res = subprocess.run(
        ['xo-cli', action] + ["%s=%s" % (key, value) for key, value in args.items()],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check
    )
    if simple_output:
        return res.stdout.decode().strip()
    else:
        return res

class SSHException(Exception):
    pass

def ssh(hostname_or_ip, cmd, check=True, simple_output=True, suppress_fingerprint_warnings=True, background=False):
    options = ""
    if suppress_fingerprint_warnings:
        # Suppress warnings and questions related to host key fingerprints
        # because on a test network IPs get reused, VMs are reinstalled, etc.
        # Based on https://unix.stackexchange.com/a/365976/257493
        options = '-o "StrictHostKeyChecking no" -o "LogLevel ERROR" -o "UserKnownHostsFile /dev/null"'

    command = " ".join(cmd)
    if background:
        # https://stackoverflow.com/questions/29142/getting-ssh-to-execute-a-command-in-the-background-on-target-machine
        command = "nohup %s &>/dev/null &" % command
    res = subprocess.run(
        "ssh root@%s %s '%s'" % (hostname_or_ip, options, command),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check
    )

    # Even if check is False, we still raise in case of return code 255, which means a SSH error.
    if res.returncode == 255:
        raise SSHException("SSH Error: %s" % res.stdout.decode())

    if simple_output:
        return res.stdout.decode().strip()
    else:
        return res

def to_xapi_bool(b):
    return 'true' if b else 'false'

class Host:
    def __init__(self, hostname_or_ip):
        self.hostname_or_ip = hostname_or_ip
        self.inventory = None
        self.uuid = None
        self.xo_srv_id = None
        self.user = None
        self.password = None

    def __str__(self):
        return self.hostname_or_ip

    def initialize(self):
        self.inventory = self._get_xensource_inventory()
        self.uuid = self.inventory['INSTALLATION_UUID']

    def ssh(self, cmd, check=True, simple_output=True, suppress_fingerprint_warnings=True, background=False):
        return ssh(self.hostname_or_ip, cmd, check=check, simple_output=simple_output,
                   suppress_fingerprint_warnings=suppress_fingerprint_warnings, background=background)

    def xe(self, action, args={}, check=True, simple_output=True, minimal=False):
        maybe_param_minimal = ['--minimal'] if minimal else []
        return self.ssh(
            ['xe', action]  + maybe_param_minimal + ["%s=%s" % (key, value) for key, value in args.items()],
            check=check,
            simple_output=simple_output
        )

    def _get_xensource_inventory(self):
        output = self.ssh(['cat', '/etc/xensource-inventory'])
        inventory = {}
        for line in output.splitlines():
            key, raw_value = line.split('=')
            inventory[key] = raw_value.strip('\'')
        return inventory

    def xo_server_remove(self):
        if self.xo_srv_id is not None:
            xo_cli('server.remove', {'id': self.xo_srv_id})
        else:
            servers = json.loads(xo_cli('server.getAll'))
            for server in servers:
                if server['host'] == self.hostname_or_ip:
                    xo_cli('server.remove', {'id': server['id']})

    def xo_server_add(self, username, password, label=None, unregister_first=True):
        """
        Returns the server ID created by XO's server.add
        """
        if unregister_first:
            self.xo_server_remove()
        if label is None:
            label = 'Auto tests %s' % self.hostname_or_ip
        xo_srv_id = xo_cli(
            'server.add',
            {
                'host': self.hostname_or_ip,
                'username': username,
                'password': password,
                'allowUnauthorized': 'true',
                'label': label
            }
        )
        self.xo_srv_id = xo_srv_id

    def xo_server_status(self):
        servers = json.loads(xo_cli('server.getAll'))
        for server in servers:
            if server['host'] == self.hostname_or_ip:
                return server['status']
        return None

    def xo_server_connected(self):
        return self.xo_server_status() == "connected"

    def import_vm_url(self, url):
        print("Import VM %s on host %s" % (url, self))
        vm = VM(self.xe('vm-import', {'url': url}), self)
        # Set VM VIF networks to the host's management network
        for vif in vm.vifs():
            vif.move(self.management_network())
        return vm

    def pool_has_vm(self, vm_uuid, vm_type='vm'):
        if vm_type == 'snapshot':
            return self.xe('snapshot-list', {'uuid': vm_uuid}, minimal=True) == vm_uuid
        else:
            return self.xe('vm-list', {'uuid': vm_uuid}, minimal=True) == vm_uuid

    def install_updates(self):
        print("Install updates on host %s" % self)
        return self.ssh(['yum', 'update', '-y'])

    def restart_toolstack(self):
        print("Restart toolstack on host %s" % self)
        return self.ssh(['xe-toolstack-restart'])

    def is_enabled(self):
        try:
            return self.xe('host-param-get', {'uuid': self.uuid, 'param-name': 'enabled'}) == 'true'
        except subprocess.CalledProcessError:
            # If XAPI is not ready yet, this will throw. We return False in that case.
            return False

    def has_updates(self):
        try:
            # yum check-update returns 100 if there are updates, 1 if there's an error, 0 if no updates
            self.ssh(['yum', 'check-update'])
            # returned 0, else there would have been a CalledProcessError
            return False
        except CalledProcessError as e:
            if e.returncode == 100:
                return True
            else:
                raise

    def reboot(self):
        print("Reboot host %s" % self)
        try:
            self.ssh(['reboot'])
        except subprocess.CalledProcessError as e:
            # ssh connection may get killed by the reboot and terminate with an error code
            if "closed by remote host" in e.stdout.decode().strip():
                return

    def pool_uuid(self):
        return self.xe('pool-list', minimal=True)

    def management_network(self):
        return self.xe('network-list', {'bridge': self.inventory['MANAGEMENT_INTERFACE']}, minimal=True)

class BaseVM:
    def __init__(self, uuid, host):
        self.uuid = uuid
        self.host = host

    def param_get(self, param_name, key=None, accept_unknown_key=False):
        args = {'uuid': self.uuid, 'param-name': param_name}
        if key is not None:
            args['param-key'] = key
        try:
            value = self.host.xe('vm-param-get', args)
        except subprocess.CalledProcessError as e:
            if key and accept_unknown_key and e.stdout.decode().strip() == "Error: Key %s not found in map" % key:
                value = None
            else:
                raise
        return value

    def vdi_uuids(self):
        output = self._disk_list()
        vdis = []
        for line in output.splitlines():
            vdis.append(line.split(',')[0])
        return vdis

    def destroy_vdi(self, vdi_uuid):
        self.host.xe('vdi-destroy', {'uuid': vdi_uuid})

    # move this method and the above back to class VM if not useful in Snapshot class?
    def destroy(self):
        for vdi_uuid in self.vdi_uuids():
            self.destroy_vdi(vdi_uuid)
        self._destroy()

class VM(BaseVM):
    def __init__(self, uuid, host):
        super().__init__(uuid, host)
        self.ip = None
        self.previous_host = None # previous host when migrated or being migrated

    def power_state(self):
        return self.param_get('power-state')

    def is_running(self):
        return self.power_state() == 'running'

    def is_halted(self):
        return self.power_state() == 'halted'

    def is_suspended(self):
        return self.power_state() == 'suspended'

    def is_paused(self):
        return self.power_state() == 'paused'

    def start(self):
        print("Start VM")
        return self.host.xe('vm-start', {'uuid': self.uuid})

    def shutdown(self, force=False):
        print("Shutdown VM")
        return self.host.xe('vm-shutdown', {'uuid': self.uuid, 'force': to_xapi_bool(force)})

    def try_get_and_store_ip(self):
        ip = self.param_get('networks', '0/ip', accept_unknown_key=True)

        if not ip:
            return False
        else:
            self.ip = ip
            return True

    def ssh(self, cmd, check=True, simple_output=True, background=False):
        # raises by default for any nonzero return code
        return ssh(self.ip, cmd, check=check, simple_output=simple_output, background=background)

    def ssh_with_result(self, cmd):
        # doesn't raise if the command's return is nonzero, unless there's a SSH error
        return self.ssh(cmd, check=False, simple_output=False)

    def is_ssh_up(self):
        try:
            return self.ssh_with_result(['true']).returncode == 0
        except SSHException:
            # probably not up yet
            return False

    def wait_for_vm_running_and_ssh_up(self, wait_for_ip=False):
        wait_for(self.is_running, "Wait for VM running")
        if wait_for_ip:
            wait_for(self.try_get_and_store_ip, "Wait for VM IP")
        assert(self.ip is not None)
        wait_for(self.is_ssh_up, "Wait for SSH up")

    def ssh_touch_file(self, filepath):
        print("Create file on VM (%s)" % filepath)
        self.ssh(['touch', filepath])
        print("Check file created")
        self.ssh(['test -f ' + filepath])

    def suspend(self):
        print("Suspend VM")
        self.host.xe('vm-suspend', {'uuid': self.uuid})

    def resume(self):
        print("Resume VM")
        self.host.xe('vm-resume', {'uuid': self.uuid})

    def pause(self):
        print("Pause VM")
        self.host.xe('vm-pause', {'uuid': self.uuid})

    def unpause(self):
        print("Unpause VM")
        self.host.xe('vm-unpause', {'uuid': self.uuid})

    def _disk_list(self):
        return self.host.xe('vm-disk-list', {'uuid': self.uuid}, minimal=True)

    def _destroy(self):
        self.host.xe('vm-destroy', {'uuid': self.uuid})

    def destroy(self):
        # Note: not using xe vm-uninstall (which would be convenient) because it leaves a VDI behind
        # See https://github.com/xapi-project/xen-api/issues/4145
        if not self.is_halted():
            self.shutdown(force=True)
        super().destroy()

    def exists(self):
        return self.host.pool_has_vm(self.uuid)

    def exists_on_previous_pool(self):
        return self.previous_host.pool_has_vm(self.uuid)

    def migrate(self, target_host):
        print("Migrate VM to host %s" % target_host)
        xo_cli('vm.migrate', {'vm': self.uuid, 'targetHost': target_host.uuid})
        previous_host = self.host
        self.host = target_host
        self.previous_host = previous_host

    def snapshot(self):
        print("Snapshot VM")
        return Snapshot(self.host.xe('vm-snapshot', {'uuid': self.uuid,
                                                     'new-name-label': '"Snapshot of %s"' % self.uuid}),
                        self.host)

    def checkpoint(self):
        print("Checkpoint VM")
        return Snapshot(self.host.xe('vm-checkpoint', {'uuid': self.uuid,
                                                     'new-name-label': '"Checkpoint of %s"' % self.uuid}),
                        self.host)

    def vifs(self):
        _vifs = []
        for vif_uuid in self.host.xe('vif-list', {'vm-uuid': self.uuid}, minimal=True).split(','):
            _vifs.append(VIF(vif_uuid, self))
        return _vifs


class Snapshot(BaseVM):
    def _disk_list(self):
        return self.host.xe('snapshot-disk-list', {'uuid': self.uuid}, minimal=True)

    def destroy(self):
        print("Delete snapshot")
        # that uninstall command apparently works better for snapshots than for VMs apparently
        self.host.xe('snapshot-uninstall', {'uuid': self.uuid, 'force': 'true'})
        print("Check snapshot doesn't exist anymore")
        assert(not self.exists())

#     def _destroy(self):
#         self.host.xe('snapshot-destroy', {'uuid': self.uuid})

    def exists(self):
        return self.host.pool_has_vm(self.uuid, vm_type='snapshot')

    def revert(self):
        print("Revert snapshot")
        self.host.xe('snapshot-revert', {'uuid': self.uuid})

class VIF:
    def __init__(self, uuid, vm):
        self.uuid = uuid
        self.vm = vm

    def move(self, network_uuid):
        self.vm.host.xe('vif-move', {'uuid': self.uuid, 'network-uuid': network_uuid})