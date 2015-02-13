# -*- coding: utf-8 -*-
#
# Copyright 2015 eNovance SAS <licensing@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import mock
import testtools
import unittest

libvirt_conn = mock.Mock()
libvirt_conn.listAllNetworks.return_value = [
    mock.Mock(**{'name.return_value': 'default_sps'})]
libvirt_conn.listAllDomains.return_value = [
    mock.Mock(**{'name.return_value': 'default_os-ci-test11'})]
libvirt_conn.lookupByName.return_value = mock.Mock(**{
    'info.return_value': [1], 'create.return_value': True})
libvirt_conn.networkLookupByName.return_value = mock.Mock(**{
    'DHCPLeases.return_value': [{'mac': '52:54:00:01:02:03',
                                 'ipaddr': '1.2.3.4'}]})


class FakeLibvirt(object):
    VIR_DOMAIN_NOSTATE = 0
    VIR_DOMAIN_RUNNING = 1
    VIR_DOMAIN_BLOCKED = 2
    VIR_DOMAIN_PAUSED = 3
    VIR_DOMAIN_SHUTDOWN = 4
    VIR_DOMAIN_SHUTOFF = 5
    VIR_DOMAIN_CRASHED = 6
    VIR_DOMAIN_PMSUSPENDED = 7

    def open(a, b):
        return libvirt_conn

    class libvirtError(Exception):
        pass


class TestVirtualizor(testtools.TestCase):

    def setUp(self):
        super(TestVirtualizor, self).setUp()
        self.module_patcher = mock.patch.dict(
            'sys.modules', {'libvirt': FakeLibvirt()})
        self.module_patcher.start()
        import virtualizor
        self.virtualizor = virtualizor
        self.virtualizor.random_mac = mock.Mock(
            return_value='52:54:00:01:02:03')
        self.virtualizor.logging = mock.Mock()
        libvirt_conn.reset_mock()

    def test_random_mac(self):
        import virtualizor
        self.assertRegex(virtualizor.random_mac(),
                         '^([0-9a-fA-F]{2}:){5}([0-9a-fA-F]{2})$')

    @mock.patch('virtualizor.subprocess.call')
    def test_main(self, sub_call):
        self.virtualizor.main(['virt_platform_qcow2.yml.sample', 'bar',
                               '--pub-key-file',
                               'virt_platform_qcow2.yml.sample'])
        self.assertEqual(sub_call.call_count, 36)
        self.assertEqual(libvirt_conn.networkCreateXML.call_count, 1)
        self.assertEqual(libvirt_conn.defineXML.call_count, 3)

    @mock.patch('virtualizor.subprocess.call')
    def test_main_with_replace(self, sub_call):
        self.virtualizor.main(['--replace', 'virt_platform_qcow2.yml.sample',
                               'bar', '--pub-key-file',
                               'virt_platform_qcow2.yml.sample'])
        self.assertEqual(sub_call.call_count, 49)
        self.assertEqual(libvirt_conn.networkCreateXML.call_count, 2)
        self.assertEqual(libvirt_conn.defineXML.call_count, 4)

if __name__ == '__main__':
    unittest.main()
