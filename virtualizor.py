#!/usr/bin/env python
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

import argparse
import logging
import os.path
import random
import re
import string
import subprocess
import sys
import tempfile
import time
import uuid

import ipaddress
import jinja2
import libvirt
import six
import yaml

logging.basicConfig(level=logging.DEBUG)


def random_mac():
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255))


def canical_size(size):
    """Convert size to GB or MB

    Convert GiB to MB or return the original string.

    """
    gi = re.search('^(\d+)Gi', size)
    if gi:
        new_size = "%i" % (int(gi.group(1)) * 1000 ** 3)
    else:
        new_size = size
    return new_size


def get_conf(argv=sys.argv):
    def check_prefix(value):
        if not re.match('^[a-zA-Z\d]+$', value):
            sys.stderr.write("Invalid value for --prefix parameter\n")
            sys.exit(1)
        return value
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description='Deploy a virtual infrastructure.')
    parser.add_argument('--replace', action='store_true',
                        help='existing conflicting resources will be remove '
                        'recreated.')
    parser.add_argument('input_file', type=str,
                        help='the YAML input file, as generated by '
                        'collector.py.')
    parser.add_argument('target_host', type=str,
                        help='the name of the libvirt server. The local user '
                        'must be able to connect to the root account with no '
                        'password authentification.')
    parser.add_argument('--pub-key-file', type=str,
                        default=os.path.expanduser(
                            '~/.ssh/id_rsa.pub'),
                        help='the path to the SSH public key file that must '
                        'be injected in the install-server root and jenkins '
                        'account')
    parser.add_argument('--prefix', default='default', type=check_prefix,
                        help='optional prefix to put in the machine and '
                        'network to avoid conflict with resources create by '
                        'another virtualizor instance. Thanks to this '
                        'parameter, the user can run as virtualizor as '
                        'needed on the same machine.')
    parser.add_argument('--public_network', default='nat', type=str,
                        help='allow the user to pass the name of a libvirt '
                        'NATed network that will be used as a public network '
                        'for the install-server. This public network will by '
                        'attached to eth1 interface and IP address is '
                        'associated using the DHCP.')

    conf = parser.parse_args(argv)
    return conf


class Hypervisor(object):
    def __init__(self, conf):
        self.target_host = conf.target_host
        self.conn = libvirt.open('qemu+ssh://root@%s/system' %
                                 conf.target_host)

    def create_networks(self, conf, install_server_info):
        existing_networks = ([n.name() for n in self.conn.listAllNetworks()])
        # Ensure the public_network is defined, we don't replace this network,
        # even if --replace is used because other VM may by connected to the
        # same networks.
        if conf.public_network not in existing_networks:
            pub_net = Network(conf.public_network, {
                "dhcp": {"address": "192.168.140.1",
                         "netmask": "255.255.255.0",
                         "range": {
                             "ipstart": "192.168.140.2",
                             "ipend": "192.168.140.254"}}})
            self.conn.networkCreateXML(pub_net.dump_libvirt_xml())
        self.public_net = self.conn.networkLookupByName(
            conf.public_network)
        if not self.public_net.isActive():
            self.public_net.create()

        net_definitions = {("%s_sps" % conf.prefix): {}}
        for netname in net_definitions:
            exists = netname in existing_networks
            if exists and conf.replace:
                self.conn.networkLookupByName(netname).destroy()
                logging.info("Cleaning network %s." % netname)
                exists = False
            if not exists:
                logging.info("Creating network %s." % netname)
                network = Network(netname, net_definitions[netname])
                self.conn.networkCreateXML(network.dump_libvirt_xml())
        self.public_net = self.conn.networkLookupByName(
            conf.public_network)

    def wait_for_lease(self, hypervisor, mac):
        while True:
            for lease in hypervisor.public_net.DHCPLeases():
                if lease['mac'] == mac:
                    return lease['ipaddr']
            time.sleep(1)

    def push(self, source, dest):
        subprocess.call(['scp', '-q', '-r', source,
                         'root@%s' % self.target_host + ':' + dest])

    def call(self, *kargs):
        subprocess.call(['ssh', 'root@%s' % self.target_host] +
                        list(kargs))

    class MissingPublicNetwork(Exception):
        pass


class Host(object):
    host_template_string = """
<domain type='kvm'>
  <name>{{ hostname_with_prefix }}</name>
  <uuid>{{ uuid }}</uuid>
  <memory unit='KiB'>{{ memory }}</memory>
  <currentmemory unit='KiB'>{{ memory }}</currentmemory>
  <vcpu>{{ ncpus }}</vcpu>
  <os>
    <smbios mode='sysinfo'/>
    <type arch='x86_64' machine='pc'>hvm</type>
    <bios useserial='yes' rebootTimeout='5000'/>
  </os>
  <sysinfo type='smbios'>
    <bios>
      <entry name='vendor'>eNovance</entry>
    </bios>
    <system>
      <entry name='manufacturer'>QEMU</entry>
      <entry name='product'>virtualizor</entry>
      <entry name='version'>1.0</entry>
    </system>
  </sysinfo>
  <features>
    <acpi/>
    <apic/>
    <pae/>
  </features>
  <clock offset='utc'/>
  <on_poweroff>restart</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
{% for disk in disks %}
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{{ disk.path }}'/>
      <target dev='{{ disk.name }}' bus='virtio'/>
{% if disk.boot_order is defined %}
      <boot order='{{ disk.boot_order }}'/>
{% endif %}
    </disk>
{% endfor %}
{% for nic in nics %}
{% if nic.network_name is defined %}
    <interface type='network'>
      <mac address='{{ nic.mac }}'/>
      <source network='{{ nic.network_name }}'/>
      <model type='virtio'/>
{% if nic.boot_order is defined %}
      <boot order='{{ nic.boot_order }}'/>
{% endif %}
    </interface>
{% endif %}
{% endfor %}
    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <input type='mouse' bus='ps2'/>
    <graphics type='vnc' port='-1' autoport='yes'/>
    <video>
      <model type='cirrus' vram='9216' heads='1'/>
    </video>
  </devices>
</domain>
    """
    host_libvirt_image_dir = "/var/lib/libvirt/images"
    user_data_template_string = """#cloud-config
users:
 - default
 - name: jenkins
   ssh-authorized-keys:
{% for ssh_key in ssh_keys %}   - {{ ssh_key|trim }}
{% endfor %}
 - name: root
   ssh-authorized-keys:
{% for ssh_key in ssh_keys %}   - {{ ssh_key|trim }}
{% endfor %}

write_files:
  - path: /etc/resolv.conf
    content: |
      nameserver 8.8.8.8
      options rotate timeout:1
  - path: /etc/sudoers.d/jenkins-cloud-init
    permissions: 0440
    content: |
      Defaults:jenkins !requiretty
      jenkins ALL=(ALL) NOPASSWD:ALL
{% for nic in nics %}
  - path: /etc/sysconfig/network-scripts/ifcfg-{{ nic.name }}
    content: |
      DEVICE={{ nic.name }}
      ONBOOT=yes
{% if nic.ip is defined %}
      BOOTPROTO=none
      IPADDR={{ nic.ip }}
      NETWORK={{ nic.network }}
      NETMASK={{ nic.netmask }}
{% else %}
      BOOTPROTO=dhcp
{% endif %}
{% endfor %}
  - path: /etc/sysconfig/network
    content: |
      NETWORKING=yes
      NOZEROCONF=no
      HOSTNAME={{ hostname }}
  - path: /etc/sysctl.conf
    content: |
      net.ipv4.ip_forward = 1
  - path: /root/.ssh/id_rsa
    permissions: 0400
    owner: root:root
    content: |
        -----BEGIN RSA PRIVATE KEY-----
        MIIEowIBAAKCAQEA47rZ0qSOqzLIspnMFvfwSYsms00nJkmsLHLem/9JsvqM2brk
        0nfUDzr/6BbieH4HoC+GZMnhh21gX0C/nk/fYFLP+0fY2KbRObbCbdZGlOCguMcJ
        QgcKz0sU9mms4+bPcmnil+j8ljP+KjrBDXiZtl6sYj0CnoD3MlVZfHmLI20xa0aO
        CUvjLDsgTjVFhtRDdEHXcl4XHPJ5RKT9sEBlKbhCmI/6O1pNxEGEWvwwQE08+9Xl
        9rJ08/nb/hIuzhTGJdEX/u7jwidxnYbzFyPdgs4jCHDAciqUorlgd/yw6Hs0z2IY
        GxD9CPs6shx8aRmVLGMf+YnOUQHVD1Hi0FrBFwIDAQABAoIBAQDXekZ/DJu+G7hR
        XjsBhKrFO7hrsdYYYV9bU3mVS7I1euNpZXD8QMvTeXUI6xZxAnc+t5lHpsoSNYkZ
        uA9XwaXP46vNzQa+wOF55ZcFDNoOJpmNHS+CXV16FUYJfqZLomqpjM0OBjNyAFI/
        LQbcMz/mkqAz+ByRU+ASrTWWFP91jSRSWAO/xmRcgqmh02TWlVJRROS3CsWz9C47
        Ag1diZ4r2d1gFwnc6ZfSTNActLgUNU2NyDsFL4qHipWssGqoclfhsIdL1CLmhTix
        t8tO0QBSw60H2XqQ0Y77MNfEYgdqvp6XRlB+Uw9Qjf3Y0ukA6ekD3BGfTcaNcq4b
        4N1WUmTpAoGBAPYCzaWRcXHCJ/0WAUhggvy1kKbAdEtoJfkS3lva9pPgyRx+cTrW
        98az6NhdD774O3F1GT8RemoX/9JpX0Z2HG3+f0r2vcSqhsyjJSJF6dEU3DMFte+G
        A67iHnmmfelc1tZKrGuqfrGnFeMQgrmj3ugekKAoyeybPXkd7YTC9cidAoGBAOz6
        Bbpmvrqr41TOgZCssFjteBNDvDU9NfHtpkgAx7HYkNp4xaWPwlBBydS6Ubsx5RQJ
        EXf6y5OfCuNkmHTFvubeaG6rg450YKWLO95F5TYfRJdQ6/lkFjhPpsIe9q/QFLP3
        ZOu+nE2ONCIlUKY7cpLOpYPs+RvYBMETYnSBYEBDAoGAI/ra+tkfv2SHFrPOMjiz
        T6R6aHkDSTgNPbVtwf9vSsd4gmtXwiRIjs4nQuWxdNu3Teuzao7y2WtzJeH1ZkfF
        9qxfD6awsH/EQU+nEbEp9kNXxTqTllmCVmSJ0n7wMV47qZG4T/Lanr7yK4hxphb6
        dfZqbpIonitCPWGMKHufGN0CgYB8yCZuAZ4a01nQFTEaSiRNnzVkB326FvIp4vZ0
        4ZxFZIDZ2VBRnoI2Gn45eqaAyIQUabX+FFxP7iYgmJ7ClkGwdZpN9BhA0bz2TnuG
        zg0k05AdkWnAF1iv7BkmDIHfD9Vm8jT9AZByMhf3huiRr6nj7dYvwn9ljvjp5dgo
        +tsA2wKBgF7pLURG7z1TAM3jKikqjs2UUgPBW+Fd9gpzpgVnujoQnC30/aZvUzUL
        ZPICIuMYWuFGC/KCrq/X+pMqH6t9WmpX6SFW3TMjKrPOkqf5m7nJHTiHX+DmBfGr
        bzgHWb/BDGyPxBbv34G6TdlZo64M3pQhz9Yr9DB1QQjkgJpVVds0
        -----END RSA PRIVATE KEY-----
  - path: /var/lib/jenkins/.ssh/id_rsa
    permissions: 0400
    owner: jenkins:jenkins
    content: |
        -----BEGIN RSA PRIVATE KEY-----
        MIIEowIBAAKCAQEA47rZ0qSOqzLIspnMFvfwSYsms00nJkmsLHLem/9JsvqM2brk
        0nfUDzr/6BbieH4HoC+GZMnhh21gX0C/nk/fYFLP+0fY2KbRObbCbdZGlOCguMcJ
        QgcKz0sU9mms4+bPcmnil+j8ljP+KjrBDXiZtl6sYj0CnoD3MlVZfHmLI20xa0aO
        CUvjLDsgTjVFhtRDdEHXcl4XHPJ5RKT9sEBlKbhCmI/6O1pNxEGEWvwwQE08+9Xl
        9rJ08/nb/hIuzhTGJdEX/u7jwidxnYbzFyPdgs4jCHDAciqUorlgd/yw6Hs0z2IY
        GxD9CPs6shx8aRmVLGMf+YnOUQHVD1Hi0FrBFwIDAQABAoIBAQDXekZ/DJu+G7hR
        XjsBhKrFO7hrsdYYYV9bU3mVS7I1euNpZXD8QMvTeXUI6xZxAnc+t5lHpsoSNYkZ
        uA9XwaXP46vNzQa+wOF55ZcFDNoOJpmNHS+CXV16FUYJfqZLomqpjM0OBjNyAFI/
        LQbcMz/mkqAz+ByRU+ASrTWWFP91jSRSWAO/xmRcgqmh02TWlVJRROS3CsWz9C47
        Ag1diZ4r2d1gFwnc6ZfSTNActLgUNU2NyDsFL4qHipWssGqoclfhsIdL1CLmhTix
        t8tO0QBSw60H2XqQ0Y77MNfEYgdqvp6XRlB+Uw9Qjf3Y0ukA6ekD3BGfTcaNcq4b
        4N1WUmTpAoGBAPYCzaWRcXHCJ/0WAUhggvy1kKbAdEtoJfkS3lva9pPgyRx+cTrW
        98az6NhdD774O3F1GT8RemoX/9JpX0Z2HG3+f0r2vcSqhsyjJSJF6dEU3DMFte+G
        A67iHnmmfelc1tZKrGuqfrGnFeMQgrmj3ugekKAoyeybPXkd7YTC9cidAoGBAOz6
        Bbpmvrqr41TOgZCssFjteBNDvDU9NfHtpkgAx7HYkNp4xaWPwlBBydS6Ubsx5RQJ
        EXf6y5OfCuNkmHTFvubeaG6rg450YKWLO95F5TYfRJdQ6/lkFjhPpsIe9q/QFLP3
        ZOu+nE2ONCIlUKY7cpLOpYPs+RvYBMETYnSBYEBDAoGAI/ra+tkfv2SHFrPOMjiz
        T6R6aHkDSTgNPbVtwf9vSsd4gmtXwiRIjs4nQuWxdNu3Teuzao7y2WtzJeH1ZkfF
        9qxfD6awsH/EQU+nEbEp9kNXxTqTllmCVmSJ0n7wMV47qZG4T/Lanr7yK4hxphb6
        dfZqbpIonitCPWGMKHufGN0CgYB8yCZuAZ4a01nQFTEaSiRNnzVkB326FvIp4vZ0
        4ZxFZIDZ2VBRnoI2Gn45eqaAyIQUabX+FFxP7iYgmJ7ClkGwdZpN9BhA0bz2TnuG
        zg0k05AdkWnAF1iv7BkmDIHfD9Vm8jT9AZByMhf3huiRr6nj7dYvwn9ljvjp5dgo
        +tsA2wKBgF7pLURG7z1TAM3jKikqjs2UUgPBW+Fd9gpzpgVnujoQnC30/aZvUzUL
        ZPICIuMYWuFGC/KCrq/X+pMqH6t9WmpX6SFW3TMjKrPOkqf5m7nJHTiHX+DmBfGr
        bzgHWb/BDGyPxBbv34G6TdlZo64M3pQhz9Yr9DB1QQjkgJpVVds0
        -----END RSA PRIVATE KEY-----

runcmd:
 - /usr/sbin/sysctl -p
 - /usr/sbin/iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE
 - /bin/rm -f /etc/yum.repos.d/*.repo
 - /usr/bin/systemctl restart network

"""
    meta_data_template_string = """
instance-id: id-install-server
local-hostname: {{ hostname }}

"""

    def __init__(self, hypervisor, conf, definition,
                 install_server_info, gateway_info):
        self.hypervisor = hypervisor
        self.conf = conf
        self.hostname = definition['hostname']
        self.hostname_with_prefix = definition['hostname_with_prefix']
        self.meta = {'hostname': definition['hostname'],
                     'hostname_with_prefix':
                         definition['hostname_with_prefix'],
                     'uuid': str(uuid.uuid1()),
                     'memory': 8,
                     'ncpus': 1,
                     'cpus': [], 'disks': [], 'nics': []}
        self.disk_cpt = 0

        for k in ('uuid', 'serial', 'product_name',
                  'memory', 'ncpus', 'is_install_server'):
            if k not in definition:
                continue
            self.meta[k] = definition[k]

        if definition['profile'] == 'install-server':
            logging.info("  Configuring the install-server")
            self.meta['is_install_server'] = True
            definition['nics'].append({
                'mac': install_server_info['mac'],
                'network_name': conf.public_network
            })

        if definition['profile'] == 'gateway':
            logging.info("  Configuring the Gateway")
            self.meta['is_gateway'] = True
            definition['nics'].append({
                'mac': gateway_info['mac'],
                'network_name': conf.public_network
            })

        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.template = env.from_string(Host.host_template_string)

        for nic in definition['nics']:
            self.register_nic(nic)
        for disk in definition['disks']:
            self.initialize_disk(disk)
            self.register_disk(disk)
        if 'image' in definition['disks'][0]:
            cloud_init_image = self.create_cloud_init_image()
            self.register_disk(cloud_init_image)

        self.meta['nics'][0]['boot_order'] = 2
        self.meta['disks'][0]['boot_order'] = 1

    def create_cloud_init_image(self):

        ssh_key_file = self.conf.pub_key_file
        meta = {
            'ssh_keys': open(ssh_key_file).readlines(),
            'hostname': self.hostname,
            'nics': self.meta['nics']
        }
        # NOTE(Gonéri): Add the unsecure key
        meta['ssh_keys'].append(
            'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDjutnSpI6rMsiym'
            'cwW9/BJiyazTScmSawsct6b/0my+ozZuuTSd9QPOv/oFuJ4fgegL4Z'
            'kyeGHbWBfQL+eT99gUs/7R9jYptE5tsJt1kaU4KC4xwlCBwrPSxT2a'
            'azj5s9yaeKX6PyWM/4qOsENeJm2XqxiPQKegPcyVVl8eYsjbTFrRo4'
            'JS+MsOyBONUWG1EN0QddyXhcc8nlEpP2wQGUpuEKYj/o7Wk3EQYRa/'
            'DBATTz71eX2snTz+dv+Ei7OFMYl0Rf+7uPCJ3GdhvMXI92CziMIcMB'
            'yKpSiuWB3/LDoezTPYhgbEP0I+zqyHHxpGZUsYx/5ic5RAdUPUeLQW'
            'sEX unsecure')
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        contents = {
            'user-data': env.from_string(Host.user_data_template_string),
            'meta-data': env.from_string(Host.meta_data_template_string)}
        # TODO(Gonéri): use mktemp
        data_dir = "/tmp/%s_data" % self.hostname_with_prefix
        self.hypervisor.call("mkdir", "-p", data_dir)
        for name in sorted(contents):
            fd = tempfile.NamedTemporaryFile()
            fd.write(contents[name].render(meta))
            fd.seek(0)
            fd.flush()
            self.hypervisor.push(fd.name, data_dir + '/' + name)

        image = '%s/%s_cloud-init.qcow2' % (
                Host.host_libvirt_image_dir,
                self.hostname_with_prefix)
        self.hypervisor.call(
            'truncate', '--size', '2M', image + '.tmp')
        self.hypervisor.call(
            'mkfs.vfat', '-n', 'cidata', image + '.tmp')
        self.hypervisor.call(
            'mcopy', '-oi', image + '.tmp',
            data_dir + '/user-data', data_dir + '/meta-data', '::')
        self.hypervisor.call(
            'qemu-img', 'convert', '-O', 'qcow2', image + '.tmp', image)
        self.hypervisor.call(
            'rm', image + '.tmp')
        return({'path': image})

    def initialize_disk(self, disk):
        disk_cpt = len(self.meta['disks'])
        filename = "%s-%03d.qcow2" % (self.hostname_with_prefix, disk_cpt)
        if 'image' in disk:
            self.hypervisor.call(
                'qemu-img', 'create', '-q', '-f', 'qcow2',
                '-b', disk['image'],
                Host.host_libvirt_image_dir + '/' + filename,
                canical_size(disk['size']))
            self.hypervisor.call(
                'qemu-img', 'resize', '-q',
                Host.host_libvirt_image_dir + '/' + filename,
                canical_size(disk['size']))
        else:
            self.hypervisor.call(
                'qemu-img', 'create', '-q', '-f', 'qcow2',
                Host.host_libvirt_image_dir + '/' + filename,
                canical_size(disk['size']))

        disk.update({'path': Host.host_libvirt_image_dir + '/' + filename})

    def register_disk(self, disk):
        disk_cpt = len(self.meta['disks'])
        disk['name'] = 'vd' + string.ascii_lowercase[disk_cpt]
        self.meta['disks'].append(disk)

    def register_nic(self, nic):
        default = {
            'mac': random_mac(),
            'name': 'eth%i' % len(self.meta['nics']),
            'network_name': '%s_sps' % self.conf.prefix
        }
        default.update(nic)
        self.meta['nics'].append(default)

    def dump_libvirt_xml(self):
        return self.template.render(self.meta)


class Network(object):
    network_template_string = """
<network>
  <name>{{ name }}</name>
  <uuid>{{ uuid }}</uuid>
  <bridge name='{{ bridge_name }}' stp='on' delay='0'/>
  <mac address='{{ mac }}'/>
{% if dhcp is defined %}
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <ip address='{{ dhcp.address }}' netmask='{{ dhcp.netmask }}'>
    <dhcp>
{%if dhcp.range is defined %}
      <range start='{{ dhcp.range.ipstart }}' end='{{ dhcp.range.ipend }}' />
{% endif %}
    </dhcp>
  </ip>
{% endif %}
</network>
    """

    def __init__(self, name, definition):
        self.name = name
        self.meta = {
            'name': name,
            'uuid': str(uuid.uuid1()),
            'mac': random_mac(),
            'bridge_name': 'virbr%d' % random.randrange(0, 0xffffffff)}

        for k in ('uuid', 'mac', 'ips', 'dhcp'):
            if k not in definition:
                continue
            self.meta[k] = definition[k]

        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.template = env.from_string(Network.network_template_string)

    def dump_libvirt_xml(self):
        return self.template.render(self.meta)


def get_profile_info(hosts_definition, profile):
    for hostname, definition in six.iteritems(hosts_definition['hosts']):
        if definition.get('profile', '') == profile:
            break

    logging.info("%s (%s)" % (profile, hostname))
    admin_nic_info = definition['nics'][0]
    network = ipaddress.ip_network(
        unicode(
            admin_nic_info['network'] + '/' + admin_nic_info['netmask']))
    admin_nic_info = definition['nics'][0]
    return {
        'mac': admin_nic_info.get('mac', random_mac()),
        'hostname': hostname,
        'ip': admin_nic_info['ip'],
        'gateway': str(network.network_address + 1),
        'netmask': str(network.netmask),
        'network': str(network.network_address),
        'version': hosts_definition.get('version', 'RH7.0-I.1.2.1'),
    }


def main(argv=sys.argv[1:]):
    conf = get_conf(argv)
    hosts_definition = yaml.load(open(conf.input_file, 'r'))
    hypervisor = Hypervisor(conf)
    install_server_info = get_profile_info(hosts_definition, "install-server")
    gateway_info = get_profile_info(hosts_definition, "gateway")
    hypervisor.create_networks(conf, install_server_info)

    hosts = hosts_definition['hosts']
    existing_hosts = ([n.name() for n in hypervisor.conn.listAllDomains()])
    for hostname in sorted(hosts):
        definition = hosts[hostname]
        hostname_with_prefix = "%s_%s" % (conf.prefix, hostname)
        definition['hostname'] = hostname
        definition['hostname_with_prefix'] = hostname_with_prefix
        exists = hostname_with_prefix in existing_hosts
        if exists and conf.replace:
            dom = hypervisor.conn.lookupByName(hostname_with_prefix)
            if dom.info()[0] in [libvirt.VIR_DOMAIN_RUNNING,
                                 libvirt.VIR_DOMAIN_PAUSED]:
                dom.destroy()
            if dom.info()[0] in [libvirt.VIR_DOMAIN_SHUTOFF]:
                dom.undefine()
            exists = False
        if not exists:
            host = Host(hypervisor, conf, definition,
                        install_server_info, gateway_info)
            hypervisor.conn.defineXML(host.dump_libvirt_xml())
            dom = hypervisor.conn.lookupByName(hostname_with_prefix)
            dom.create()
        else:
            logging.info("a host called %s is already defined, skipping "
                         "(see: --replace)." % hostname_with_prefix)

    logging.info("Waiting for install-server DHCP query with MAC %s" %
                 install_server_info['mac'])
    ip = hypervisor.wait_for_lease(
        hypervisor, install_server_info['mac'])

    logging.info("Install-server up and running with IP: %s" % ip)

    logging.info("Waiting for Gateway DHCP query with MAC %s" %
                 gateway_info['mac'])
    ip = hypervisor.wait_for_lease(
        hypervisor, gateway_info['mac'])

    logging.info("Gateway up and running with IP: %s" % ip)


if __name__ == '__main__':
    main()
