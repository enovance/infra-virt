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

import glob
import netaddr
import os
import sys

import argparse
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
            "name": "eth1",
            "nat": True,
            "network_name": "__public_network__"}


def _get_router_nic(config_path):
    cmdb_files = glob.glob("%s/edeploy/*.cmdb" % config_path)
    cmdb_files = [os.path.splitext(os.path.basename(cmdb_file))[0]
                  for cmdb_file in cmdb_files]

    for cmdb_file in cmdb_files:
        loaded_cmdb = cmdb.load_cmdb("%s/edeploy/" % config_path, cmdb_file)
        for host in loaded_cmdb:
            if "gateway" in host and "netmask" in host:
                net = netaddr.IPNetwork("%s/%s" % (host["gateway"],
                                                   host["netmask"])).network

                return {"ip": host["gateway"],
                        "name": "eth0",
                        "netmask": host["netmask"],
                        "network": str(net)}


def collect(config_path, qcow, sps_version):
    # check config directory path
    if not os.path.exists(config_path):
        print("Error: --config-dir='%s' does not exist." % config_path)
        sys.exit(1)

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
    img = "%s-%s.img.qcow2" % ("install-server", sps_version)
    virt_platform["hosts"]["router"]["disks"] = [{'size': '15Gi',
                                                  'image': img}]

    virt_platform["hosts"]["router"]["nics"] = [_get_router_nic(config_path)]
    virt_platform["hosts"]["router"]["nics"].append(dict(INT_DHCP))

    gateway = virt_platform["hosts"]["router"]["nics"][0]["ip"]

    # adds hardware info to the hosts
    for hostname in global_conf["hosts"]:
        # construct the host virtual configuration
        virt_platform["hosts"][hostname] = state_obj.hardware_info(hostname)
        img = "%s-%s.img.qcow2" % (global_conf["hosts"][hostname]["profile"],
                                   sps_version)
        if qcow or \
           global_conf["hosts"][hostname]["profile"] == "install-server":
            if 'disks' not in virt_platform["hosts"][hostname]:
                virt_platform["hosts"][hostname]["disks"] = [{'size': '15Gi'}]
            virt_platform["hosts"][hostname]["disks"][0]['image'] = img

        # add the profile
        virt_platform["hosts"][hostname]["profile"] = \
            global_conf["hosts"][hostname]["profile"]

        virt_platform["hosts"][hostname]["nics"][0].update(
            {"gateway": gateway})

    # release the lock obtained during the load call
    state_obj.unlock()

    for hostname in global_conf["hosts"]:
        admin_network = global_conf["config"]["admin_network"]
        admin_network = netaddr.IPNetwork(admin_network)
        try:
            nics = virt_platform["hosts"][hostname]["nics"]
        except KeyError:
            nics = [{}]

        nics[0].update({
            "name": "eth0",
            "ip": global_conf["hosts"][hostname]["ip"],
            "network": str(admin_network.network),
            "netmask": str(admin_network.netmask)})
        if global_conf["hosts"][hostname]["profile"] == "install-server":
            nics.append(dict(INT_DHCP))
        virt_platform["hosts"][hostname]["nics"] = nics

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

    cli_arguments = cli_parser.parse_args()

    virt_platform = collect(cli_arguments.config_dir, cli_arguments.qcow,
                            cli_arguments.sps_version)
    save_virt_platform(virt_platform,
                       cli_arguments.output_dir)


if __name__ == '__main__':
    main()
