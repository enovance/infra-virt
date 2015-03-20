#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 eNovance SAS <licensing@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from hardware import cmdb
from hardware import generate
from hardware import state

import argparse
import glob
import netaddr
import os
import re
import sys

import requests
import six
import yaml

_VERSION = "0.0.1"


def _get_yaml_content(path):
    try:
        with open(path, "r") as f:
            return yaml.load(f)
    except (OSError, IOError) as e:
        print("Error: cannot open or read file '%s': %s" % (path, e))
        sys.exit(1)

INT_DHCP = {"bootproto": "dhcp",
            "nat": True,
            "network_name": "__public_network__"}


def _get_router_nics(config_path, global_conf):
    cmdb_files = glob.glob("%s/edeploy/*.cmdb" % config_path)
    cmdb_files = [os.path.splitext(os.path.basename(cmdb_file))[0]
                  for cmdb_file in cmdb_files]

    detected_net = {}
    for k, v in six.iteritems(global_conf['config']):
        m = re.search('(\w+)_(ip|netif|network)', k)
        if not m:
            continue
        net_name = m.group(1)
        if net_name not in detected_net:
            detected_net[net_name] = {}
        val_type = m.group(2)
        if val_type == 'netif':
            detected_net[net_name]['name'] = v
        elif val_type == 'ip':
            pass
        elif val_type == 'network':
            detected_net[net_name]['netobj'] = netaddr.IPNetwork(v)
        else:
            print("type not supported: %s" % val_type)

    for cmdb_file in cmdb_files:
        loaded_cmdb = cmdb.load_cmdb("%s/edeploy/" % config_path, cmdb_file)
        for host in loaded_cmdb:
            for k, v in six.iteritems(host):
                m = re.search('(vlan|gateway|netmask|network)(-(\w+|)|)', k)
                if not m:
                    continue
                net_name = m.group(3)
                val_type = m.group(1)
                if not net_name:
                    net_name = 'admin'

                if net_name not in detected_net:
                    detected_net[net_name] = {}

                # TODO(Gonéri): We should directly store the vlan ID in the
                # vlan key to avoid this ask
                if val_type == 'vlan' and 'name' not in detected_net[net_name]:
                    detected_net[net_name]['name'] = ("ethX.%s" % v)
                elif val_type == 'network':
                    detected_net[net_name]['netobj'] = netaddr.IPNetwork(v)
                else:
                    detected_net[net_name][val_type] = v

    for host in global_conf['hosts']:
        ip = global_conf['hosts'][host]['ip']
        found = False
        for net in detected_net.values():
            if 'netobj' in net and ip in net['netobj']:
                found = True
                continue
        if not found:
            detected_net[host + '-net'] = {
                'netobj': netaddr.IPNetwork(ip + '/24')
            }
    nics = {}
    for net in detected_net.values():
        if 'netobj' not in net:
            continue
        netobj = net['netobj']

        if netobj not in nics:
            nics[netobj] = {}
        if 'name' in net and 'name' not in nics[netobj]:
            nics[netobj]['name'] = net['name']
        if 'gateway' in net:
            nics[netobj]['ip'] = net['gateway']
        try:
            nics[netobj]['ip'] = net['gateway']
        except KeyError:
            pass
        nics[netobj]['netmask'] = str(net['netobj'].netmask)
        nics[netobj]['network'] = str(net['netobj'].network)

    i = 0
    for netobj, entry in six.iteritems(nics):
        try:
            m = re.search('\.(\d+)$', entry['name'])
            entry['name'] = 'eth%d.%s' % (i, m.group(1))
            entry['vlan'] = True
        except KeyError:
            entry['name'] = 'eth%d' % i
        if 'ip' not in entry:
            print("Cannot find the gateway for network %s." % netobj)
        i += 1

    return nics.values()


def _get_files(config_path):
    cmdb_files = glob.glob("%s/edeploy/*.cmdb" % config_path)
    cmdb_files = [os.path.splitext(os.path.basename(cmdb_file))[0]
                  for cmdb_file in cmdb_files]

    files = {}
    for cmdb_file in cmdb_files:
        loaded_cmdb = cmdb.load_cmdb("%s/edeploy/" % config_path, cmdb_file)
        configure_file = "%s/edeploy/%s.configure" % (config_path, cmdb_file)
        configure_file_content = ""
        try:
            configure_file_content = open(configure_file, 'r').read()
        except IOError:
            return {}

        for host in loaded_cmdb:
            files[host['hostname']] = []
            for line in re.findall(r'(config\([\s\S\n]*?)\)\n',
                                   configure_file_content, re.MULTILINE):
                m = re.match(r"config\([\"'](.+?)[\"'].*write\(([\S\s]*''')",
                             line, re.MULTILINE)
                file_path = m.group(1)
                # For the moment, we only use collect the network configuration
                if not file_path.startswith("/etc/sysconfig"):
                    continue
                files[host['hostname']].append({
                    'path': file_path,
                    'content': eval("%s %% %s" % (m.group(2), str(host)))
                })
    return files


def _get_checksum(images_url, sps_version, img):

    if not images_url:
        return None

    try:
        img_checksum = img.replace("qcow2", "md5")
        url = "%s/%s/%s" % (images_url, sps_version, img_checksum)
        resp = requests.get(url)
    except requests.exceptions.MissingSchema as e:
        print("Invalid url '%s': %s" % (url, e))
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print("Unreachable url '%s': %s" % (url, e))
        print("The infra description will not contain the images checksums.")
        return None

    return resp.text.split(" ")[0].encode("utf8")


def collect(config_path, qcow, sps_version, images_url, parse_configure_files):
    # check config directory path
    if not os.path.exists(config_path):
        print("Error: --config-dir='%s' does not exist." % config_path)
        sys.exit(1)

    images_checksums = {}
    # get state object
    state_obj = state.State()
    state_obj.load(os.path.join(config_path, 'edeploy') + '/')

    # get global conf
    global_conf = _get_yaml_content("%s/config-tools/global.yml" % config_path)
    # expand keys prefixed by "="
    global_conf["hosts"] = generate.generate_dict(global_conf["hosts"], "=")

    # the virtual configuration of each host
    virt_platform = {"hosts": {}}

    # adds router
    virt_platform["hosts"]["router"] = {}
    img_name = "%s-%s.img.qcow2" % ("install-server", sps_version)
    virt_platform["hosts"]["router"]["disks"] = [{'size': '15Gi',
                                                  'image': img_name}]
    virt_platform["hosts"]["router"]["profile"] = 'router'

    # adds image checksum to the router
    checksum = _get_checksum(images_url, sps_version, img_name)
    images_checksums[img_name] = checksum
    if checksum:
        virt_platform["hosts"]["router"]["disks"][0]['checksum'] = checksum

    virt_platform["hosts"]["router"]["nics"] = _get_router_nics(config_path,
                                                                global_conf)
    virt_platform["hosts"]["router"]["nics"].append(dict(INT_DHCP))

    gateway = virt_platform["hosts"]["router"]["nics"][0]["ip"]

    # adds hardware info to the hosts
    for hostname in global_conf["hosts"]:
        # construct the host virtual configuration
        virt_platform["hosts"][hostname] = state_obj.hardware_info(hostname)
        profile = global_conf["hosts"][hostname]["profile"]
        edeploy_role = global_conf["profiles"][profile]["edeploy"]
        img_name = "%s-%s.img.qcow2" % (edeploy_role, sps_version)

        if img_name not in images_checksums:
            checksum = _get_checksum(images_url, sps_version, img_name)
            images_checksums[img_name] = checksum

        if qcow or \
           global_conf["hosts"][hostname]["profile"] == "install-server":
            if 'disks' not in virt_platform["hosts"][hostname]:
                virt_platform["hosts"][hostname]["disks"] = [{'size': '40Gi'}]
            virt_platform["hosts"][hostname]["disks"][0]['image'] = img_name
            if images_checksums.get(img_name):
                virt_platform["hosts"][hostname]["disks"][0]['checksum'] = \
                    images_checksums.get(img_name)

        # add the profile
        virt_platform["hosts"][hostname]["profile"] = \
            global_conf["hosts"][hostname]["profile"]

    # release the lock obtained during the load call
    state_obj.unlock()

    configure_files = _get_files(config_path)
    # adds network info to the hosts
    for hostname in global_conf["hosts"]:
        admin_network = global_conf["config"]["admin_network"]
        admin_network = netaddr.IPNetwork(admin_network)
        try:
            nics = virt_platform["hosts"][hostname]["nics"]
        except KeyError:
            nics = [{}]

        network_configuration = "file" if parse_configure_files else "standard"

        if network_configuration == "file":
            try:
                virt_platform["hosts"][hostname]['files'] \
                    = configure_files[hostname]
            except KeyError:
                print("no network configure scripts for node %s. "
                      "Switch to the standard configuration system." % hostname)
                network_configuration = "standard"

        if network_configuration == "standard":
            nics[0].update({
                "name": "eth0",
                "ip": global_conf["hosts"][hostname]["ip"],
                "network": str(admin_network.network),
                "netmask": str(admin_network.netmask)})
            if gateway in admin_network:
                nics[0]['gateway'] = gateway
        if global_conf["hosts"][hostname]["profile"] == "install-server":
            nics.append(dict(INT_DHCP))
        virt_platform["hosts"][hostname]["nics"] = nics

    if images_url:
        virt_platform["images-url"] = "%s/%s" % (images_url, sps_version)
    return virt_platform


def save_virt_platform(virt_platform, output_path):
    output_file_path = os.path.normpath("%s/virt_platform.yml" % output_path)

    try:
        with open(output_file_path, 'w') as outfile:
            outfile.write(yaml.dump(virt_platform, default_flow_style=False))
        print("Virtual platform generated successfully at '%s' !" %
              output_file_path)
    except (OSError, IOError) as e:
        print("Error: cannot write file '%s': %s" % (output_file_path, e))
        sys.exit(1)


def main():
    cli_parser = argparse.ArgumentParser(
        description='Collect architecture information from the edeploy '
        'directory as generated by config-tools/download.sh.')
    cli_parser.add_argument('--config-dir',
                            default="./top/etc",
                            help='The config directory absolute path.')
    cli_parser.add_argument('--output-dir',
                            default="./",
                            help='The output directory of the virtual'
                                 ' configuration.')
    cli_parser.add_argument('--sps-version',
                            required=True,
                            help='The SpinalStack version.')
    cli_parser.add_argument('--qcow',
                            required=False,
                            default=False,
                            action="store_true",
                            help='Boot on qcow image.')
    cli_parser.add_argument('--parse-configure-files',
                            required=False,
                            default=False,
                            action="store_true",
                            help='Enable experimental .configure file \
                                  parsing.')
    cli_parser.add_argument('--images-url',
                            required=False,
                            help='Url of the qcow images.')

    cli_arguments = cli_parser.parse_args()

    virt_platform = collect(cli_arguments.config_dir, cli_arguments.qcow,
                            cli_arguments.sps_version,
                            cli_arguments.images_url,
                            cli_arguments.parse_configure_files)

    save_virt_platform(virt_platform,
                       cli_arguments.output_dir)


if __name__ == '__main__':
    main()
