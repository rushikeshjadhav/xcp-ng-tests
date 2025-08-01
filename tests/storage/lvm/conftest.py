import pytest

import logging

@pytest.fixture(scope='package')
def lvm_sr(host, sr_disk):
    """ An LVM SR on first host. """
    sr = host.sr_create('lvm', "LVM-local-SR-test", {'device': '/dev/' + sr_disk})
    yield sr
    # teardown
    sr.destroy()

@pytest.fixture(scope='module')
def vdi_on_lvm_sr(lvm_sr):
    vdi = lvm_sr.create_vdi('LVM-local-VDI-test')
    yield vdi
    vdi.destroy()

@pytest.fixture(scope='module')
def vm_on_lvm_sr(host, lvm_sr, vm_ref):
    vm = host.import_vm(vm_ref, sr_uuid=lvm_sr.uuid)
    yield vm
    # teardown
    logging.info("<< Destroy VM")
    vm.destroy(verify=True)
