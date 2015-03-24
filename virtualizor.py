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

from templates import host as host_template
from templates import network as network_template

import argparse
import logging
import random
import re
import string
import subprocess
import sys
import tempfile
import time
import uuid

import jinja2
import libvirt
import six
import xml.etree.ElementTree as ET
import yaml

logging.basicConfig(level=logging.DEBUG)

_LIBVIRT_IMAGE_DIR = "/var/lib/libvirt/images/"


def random_mac():
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255))


def canonical_size(size):
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
        if not re.match('^[\._a-zA-Z\d]+$', value):
            sys.stderr.write("Invalid value for --prefix parameter\n")
            sys.exit(1)
        return value
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description='Deploy a virtual infrastructure.')
    parser.add_argument('--cleanup', action='store_true',
                        help='existing resources with the same prefix will be '
                        'remove first.')
    parser.add_argument('input_file', type=str,
                        help='the YAML input file, as generated by '
                        'collector.py.')
    parser.add_argument('target_host', type=str,
                        help='the name of the libvirt server. The local user '
                        'must be able to connect to the root account with no '
                        'password authentification.')
    parser.add_argument('--pub-key-file', type=str, action='append',
                        default=[],
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
    def __init__(self, conf, infra_description):
        self._conf = conf
        self._infra_description = infra_description
        self.conn = libvirt.open('qemu+ssh://root@%s/system' %
                                 self._conf.target_host)
        self.emulator = self._find_emulator()
        if self.emulator is None:
            logging.error("No emulator found")
            sys.exit(2)

    def _find_emulator(self):
        for location in ('/usr/bin/qemu-system-x86_64',
                         '/usr/libexec/qemu-kvm'):
            if self.call('test', '-f', location) == 0:
                return location
        return None

    def download_images(self):

        if "images-url" not in self._infra_description:
            logging.warn("Images url is not provided by the infra description,"
                         " no images will be downloaded from the hypervisor.")
            return

        images_url = self._infra_description["images-url"]
        for host in self._infra_description["hosts"]:
            host_disks = self._infra_description["hosts"][host]["disks"]
            for disk in host_disks:
                if "image" not in disk:
                    continue
                libvirt_img = "%s/%s" % (_LIBVIRT_IMAGE_DIR, disk["image"])
                exist_img = self.call("test", "-s", libvirt_img)
                if exist_img == 0:
                    continue
                wget_status = self.call('wget', '--continue', '--no-verbose',
                                        '-O', libvirt_img,
                                        "%s/%s" % (images_url, disk["image"]))
                if wget_status != 0:
                    logging.error("Failed to download '%s' from '%s'" %
                                  (disk["image"], images_url))
                logging.info("Downloaded image '%s'" % libvirt_img)

    def configure_networks(self):
        existing_networks = [n.name() for n in self.conn.listAllNetworks()]
        # Ensure the public_network is defined, we don't replace this network,
        # even if --replace is used because other VM may by connected to the
        # same networks.
        if self._conf.public_network not in existing_networks:
            pub_net = Network(self._conf.public_network, {
                "dhcp": {"address": "192.168.140.1",
                         "netmask": "255.255.255.0",
                         "range": {
                             "ipstart": "192.168.140.2",
                             "ipend": "192.168.140.254"}}})
            self.conn.networkCreateXML(pub_net.dump_libvirt_xml())
        self.public_net = self.conn.networkLookupByName(
            self._conf.public_network)
        if not self.public_net.isActive():
            self.public_net.create()

        net_definitions = {"%s_sps" % self._conf.prefix: {}}
        for netname in net_definitions:
            exists = netname in existing_networks
            if exists and self._conf.cleanup:
                self.conn.networkLookupByName(netname).destroy()
                logging.info("Cleaning network %s." % netname)
                exists = False
            if not exists:
                logging.info("Creating network %s." % netname)
                network = Network(netname, net_definitions[netname])
                self.conn.networkCreateXML(network.dump_libvirt_xml())
        self.public_net = self.conn.networkLookupByName(
            self._conf.public_network)

    def wait_for_lease(self, mac):
        while True:
            if not hasattr(self.public_net, "DHCPLeases"):
                stdout = subprocess.check_output([
                    'ssh', 'root@%s' % self._conf.target_host, 'cat',
                    "/var/lib/libvirt/dnsmasq/%s.leases" %
                    self._conf.public_network])
                for line in stdout.split('\n'):
                    m = re.search("^\S+\s%s\s(\S+)\s" % mac, line)
                    if m:
                        return(m.group(1))

            else:
                for lease in self.public_net.DHCPLeases():
                    if lease['mac'] == mac:
                        return lease['ipaddr']

            time.sleep(1)

    def push(self, source, dest):
        subprocess.call(['scp', '-q', '-r', source,
                         'root@%s' % self._conf.target_host + ':' + dest])

    def call(self, *kargs):
        return subprocess.call(['ssh', 'root@%s' % self._conf.target_host] +
                               list(kargs))

    class MissingPublicNetwork(Exception):
        pass


class Host(object):

    def __init__(self, hypervisor, conf, host_definition):
        self.hypervisor = hypervisor
        self.conf = conf
        self.hostname = host_definition['hostname']
        self.files = host_definition.get('files', [])
        self.hostname_with_prefix = host_definition['hostname_with_prefix']

        self.meta = {'hostname': host_definition['hostname'],
                     'hostname_with_prefix':
                         host_definition['hostname_with_prefix'],
                     'uuid': str(uuid.uuid1()),
                     'emulator': self.hypervisor.emulator,
                     'memory': 8 * 1024 ** 2,
                     'ncpus': 2,
                     'cpus': [],
                     'disks': [],
                     'nics': [],
                     'prefix': conf.prefix}
        self.disk_cpt = 0

        for k in ('uuid', 'serial', 'product_name',
                  'memory', 'ncpus', 'profile'):
            if k not in host_definition:
                continue
            self.meta[k] = host_definition[k]

        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.template = env.from_string(host_template.HOST)

        for nic in host_definition['nics']:
            self._register_nic(nic)
        for disk in host_definition['disks']:
            self._initialize_disk(disk)
            self._register_disk(disk)
        if 'image' in host_definition['disks'][0]:
            cloud_init_image = self._create_cloud_init_image()
            self._register_disk(cloud_init_image)

        self.meta['nics'][0]['boot_order'] = 2
        self.meta['disks'][0]['boot_order'] = 1

    def _create_cloud_init_image(self):

        ssh_keys = []
        for file_path in self.conf.pub_key_file:
            with open(file_path) as fd:
                for line in fd.readlines():
                    ssh_keys.append(line)
        # NOTE(Gonéri): Add the unsecure key
        ssh_keys.append(
            'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDjutnSpI6rMsiym'
            'cwW9/BJiyazTScmSawsct6b/0my+ozZuuTSd9QPOv/oFuJ4fgegL4Z'
            'kyeGHbWBfQL+eT99gUs/7R9jYptE5tsJt1kaU4KC4xwlCBwrPSxT2a'
            'azj5s9yaeKX6PyWM/4qOsENeJm2XqxiPQKegPcyVVl8eYsjbTFrRo4'
            'JS+MsOyBONUWG1EN0QddyXhcc8nlEpP2wQGUpuEKYj/o7Wk3EQYRa/'
            'DBATTz71eX2snTz+dv+Ei7OFMYl0Rf+7uPCJ3GdhvMXI92CziMIcMB'
            'yKpSiuWB3/LDoezTPYhgbEP0I+zqyHHxpGZUsYx/5ic5RAdUPUeLQW'
            'sEX unsecure')
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        user_data = {
            'users': [
                {
                    'name': 'jenkins',
                    'ssh-authorized-keys': ssh_keys
                },
                {
                    'name': 'root',
                    'ssh-authorized-keys': ssh_keys
                }
                  ],
            'write_files': [
                {
                    'path': '/etc/resolv.conf',
                    'content': "nameserver 8.8.8.8"
                },
                {
                    'path': '/etc/sudoers.d/jenkins-cloud-init',
                    'permissions': '0440',
                    'content': """
Defaults:jenkins !requiretty
jenkins ALL=(ALL) NOPASSWD:ALL
"""
                },
                {
                    'path': '/etc/sysconfig/network',
                    'content': "NETWORKING=yes\n" +
                    "NOZEROCONF=no\n" +
                    "HOSTNAME=%s\n" % self.hostname},
                {
                    'path': '/etc/sysctl.conf',
                    # TODO(Gonéri): Should be there only for the router
                    'content': "net.ipv4.ip_forward = 1"
                },
                {
                    'path': '/root/.ssh/id_rsa',
                    'permissions': '0400',
                    'owner': 'root:root',
                    'content': host_template.PRIVATE_SSH_KEY
                },
                {
                    'path': '/var/lib/jenkins/.ssh/id_rsa',
                    'permissions': '0400',
                    'owner': 'root:root',
                    # TODO(Gonéri): duplicated key
                    'content': host_template.PRIVATE_SSH_KEY
                }
            ],
            'runcmd': [
                '/bin/rm -f /etc/yum.repos.d/*.repo',
                '/bin/systemctl restart network',
                '/usr/sbin/service networking restart'
            ],
            'bootcmd': [
                '/sbin/sysctl -p'
            ]
        }
        for nic in self.meta['nics']:
            content = ("#Generated by virtualizor.py\n"
                       "DEVICE=%(name)s\n"
                       "ONBOOT=yes\n"
                       "IPADDR=%(ip)s\n"
                       "NETWORK=%(network)s\n"
                       "NETMASK=%(netmask)s\n"
                       "GATEWAY=%(gateway)s\n"
                       "BOOTPROTO=%(bootproto)s\n") % nic
            if nic['vlan']:
                content += "VLAN=yes\n"

            user_data['write_files'].append({
                'path': '/etc/sysconfig/network-scripts/ifcfg-' + nic['name'],
                'content': content})
            if nic['nat']:
                user_data['bootcmd'].append(
                    '/sbin/iptables -t nat -A POSTROUTING -o ' +
                    nic['name'] +
                    ' -j MASQUERADE')
        user_data['write_files'] += self.files
        contents = {
            'user-data': "#cloud-config\n" + yaml.dump(user_data),
            'meta-data': env.from_string(host_template.META_DATA).render({
                'hostname': self.hostname
            })}
        # TODO(Gonéri): use mktemp
        data_dir = "/tmp/%s_data" % self.hostname_with_prefix
        self.hypervisor.call("mkdir", "-p", data_dir)
        for name in sorted(contents):
            fd = tempfile.NamedTemporaryFile()
            fd.write(contents[name]),
            fd.seek(0)
            fd.flush()
            self.hypervisor.push(fd.name, data_dir + '/' + name)

        image = '%s/%s_cloud-init.qcow2' % (
                host_template.HOST_LIBVIRT_IMAGES_LOCATION,
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
        return {'path': image}

    def _initialize_disk(self, disk):
        disk_cpt = len(self.meta['disks'])
        filename = "%s-%03d.qcow2" % (self.hostname_with_prefix, disk_cpt)
        if 'image' in disk:
            self.hypervisor.call(
                'qemu-img', 'create', '-q', '-f', 'qcow2',
                '-b', disk['image'],
                host_template.HOST_LIBVIRT_IMAGES_LOCATION + '/' + filename,
                canonical_size(disk['size']))
            self.hypervisor.call(
                'qemu-img', 'resize', '-q',
                host_template.HOST_LIBVIRT_IMAGES_LOCATION + '/' + filename,
                canonical_size(disk['size']))
        else:
            self.hypervisor.call(
                'qemu-img', 'create', '-q', '-f', 'qcow2',
                host_template.HOST_LIBVIRT_IMAGES_LOCATION + '/' + filename,
                canonical_size(disk['size']))

        disk.update({'path': "%s/%s" %
                             (host_template.HOST_LIBVIRT_IMAGES_LOCATION,
                              filename)})

    def _register_disk(self, disk):
        disk_cpt = len(self.meta['disks'])
        disk['name'] = 'vd' + string.ascii_lowercase[disk_cpt]
        self.meta['disks'].append(disk)

    def _register_nic(self, nic):
        nic.setdefault('network_name', '%s_sps' % self.conf.prefix)
        nic.setdefault('bootproto', 'dhcp')
        nic.setdefault('ip', '')
        nic.setdefault('network', '')
        nic.setdefault('netmask', '')
        nic.setdefault('gateway', '')
        nic.setdefault('nat', False)
        nic.setdefault('vlan', False)
        if nic['network_name'] == '__public_network__':
            nic['network_name'] = self.conf.public_network
        if nic['ip']:
            nic['bootproto'] = 'static'
        self.meta['nics'].append(nic)

    def dump_libvirt_xml(self):
        return self.template.render(self.meta)


class Network(object):

    def __init__(self, name, network_definition):
        self._template_values = {
            'name': name,
            'uuid': str(uuid.uuid1()),
            'mac': random_mac(),
            'bridge_name': 'virbr%d' % random.randrange(0, 0xffffffff)}

        for k in ('uuid', 'mac', 'ips', 'dhcp'):
            if k not in network_definition:
                continue
            self._template_values[k] = network_definition[k]

        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self._template = env.from_string(network_template.NETWORK)

    def dump_libvirt_xml(self):
        return self._template.render(self._template_values)


def load_infra_description(input_file):
    infra_description = yaml.load(open(input_file, 'r'))

    for hostname, definition in six.iteritems(infra_description['hosts']):
        i = 0
        # Add the missing MAC because we use them later to know then the DHCP
        # give the IP
        for n in definition["nics"]:
            # TODO(Gonéri): to move in _register_nic
            n.setdefault('mac', random_mac())
            n.setdefault('name', 'eth%d' % i)
            # NOTE(Gonéri): hardware can return mac == none when the MAC is not
            # defined.
            if n['mac'] == 'none':
                n['mac'] = random_mac()
            i += 1
    return infra_description


def purge_existing_domains(hypervisor, prefix):
    logging.info("Cleaning the %s prefix up on the hypervisor" % prefix)
    existing_domains = [d for d in hypervisor.conn.listAllDomains()]
    for dom in existing_domains:
        try:
            metadata = dom.metadata(
                libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                'http://virtualizor/instance',
                flags=libvirt.VIR_DOMAIN_AFFECT_CONFIG)
        except libvirt.libvirtError as e:
            if e.message == ('metadata not found: Requested '
                             'metadata element is not present'):
                continue
            else:
                raise(e)
        root = ET.fromstring(metadata)
        try:
            dom_prefix = root.find('prefix').text
        except AttributeError:
            continue
        if prefix == dom_prefix:
            logging.debug("purging domain %s" % dom.name())
            if dom.info()[0] in [libvirt.VIR_DOMAIN_RUNNING,
                                 libvirt.VIR_DOMAIN_PAUSED]:
                dom.destroy()
            if dom.info()[0] in [libvirt.VIR_DOMAIN_SHUTOFF]:
                dom.undefine()


def main(argv=sys.argv[1:]):
    conf = get_conf(argv)
    infra_description = load_infra_description(conf.input_file)
    hypervisor = Hypervisor(conf, infra_description)

    if conf.cleanup:
        purge_existing_domains(hypervisor, conf.prefix)
    hypervisor.configure_networks()

    hypervisor.download_images()

    hosts = infra_description['hosts']

    for hostname in sorted(hosts):
        host_description = hosts[hostname]
        hostname_with_prefix = "%s_%s" % (conf.prefix, hostname)
        host_description['hostname'] = hostname
        host_description['hostname_with_prefix'] = hostname_with_prefix
        host = Host(hypervisor, conf, host_description)
        hypervisor.conn.defineXML(host.dump_libvirt_xml())
        dom = hypervisor.conn.lookupByName(hostname_with_prefix)
        dom.create()

    for hostname, host_description in \
            six.iteritems(infra_description['hosts']):
        for n in host_description['nics']:
            try:
                if n['network_name'] != conf.public_network:
                    continue
                if n['bootproto'] != 'dhcp':
                    continue
            except KeyError:
                continue

            logging.info("Waiting for '%s' DHCP query with MAC '%s'" % (
                hostname, n['mac']))
            logging.info("Host '%s' has public IP: '%s'" % (
                hostname, hypervisor.wait_for_lease(n['mac'])))


if __name__ == '__main__':
    main()
